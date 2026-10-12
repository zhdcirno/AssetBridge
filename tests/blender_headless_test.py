# -*- coding: utf-8 -*-
"""
Asset Bridge :: Blender 发送端 headless 实测驱动脚本
============================================================================
用法（PowerShell / Git Bash）：
    "D:\\DCC\\Blender5.0.1\\blender.exe" --background ^
        --python "E:\\CirnoWorkSpace\\AssetBridge\\tests\\blender_headless_test.py"

全程无 GUI。脚本按 a~f 顺序实测，每步打印结果，最后把原始数据落到
    E:\\CirnoWorkSpace\\AssetBridge\\logs\\_raw_result.json
（人类可读报告由外层流程据此生成）。

注意：本脚本只做「探测 + 驱动」，不修改插件源码；发现的不兼容要回去改
      blender\\addon\\asset_bridge_blender\\ 下的实现。
"""
import json
import os
import shutil
import struct
import sys
import traceback
import urllib.request
from pathlib import Path

import bpy
import mathutils

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ADDON_DIR = r"E:\CirnoWorkSpace\AssetBridge\blender\addon"
LOG_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\logs")
RAW_JSON = LOG_DIR / "_raw_result.json"
SCALE_PROBE_DIR = LOG_DIR / "_scale_probe"
SERVER = "http://127.0.0.1:27631"

RESULT = {
    "steps": [],       # 每一步的打印记录
    "errors": [],      # 运行时异常
    "dropped_params": [],  # 当前 Blender 不支持而被忽略的导出/导入参数
    "audit": {},       # operator 参数审计
}


def log(msg: str, tag: str = "INFO") -> None:
    """统一打印 + 记录，便于事后回看。"""
    line = f"[{tag}] {msg}"
    print(line, flush=True)
    RESULT["steps"].append(line)


def http_json(method: str, path: str, body=None, timeout: float = 5.0):
    """绕过系统代理访问本机中转服务（公司网络常设 HTTP_PROXY，会把 127.0.0.1 也代理掉）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url=SERVER + "/api/v1" + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# 极简 FBX 二进制解析：只为了读出 GlobalSettings 里的 UnitScaleFactor
# ---------------------------------------------------------------------------

def _fbx_read_prop(data: bytes, off: int):
    t = data[off:off + 1]
    off += 1
    if t == b"Y":
        return struct.unpack_from("<h", data, off)[0], off + 2
    if t == b"C":
        return bool(data[off]), off + 1
    if t == b"I":
        return struct.unpack_from("<i", data, off)[0], off + 4
    if t == b"F":
        return struct.unpack_from("<f", data, off)[0], off + 4
    if t == b"D":
        return struct.unpack_from("<d", data, off)[0], off + 8
    if t == b"L":
        return struct.unpack_from("<q", data, off)[0], off + 8
    if t in (b"R", b"S"):
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        return data[off:off + n], off + n
    if t in (b"b", b"i", b"l", b"f", b"d", b"c"):
        # 数组属性：元素个数 / 编码(1=zlib) / 压缩后长度
        n, enc, clen = struct.unpack_from("<III", data, off)
        off += 12
        return ("ARRAY", t, n, enc, data[off:off + clen]), off + clen
    raise ValueError(f"未知属性类型 {t!r} @ {off}")


def _fbx_read_node(data: bytes, off: int, hdr64: bool):
    if hdr64:
        end_off, _np, prop_len = struct.unpack_from("<QQQ", data, off)
        off += 24
    else:
        end_off, _np, prop_len = struct.unpack_from("<III", data, off)
        off += 12
    name_len = data[off]
    off += 1
    name = data[off:off + name_len].decode("ascii", "replace")
    off += name_len
    props, pend = [], off + prop_len
    while off < pend:
        v, off = _fbx_read_prop(data, off)
        props.append(v)
    return name, props, off, end_off


def _fbx_walk(data: bytes, off: int, end: int, hdr64: bool, path, out) -> int:
    """遍历节点树：收集 Properties70 的 P 条目，以及第一个 Geometry 的 Vertices 数组。"""
    hdr = 25 if hdr64 else 13
    while off + hdr <= end:
        if data[off:off + hdr] == b"\x00" * hdr:  # 子节点列表结束哨兵
            return off + hdr
        name, props, coff, end_off = _fbx_read_node(data, off, hdr64)
        node_end = end_off if end_off else end
        here = path + (name,)

        # Properties70 下的 P 节点：[名字, 类型, 标签, ?, 值...]
        if name == "P" and len(props) >= 4 and isinstance(props[0], bytes):
            out.setdefault("props", {}).setdefault("/".join(path[-2:]), []).append(
                (props[0].decode("ascii", "replace"), props)
            )

        # Geometry 下的 Vertices：直接量文件里真实写的坐标量级
        if name == "Vertices" and props and "verts" not in out:
            out["verts"] = _fbx_decode_double_array(props[0])

        # Model 的 Lcl Scaling：米->厘米的换算就藏在这个节点缩放里。
        # 注意必须限定在 Objects/Model 下 —— Definitions/ObjectType/PropertyTemplate
        # 里也有一个同名的模板属性（恒为 1,1,1），会把它盖掉。
        if (name == "P" and isinstance(props[0], bytes)
                and len(path) >= 2 and path[0] == "Objects" and path[1] == "Model"
                and "lcl_scaling" not in out):
            if props[0].decode("ascii", "replace") == "Lcl Scaling":
                nums = [q for q in props if isinstance(q, (int, float))
                        and not isinstance(q, bool)]
                if len(nums) >= 3:
                    out["lcl_scaling"] = [round(float(x), 6) for x in nums[-3:]]

        if coff < node_end:
            _fbx_walk(data, coff, node_end, hdr64, here, out)
        off = node_end
        if node_end >= end:
            break
    return off


def _fbx_decode_double_array(prop):
    """把 FBX 数组属性解成 float 列表，返回 (最大值绝对值, 元素个数)。"""
    import zlib
    if not (isinstance(prop, tuple) and prop and prop[0] == "ARRAY"):
        return None
    _tag, t, n, enc, raw = prop
    if enc == 1:
        raw = zlib.decompress(raw)
    fmt = {b"d": "<d", b"f": "<f"}.get(t)
    if fmt is None:
        return None
    step = struct.calcsize(fmt)
    vals = [struct.unpack_from(fmt, raw, i * step)[0] for i in range(n)]
    return {"count": n, "max_abs": round(max(abs(v) for v in vals), 6)}


def read_fbx_settings(path: Path) -> dict:
    """读出 FBX 头里的单位/轴向声明，以及文件里真实写入的顶点坐标量级。"""
    out = {}
    try:
        data = path.read_bytes()
        if not data.startswith(b"Kaydara FBX Binary  "):
            out["format"] = "ascii-or-unknown"
            return out
        version = struct.unpack_from("<I", data, 23)[0]
        hdr64 = version >= 7500
        out["fbx_version"] = int(version)
        collected: dict = {}
        _fbx_walk(data, 27, len(data), hdr64, (), collected)
        if collected.get("verts"):
            out["vertices"] = collected["verts"]
        if collected.get("lcl_scaling"):
            out["model_lcl_scaling"] = collected["lcl_scaling"]

        # 有效单位判定：顶点量级 × 根节点缩放 = 文件里真正的场景单位
        v = (collected.get("verts") or {}).get("max_abs")
        s = (collected.get("lcl_scaling") or [None])[0]
        if v is not None and s:
            effective = v * s          # 1m 立方体：0.5*100=50 -> cm；0.5*1=0.5 -> m
            out["effective_half_extent"] = round(effective, 6)
            out["effective_unit"] = "cm" if abs(effective - 50) < 5 else (
                "m" if abs(effective - 0.5) < 0.05 else "?")

        wanted = {"UnitScaleFactor", "OriginalUnitScaleFactor", "UpAxis",
                  "FrontAxis", "CoordSystem"}
        # 值一定在属性列表末尾，倒着找更稳
        for owner, items in collected.get("props", {}).items():
            for key, props in items:
                if key not in wanted:
                    continue
                val = None
                for p in reversed(props):
                    if isinstance(p, bool):
                        continue
                    if isinstance(p, (int, float)):
                        val = p
                        break
                out.setdefault(key, {"owner": owner, "value": val})
    except Exception as exc:
        out["parse_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# 工具：量世界坐标包围盒
# ---------------------------------------------------------------------------

def world_bbox_size(ob) -> list:
    """返回对象在世界坐标下的 AABB 尺寸（米）。"""
    bpy.context.view_layer.update()
    corners = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    return [round(max(c[i] for c in corners) - min(c[i] for c in corners), 6)
            for i in range(3)]


def reimport_and_measure(fbx_path: Path, tag: str) -> dict:
    """用中性设置（global_scale=1.0）把 FBX 读回来，量尺寸。"""
    valid = {p.identifier for p in bpy.ops.import_scene.fbx.get_rna_type().properties}
    kwargs = {"filepath": str(fbx_path), "global_scale": 1.0}
    kwargs = {k: v for k, v in kwargs.items() if k in valid}
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(**kwargs)
    fresh = [o for o in (set(bpy.data.objects) - before) if o.type == "MESH"]
    info = {"tag": tag, "imported_objects": [o.name for o in fresh],
            "dimensions_m": [world_bbox_size(o) for o in fresh]}
    for o in fresh:
        o.name = f"{tag}_{o.name}"
    log(f"    回读 {tag}: 对象={info['imported_objects']} 尺寸={info['dimensions_m']}")
    return info


# ===========================================================================
# a. 报告版本
# ===========================================================================

def step_a() -> None:
    log("=" * 60)
    log("步骤 a：报告真实 Blender 版本")
    log(f"bpy.app.version_string = {bpy.app.version_string}")
    log(f"bpy.app.version        = {tuple(bpy.app.version)}")
    log(f"Blender 自带 Python     = {sys.version.split()[0]}")
    RESULT["blender_version"] = bpy.app.version_string
    RESULT["blender_python"] = sys.version.split()[0]
    log("=" * 60)


# ===========================================================================
# 加载插件
# ===========================================================================

def load_addon() -> None:
    log("步骤 0：加载插件（优先 addon_enable，失败则直接 register）")
    # 从空场景起步，保证结果可复现
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sys.path.insert(0, ADDON_DIR)
    mode = "addon_enable"
    try:
        bpy.ops.preferences.addon_enable(module="asset_bridge_blender")
        prefs = bpy.context.preferences.addons["asset_bridge_blender"].preferences
        log(f"  方式1 addon_enable 成功，server_url={prefs.server_url}")
    except Exception as exc:
        log(f"  方式1 addon_enable 失败：{type(exc).__name__}: {exc}", "WARN")
        try:
            import asset_bridge_blender
            asset_bridge_blender.register()
            prefs = bpy.context.preferences.addons["asset_bridge_blender"].preferences
            mode = "direct_register"
            log(f"  方式2 直接 register 成功，server_url={prefs.server_url}")
        except Exception as exc2:
            log(f"  方式2 也失败：{type(exc2).__name__}: {exc2}", "ERROR")
            raise
    RESULT["addon_load_mode"] = mode


# ===========================================================================
# b. 建 1m 立方体 + 材质 + UV
# ===========================================================================

def step_b():
    log("步骤 b：建 1m x 1m x 1m 立方体（默认 Cube 是 2m，必须用 size=1.0）")
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    cube = bpy.context.view_layer.objects.active
    cube.name = "Cube"
    bpy.context.view_layer.update()
    log(f"  对象名={cube.name} 类型={cube.type}")
    log(f"  初始 dimensions={tuple(round(v, 6) for v in cube.dimensions)}")
    log(f"  初始 世界AABB={world_bbox_size(cube)}")

    # 材质槽：M_Test
    mat = bpy.data.materials.get("M_Test")
    if mat is None:
        mat = bpy.data.materials.new("M_Test")
    if cube.data.materials:
        cube.data.materials[0] = mat
    else:
        cube.data.materials.append(mat)
    log(f"  材质槽={[ms.material.name if ms.material else None for ms in cube.material_slots]}")

    # UV 层：primitive_cube_add 自带 UVMap，缺了才补
    uv_names = [uv.name for uv in cube.data.uv_layers]
    if not uv_names:
        cube.data.uv_layers.new(name="UVMap")
        uv_names = [uv.name for uv in cube.data.uv_layers]
    log(f"  UV 层={uv_names}")

    # 选中它（ operator 依赖 context.selected_objects ）
    bpy.ops.object.select_all(action="DESELECT")
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube
    log(f"  选中={[o.name for o in bpy.context.selected_objects]} "
        f"active={bpy.context.view_layer.objects.active.name}")
    RESULT["source_cube_size_m"] = world_bbox_size(cube)
    return cube


# ===========================================================================
# c. 设偏好 / 场景属性
# ===========================================================================

def step_c() -> None:
    log("步骤 c：设置插件偏好与场景属性")
    prefs = bpy.context.preferences.addons["asset_bridge_blender"].preferences
    prefs.server_url = SERVER
    prefs.project = "AssetBridgeTest"
    prefs.target_app = "unreal"
    prefs.content_path = "/Game/Bridge"
    prefs.send_mode = "update"
    # 资产名已经带 SM_ 前缀，关掉自动前缀避免变成 SM_SM_CalibCube
    prefs.use_prefix = False
    log(f"  server_url={prefs.server_url} project={prefs.project} "
        f"target_app={prefs.target_app} content_path={prefs.content_path} "
        f"send_mode={prefs.send_mode} use_prefix={prefs.use_prefix}")

    sc = bpy.context.scene
    sc.bridge_asset_name = "SM_CalibCube"
    sc.bridge_note = "blender 5.0.1 headless 校准测试"
    # 关键验证点：RNA 属性用 scene.get() 是取不到的（只认 ID 自定义属性）
    via_get = sc.get("bridge_asset_name", "<取不到>")
    via_attr = getattr(sc, "bridge_asset_name", "<取不到>")
    log(f"  scene.bridge_asset_name 写入后：getattr 读到={via_attr!r}，scene.get() 读到={via_get!r}")
    RESULT["scene_prop_via_get"] = via_get
    RESULT["scene_prop_via_getattr"] = via_attr
    if via_attr != "SM_CalibCube":
        RESULT["errors"].append("场景属性 bridge_asset_name 写入后读回不一致")


# ===========================================================================
# operator 参数审计（不真正调用，只看哪些参数/枚举值在当前版本合法）
# ===========================================================================

def audit_operator(op, wanted: dict, label: str) -> None:
    props = {p.identifier: p for p in op.get_rna_type().properties}
    ok, missing, bad_enum = {}, [], []
    for k, v in wanted.items():
        if k not in props:
            missing.append(k)
            continue
        p = props[k]
        if p.type == "ENUM":
            items = {i.identifier for i in p.enum_items}
            if p.is_enum_flag:
                # 多选枚举：值是集合，逐个校验
                bad = sorted(set(v) - items) if isinstance(v, (set, frozenset)) else []
                if bad:
                    bad_enum.append({"param": k, "value": sorted(v),
                                     "invalid": bad, "valid": sorted(items)})
                    continue
                ok[k] = sorted(v)
            elif not isinstance(v, str) or v not in items:
                bad_enum.append({"param": k, "value": v, "valid": sorted(items)})
                continue
            else:
                ok[k] = v
        else:
            ok[k] = v
    RESULT["audit"][label] = {
        "missing": missing,
        "bad_enum": bad_enum,
        "accepted": {k: str(v) for k, v in ok.items()},
    }
    log(f"  审计 {label}: 缺失={missing} 非法枚举={[b['param'] for b in bad_enum]}")
    for b in bad_enum:
        if "invalid" in b:
            log(f"    ! {b['param']} 非法项={b['invalid']} 合法项={b['valid']}", "WARN")
        else:
            log(f"    ! {b['param']}={b['value']} 合法项={b['valid']}", "WARN")


def audit_params(cube) -> None:
    log("步骤 c2：审计 export/import operator 的参数在当前 Blender 是否合法")
    from asset_bridge_blender import bridge_state

    profile = bridge_state.route_profile("blender", "unreal")
    exp = dict(profile.get("export", {}))
    wanted = {
        "filepath": "x.fbx", "use_selection": True,
        "object_types": {"MESH", "ARMATURE", "EMPTY", "OTHER"},
        "global_scale": exp.get("global_scale", 1.0),
        "apply_unit_scale": exp.get("apply_unit_scale", True),
        "use_space_transform": exp.get("use_space_transform", True),
        "bake_space_transform": exp.get("bake_space_transform", False),
        "add_leaf_bones": exp.get("add_leaf_bones", False),
        "use_mesh_modifiers": True, "use_mesh_modifiers_render": False,
        "mesh_smooth_type": "FACE", "use_custom_props": False,
        "path_mode": exp.get("path_mode", "COPY"),
        "embed_textures": exp.get("embed_textures", True),
        "batch_mode": "OFF", "use_metadata": True,
    }
    for k in ("primary_bone_axis", "secondary_bone_axis", "axis_forward", "axis_up"):
        if k in exp:
            wanted[k] = exp[k]
    audit_operator(bpy.ops.export_scene.fbx, wanted, "export_scene.fbx")

    imp_profile = bridge_state.route_profile("unreal", "blender").get("import", {})
    audit_operator(bpy.ops.import_scene.fbx, {
        "filepath": "x.fbx",
        "global_scale": imp_profile.get("global_scale", 1.0),
        "use_manual_orientation": imp_profile.get("use_manual_orientation", False),
        "automatic_bone_orientation": imp_profile.get("automatic_bone_orientation", True),
        "use_prepost_rot": imp_profile.get("use_prepost_rot", True),
        "use_custom_props": True, "use_image_search": True,
    }, "import_scene.fbx")


# ===========================================================================
# d. 调用 bpy.ops.bridge.send_selection()
# ===========================================================================

def step_d():
    log("步骤 d：调用 bpy.ops.bridge.send_selection()")
    from asset_bridge_blender import operators
    from asset_bridge_blender.bridge_client import BridgeClient
    from asset_bridge_blender import io_fbx

    captured = {"tid": None, "fbx": None, "publish": None, "reports": []}

    # --- 探针：不改行为，只记录 ---
    _pub = BridgeClient.publish
    def publish(self, tid):
        captured["tid"] = tid
        rec = _pub(self, tid)
        captured["publish"] = rec
        return rec
    BridgeClient.publish = publish

    _exp = io_fbx.export_selection
    def export_selection(filepath, profile, objects):
        captured["fbx"] = filepath
        return _exp(filepath, profile, objects)
    io_fbx.export_selection = export_selection

    # report 是 C 侧方法，类上取不到，只能给子类挂一个转发实现
    def report(self, type_set, message):
        captured["reports"].append(f"{sorted(str(t) for t in type_set)}: {message}")
        return bpy.types.Operator.report(self, type_set, message)
    operators.BRIDGE_OT_send_selection.report = report

    ret = None
    try:
        ret = bpy.ops.bridge.send_selection()
        ret = sorted(str(x) for x in ret) if ret else None
    except Exception as exc:
        RESULT["errors"].append(f"send_selection 抛出: {type(exc).__name__}: {exc}")
        log(f"  抛出 {type(exc).__name__}: {exc}", "ERROR")
        log(traceback.format_exc(), "ERROR")
    finally:
        BridgeClient.publish = _pub
        io_fbx.export_selection = _exp
        try:
            del operators.BRIDGE_OT_send_selection.report
        except AttributeError:
            pass

    log(f"  返回值={ret}")
    for r in captured["reports"]:
        log(f"  operator 报告：{r}")
    log(f"  transfer_id={captured['tid']}")
    log(f"  fbx 落盘={captured['fbx']}")

    RESULT["send_return"] = ret
    RESULT["transfer_id"] = captured["tid"]
    RESULT["fbx_path"] = captured["fbx"]
    RESULT["operator_reports"] = captured["reports"]
    RESULT["published"] = captured["publish"]
    RESULT["send_success"] = bool(ret and "FINISHED" in ret and captured["tid"])
    return captured


# ===========================================================================
# e. 用服务 API 复核
# ===========================================================================

def step_e(tid: str, fbx_path: str) -> None:
    log("步骤 e：用中转服务 API 复核 transfer / 文件落盘")
    if not tid:
        log("  没有 transfer_id，跳过复核", "WARN")
        return
    try:
        rec = http_json("GET", f"/transfers/{tid}")
        r = rec.get("transfer", rec)
        log(f"  status={r.get('status')} revision={r.get('revision')} "
            f"asset_name={r.get('asset_name')} to={r.get('to')}")
        log(f"  files={r.get('files')} unit={r.get('unit')}")
        log(f"  objects={r.get('objects')}")
        RESULT["server_record"] = {
            "status": r.get("status"), "revision": r.get("revision"),
            "asset_name": r.get("asset_name"), "files": r.get("files"),
            "unit": r.get("unit"), "objects": r.get("objects"),
            "kind": r.get("kind"), "format": r.get("format"),
        }
    except Exception as exc:
        RESULT["errors"].append(f"查询 transfer 失败: {exc}")
        log(f"  查询 transfer 失败: {exc}", "ERROR")

    if fbx_path and os.path.exists(fbx_path):
        size = os.path.getsize(fbx_path)
        log(f"  FBX 字节数={size}  (>0 判定：{size > 0})")
        RESULT["fbx_bytes"] = size
        RESULT["fbx_exists"] = True
        RESULT["fbx_settings"] = read_fbx_settings(Path(fbx_path))
        log(f"  FBX 头声明={RESULT['fbx_settings']}")
    else:
        log(f"  FBX 没落盘：{fbx_path}", "ERROR")
        RESULT["fbx_exists"] = False
        RESULT["fbx_bytes"] = 0
        RESULT["errors"].append(f"FBX 未落盘: {fbx_path}")

    # 收件箱侧确认（应该已经 queued 到 unreal）
    try:
        inbox = http_json("GET", "/inbox?app=unreal&limit=10")
        names = [(t.get("asset_name"), t.get("status"), t.get("revision"))
                 for t in inbox.get("transfers", [])]
        log(f"  unreal 收件箱（前10）= {names}")
        RESULT["inbox_unreal"] = names
    except Exception as exc:
        log(f"  查收件箱失败: {exc}", "WARN")


# ===========================================================================
# f. 尺寸自检
# ===========================================================================

def step_f_roundtrip(fbx_path: str) -> None:
    log("步骤 f1：把刚导出的 FBX 回读进 Blender，量尺寸")
    if not (fbx_path and os.path.exists(fbx_path)):
        log("  FBX 不存在，跳过", "WARN")
        return
    info = reimport_and_measure(Path(fbx_path), "RT")
    RESULT["roundtrip"] = info


def step_f_scale_probe(cube) -> None:
    """单位校准矩阵：global_scale × apply_unit_scale 四种组合，各自记录
    文件里真实写入的顶点量级 / UnitScaleFactor / 回读尺寸。"""
    import copy

    log("步骤 f2：global_scale(1.0/100.0) × apply_unit_scale(True/False) 四组合对比")
    from asset_bridge_blender import bridge_state, io_fbx

    # Blender 5.0 新增的 apply_scale_options，先看一眼它有哪些取值
    try:
        p = {q.identifier: q for q in bpy.ops.export_scene.fbx.get_rna_type().properties}
        if "apply_scale_options" in p:
            q = p["apply_scale_options"]
            log(f"  新参数 apply_scale_options 合法项={[i.identifier for i in q.enum_items]} "
                f"默认值={q.default}")
            RESULT["apply_scale_options"] = [i.identifier for i in q.enum_items]
    except Exception as exc:
        log(f"  读取 apply_scale_options 失败: {exc}", "WARN")

    if SCALE_PROBE_DIR.exists():
        shutil.rmtree(SCALE_PROBE_DIR, ignore_errors=True)
    SCALE_PROBE_DIR.mkdir(parents=True, exist_ok=True)

    base_profile = bridge_state.route_profile("blender", "unreal")
    rows = []
    for gs in (1.0, 100.0):
        for aus in (True, False):
            tag = f"gs{int(gs)}_aus{int(aus)}"
            prof = copy.deepcopy(base_profile)
            prof.setdefault("export", {})["global_scale"] = gs
            prof["export"]["apply_unit_scale"] = aus
            fp = SCALE_PROBE_DIR / f"cube_{tag}.fbx"
            try:
                io_fbx.export_selection(str(fp), prof, [cube])
            except Exception as exc:
                log(f"  {tag} 导出失败：{type(exc).__name__}: {exc}", "ERROR")
                RESULT["errors"].append(f"scale 探针 {tag} 导出失败: {exc}")
                rows.append({"tag": tag, "global_scale": gs,
                             "apply_unit_scale": aus, "error": str(exc)})
                continue
            b = os.path.getsize(fp) if fp.exists() else 0
            st = read_fbx_settings(fp)
            info = reimport_and_measure(fp, tag.upper()) if b else {"dimensions_m": []}
            max_edge = max((max(v) for v in info.get("dimensions_m", [])), default=None)
            log(f"  {tag}: 字节={b} 顶点max_abs="
                f"{(st.get('vertices') or {}).get('max_abs')} "
                f"LclScaling={st.get('model_lcl_scaling')} "
                f"有效半边长={st.get('effective_half_extent')}"
                f"({st.get('effective_unit')}) "
                f"UnitScaleFactor={(st.get('UnitScaleFactor') or {}).get('value')} "
                f"回读最大边={max_edge}")
            rows.append({
                "tag": tag, "global_scale": gs, "apply_unit_scale": aus,
                "path": str(fp), "bytes": b,
                "vertices_max_abs": (st.get("vertices") or {}).get("max_abs"),
                "vertices_count": (st.get("vertices") or {}).get("count"),
                "model_lcl_scaling": st.get("model_lcl_scaling"),
                "effective_half_extent": st.get("effective_half_extent"),
                "effective_unit": st.get("effective_unit"),
                "reimport_max_edge_m": max_edge,
                "fbx_unit_scale_factor": (st.get("UnitScaleFactor") or {}).get("value"),
                "fbx_original_unit_scale_factor":
                    (st.get("OriginalUnitScaleFactor") or {}).get("value"),
                "fbx_version": st.get("fbx_version"),
                "up_axis": (st.get("UpAxis") or {}).get("value"),
                "front_axis": (st.get("FrontAxis") or {}).get("value"),
            })
    RESULT["scale_probe"] = rows

    # --- 结论判定 ---
    dims = {}
    for r in rows:
        if r.get("reimport_max_edge_m") is not None:
            dims[(r["global_scale"], r["apply_unit_scale"])] = r["reimport_max_edge_m"]
    log(f"  回读最大边 (gs, apply_unit_scale) -> 米：{dims}")

    baseline = next((r for r in rows
                     if r.get("global_scale") == 1.0 and r.get("apply_unit_scale") is True), None)
    if baseline and baseline.get("reimport_max_edge_m") is not None:
        near1 = abs(baseline["reimport_max_edge_m"] - 1.0) < 0.02
        log(f"  => 现有档案(gs=1.0, apply_unit_scale=True) 回读 "
            f"{'≈1.0m：自洽，Blender 往返无损' if near1 else '≠1.0m：存在缩放漂移'}")
        RESULT["roundtrip_selfconsistent"] = bool(near1)

    # 单位判定：顶点量级 × 根节点缩放才是文件里真正的场景单位
    if baseline:
        v = baseline.get("vertices_max_abs")
        s = (baseline.get("model_lcl_scaling") or [None])[0]
        eff = baseline.get("effective_half_extent")
        RESULT["file_vertex_half_extent"] = v          # 原始顶点：恒为 0.5（不随 global_scale 变）
        RESULT["file_root_scale"] = s                  # 根节点缩放：global_scale=1.0 时为 100
        RESULT["file_effective_half_extent"] = eff
        RESULT["file_units"] = baseline.get("effective_unit")
        usf = baseline.get("fbx_unit_scale_factor")
        log(f"  => 1m 立方体：文件原始顶点半边长={v}（未烘焙 global_scale），"
            f"根节点 Lcl Scaling={s}（米->厘米在这里换算）")
        log(f"  => 文件有效半边长={eff}，判定文件有效单位 = {RESULT['file_units']}")
        log(f"  => 文件自述 UnitScaleFactor={usf}（FBX SDK 约定：=1 表示 cm，=100 表示 m）"
            f" —— 与有效单位判定{'一致' if RESULT['file_units'] == 'cm' else '不一致'}")

        gs100 = next((r for r in rows if r.get("global_scale") == 100.0
                      and r.get("apply_unit_scale") is True), None)
        if gs100:
            RESULT["global_scale_100_effect"] = (
                f"回读 {gs100.get('reimport_max_edge_m')}m，"
                f"根节点缩放 {gs100.get('model_lcl_scaling')}")
            log(f"  => global_scale=100.0 时：{RESULT['global_scale_100_effect']}")

    # 结论：给调用方一个可直接用的推荐值
    aus_rows = {(r.get("global_scale"), r.get("apply_unit_scale")): r for r in rows}
    if (1.0, True) in aus_rows and aus_rows[(1.0, True)].get("reimport_max_edge_m"):
        d = aus_rows[(1.0, True)]["reimport_max_edge_m"]
        RESULT["recommended_global_scale"] = 1.0 if abs(d - 1.0) < 0.02 else None
        log(f"  => 推荐 global_scale = {RESULT['recommended_global_scale']}"
            f"（依据：gs=1.0 时文件已是厘米，与 UE/SP 的 cm 世界单位对齐）")


def step_g_receive_path(fbx_path: str) -> None:
    """步骤 g：顺带冒烟测一下接收路径 io_fbx.import_fbx（里面全是 bpy 操作，
    只有真跑一遍才知道 5.0 有没有不兼容的参数）。"""
    log("步骤 g：冒烟测试接收路径 io_fbx.import_fbx()")
    if not (fbx_path and os.path.exists(fbx_path)):
        log("  FBX 不存在，跳过", "WARN")
        return
    from asset_bridge_blender import bridge_state, io_fbx

    profile = bridge_state.route_profile("unreal", "blender")
    imp = profile.get("import", {})
    log(f"  unreal>blender 导入档案 global_scale={imp.get('global_scale')}")
    try:
        before = set(bpy.data.objects)
        objs = io_fbx.import_fbx(fbx_path, profile)
        dims = [world_bbox_size(o) for o in objs if o.type == "MESH"]
        log(f"  import_fbx 返回 {[o.name for o in objs]}")
        log(f"  导入后尺寸={dims}")
        log(f"  场景对象数 {len(before)} -> {len(bpy.data.objects)}")
        log(f"  报告={io_fbx.last_import_report(objs)}")
        RESULT["receive_path"] = {
            "ok": True,
            "objects": [o.name for o in objs],
            "dimensions_m": dims,
            "profile_global_scale": imp.get("global_scale"),
        }
        if dims:
            max_edge = max(max(d) for d in dims)
            log(f"  !! 注意：源 FBX 是 1m 立方体，接收端量到 {max_edge} "
                f"（档案 global_scale={imp.get('global_scale')}）", "WARN")
    except Exception as exc:
        RESULT["errors"].append(f"import_fbx 失败: {type(exc).__name__}: {exc}")
        RESULT["receive_path"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        log(f"  import_fbx 失败：{type(exc).__name__}: {exc}", "ERROR")
        log(traceback.format_exc(), "ERROR")


# ===========================================================================
# main
# ===========================================================================

def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        step_a()
        load_addon()
        cube = step_b()
        step_c()
        audit_params(cube)
        cap = step_d()
        step_e(cap.get("tid"), cap.get("fbx"))
        step_f_roundtrip(cap.get("fbx"))
        step_f_scale_probe(cube)
        step_g_receive_path(cap.get("fbx"))
    except Exception as exc:
        RESULT["errors"].append(f"主流程异常: {type(exc).__name__}: {exc}")
        log(f"主流程异常: {type(exc).__name__}: {exc}", "ERROR")
        log(traceback.format_exc(), "ERROR")

    RESULT["server_config"] = None
    try:
        RESULT["server_config"] = http_json("GET", "/config", timeout=3.0)
    except Exception as exc:
        log(f"取 /config 失败: {exc}", "WARN")

    RAW_JSON.write_text(
        json.dumps(RESULT, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    log(f"原始数据已写入 {RAW_JSON}")


main()
