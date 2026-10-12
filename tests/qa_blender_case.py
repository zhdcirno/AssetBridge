# -*- coding: utf-8 -*-
"""Asset Bridge :: QA 回归 —— Blender 5.0.1 侧驱动脚本。

用法（headless）：
    "D:\\DCC\\Blender5.0.1\\blender.exe" --background ^
        --python "E:\\CirnoWorkSpace\\AssetBridge\\tests\\qa_blender_case.py"

用例由环境变量 QA_CASE 选择：
    box   -> 建 2m x 1m x 0.5m 长方体（2 个材质槽）命名 SM_QaBox 并发送到 UE
    rig   -> 建 Armature（Root + Bone_A）+ 绑定网格，命名 SK_QaRig 发送到 UE
    recv  -> 从收件箱认领发给 blender 的资产，用插件 io_fbx.import_fbx 导入并量尺寸

结果 JSON 写到 logs/qa_blender_<case>.json（QA 只读，不修改任何插件源码）。
"""
import json
import os
import sys
import traceback
from pathlib import Path

import bpy
import mathutils

# --------------------------------------------------------------------------- 常量
ADDON_DIR = r"E:\CirnoWorkSpace\AssetBridge\blender\addon"
LOG_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\logs")
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"
CONTENT_PATH = "/Game/Bridge"

CASE = os.environ.get("QA_CASE", "box").strip().lower()
OUT_JSON = LOG_DIR / f"qa_blender_{CASE}.json"

RESULT = {
    "case": CASE,
    "blender_version": bpy.app.version_string,
    "python": sys.version.split()[0],
    "steps": [],
    "errors": [],
    "checks": {},          # 每个断言项：期望 vs 实测
}

_log_lines = []


def log(msg, tag="INFO"):
    line = f"[{tag}] {msg}"
    print(line, flush=True)
    RESULT["steps"].append(line)
    _log_lines.append(line)


def err(context, exc):
    text = f"{context} -> {type(exc).__name__}: {exc}"
    log(text, "ERROR")
    try:
        log(traceback.format_exc().replace("\n", " | "), "ERROR")
    except Exception:
        pass
    RESULT["errors"].append(text)


def check(name, expected, actual, ok=None):
    """记录一条断言。ok 为 None 时自动用 == 比较。"""
    passed = (expected == actual) if ok is None else bool(ok)
    RESULT["checks"][name] = {
        "expected": expected, "actual": actual, "pass": bool(passed),
    }
    log(f"CHECK {name}: {'PASS' if passed else 'FAIL'} "
        f"(expected={expected!r} actual={actual!r})", "CHECK")
    return passed


# --------------------------------------------------------------------------- 几何工具
def world_bbox_size(ob):
    """世界坐标 AABB 尺寸（米）。"""
    bpy.context.view_layer.update()
    corners = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    return [round(max(c[i] for c in corners) - min(c[i] for c in corners), 6)
            for i in range(3)]


# --------------------------------------------------------------------------- 插件加载
def load_addon():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sys.path.insert(0, ADDON_DIR)
    try:
        bpy.ops.preferences.addon_enable(module="asset_bridge_blender")
        RESULT["addon_mode"] = "addon_enable"
    except Exception as exc:
        log(f"addon_enable 失败：{exc}", "WARN")
        import asset_bridge_blender
        asset_bridge_blender.register()
        RESULT["addon_mode"] = "direct_register"

    prefs = bpy.context.preferences.addons["asset_bridge_blender"].preferences
    prefs.server_url = SERVER
    prefs.project = PROJECT
    prefs.target_app = "unreal"
    prefs.content_path = CONTENT_PATH
    prefs.send_mode = "update"
    prefs.use_prefix = False        # 名字里已带 SM_ / SK_ 前缀
    prefs.import_materials = True
    prefs.import_textures = True
    prefs.combine_meshes = False
    prefs.import_animations = False
    prefs.auto_receive = False      # 关掉后台轮询，避免和本脚本抢收件箱
    prefs.direct_write = True
    log(f"prefs: server={prefs.server_url} project={prefs.project} "
        f"target={prefs.target_app} mode={prefs.send_mode} prefix={prefs.use_prefix}")
    return prefs


# --------------------------------------------------------------------------- 用例素材
def build_box():
    """2m x 1m x 0.5m 长方体 + 2 个材质槽。"""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    box = bpy.context.view_layer.objects.active
    box.name = "SM_QaBox"
    # 直接改顶点，保证对象 scale 仍是 1（避免非均匀缩放污染换算判定）
    for v in box.data.vertices:
        v.co = (v.co.x * 2.0, v.co.y * 1.0, v.co.z * 0.5)
    box.data.update()

    mat_a = bpy.data.materials.get("M_QaBox_A") or bpy.data.materials.new("M_QaBox_A")
    mat_b = bpy.data.materials.get("M_QaBox_B") or bpy.data.materials.new("M_QaBox_B")
    box.data.materials.append(mat_a)
    box.data.materials.append(mat_b)
    # 两个槽都必须有面用到，否则 UE 导入时会把空槽丢掉
    for i, poly in enumerate(box.data.polygons):
        poly.material_index = 0 if i < 3 else 1

    if not box.data.uv_layers:
        box.data.uv_layers.new(name="UVMap")

    dims = world_bbox_size(box)
    slots = [ms.material.name if ms.material else None for ms in box.material_slots]
    log(f"SM_QaBox 尺寸={dims} 材质槽={slots}")
    RESULT["source"] = {
        "name": box.name, "dimensions_m": dims,
        "material_slots": slots,
        "verts": len(box.data.vertices), "polys": len(box.data.polygons),
    }
    check("blender_源尺寸_2x1x0.5", [2.0, 1.0, 0.5], dims,
          ok=all(abs(a - b) < 1e-3 for a, b in zip(dims, [2.0, 1.0, 0.5])))
    check("blender_材质槽数_2", 2, len(box.material_slots))
    return [box]


def build_rig():
    """Armature（Root -> Bone_A，2 根骨）+ 绑定了顶点组的网格。"""
    bpy.ops.object.armature_add(location=(0.0, 0.0, -1.0))
    arm = bpy.context.view_layer.objects.active
    arm.name = "SK_QaRig_Arm"

    bpy.ops.object.mode_set(mode="EDIT")
    ebs = arm.data.edit_bones
    root = ebs[0]
    root.name = "Root"
    root.head = (0.0, 0.0, -1.0)
    root.tail = (0.0, 0.0, 0.0)
    bone_a = ebs.new("Bone_A")
    bone_a.head = (0.0, 0.0, 0.0)
    bone_a.tail = (0.0, 0.0, 1.0)
    bone_a.parent = root
    bone_names = sorted(b.name for b in ebs)
    bpy.ops.object.mode_set(mode="OBJECT")
    log(f"骨架骨骼 = {bone_names}")

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    mesh = bpy.context.view_layer.objects.active
    mesh.name = "SK_QaRig_Mesh"
    for v in mesh.data.vertices:
        v.co = (v.co.x, v.co.y, v.co.z * 2.0)   # 高 2m（-1 .. +1）
    mesh.data.update()

    # 绑定：优先自动权重，失败则手工建顶点组 + Armature 修改器
    bound = "ARMATURE_AUTO"
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as exc:
        err("parent_set(ARMATURE_AUTO)", exc)
        bound = "MANUAL"

    if len(mesh.vertex_groups) == 0 or not any(
            m.type == "ARMATURE" for m in mesh.modifiers):
        bound = "MANUAL"
        for bn in ("Root", "Bone_A"):
            if bn not in mesh.vertex_groups:
                mesh.vertex_groups.new(name=bn)
        for v in mesh.data.vertices:
            target = "Bone_A" if v.co.z > 0 else "Root"
            mesh.vertex_groups[target].add([v.index], 1.0, "REPLACE")
        if not any(m.type == "ARMATURE" for m in mesh.modifiers):
            mod = mesh.modifiers.new("Armature", "ARMATURE")
            mod.object = arm
        mesh.parent = arm

    RESULT["source"] = {
        "armature": arm.name, "mesh": mesh.name,
        "blender_bones": bone_names,
        "vertex_groups": [g.name for g in mesh.vertex_groups],
        "modifiers": [(m.type, m.name) for m in mesh.modifiers],
        "bind_method": bound,
        "dimensions_m": world_bbox_size(mesh),
    }
    log(f"骨架绑定方式={bound} 顶点组={RESULT['source']['vertex_groups']} "
        f"修改器={RESULT['source']['modifiers']}")
    check("blender_骨骼数_2", 2, len(bone_names))
    check("blender_顶点组非空", True, len(mesh.vertex_groups) > 0)
    return [mesh, arm]


# --------------------------------------------------------------------------- 发送
def send_objects(objects, asset_name, note=""):
    """走真实 operator 路径 bpy.ops.bridge.send_selection()，并抓下 transfer id。"""
    from asset_bridge_blender import bridge_state, io_fbx, operators
    from asset_bridge_blender.bridge_client import BridgeClient

    bpy.ops.object.select_all(action="DESELECT")
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    log(f"选中 = {[o.name for o in bpy.context.selected_objects]}")

    bpy.context.scene.bridge_asset_name = asset_name
    bpy.context.scene.bridge_note = note or f"QA {CASE} 回归"

    captured = {"tid": None, "publish": None, "fbx": None, "reports": []}

    _pub = BridgeClient.publish

    def publish(self, tid):
        captured["tid"] = tid
        rec = _pub(self, tid)
        captured["publish"] = rec
        return rec

    BridgeClient.publish = publish

    _exp = io_fbx.export_selection

    def export_selection(filepath, profile, objs):
        captured["fbx"] = filepath
        return _exp(filepath, profile, objs)

    io_fbx.export_selection = export_selection

    def report(self, type_set, message):
        captured["reports"].append(f"{sorted(str(t) for t in type_set)}: {message}")
        return bpy.types.Operator.report(self, type_set, message)

    operators.BRIDGE_OT_send_selection.report = report

    ret = None
    try:
        r = bpy.ops.bridge.send_selection()
        ret = sorted(str(x) for x in r) if r else None
    except Exception as exc:
        err("send_selection", exc)
    finally:
        BridgeClient.publish = _pub
        io_fbx.export_selection = _exp
        try:
            del operators.BRIDGE_OT_send_selection.report
        except AttributeError:
            pass

    log(f"send_selection 返回={ret}")
    for r in captured["reports"]:
        log(f"  operator 报告: {r}")

    rec = captured["publish"] or {}
    RESULT["send"] = {
        "return": ret,
        "transfer_id": captured["tid"],
        "fbx_path": captured["fbx"],
        "asset_name": rec.get("asset_name"),
        "kind": rec.get("kind"),
        "revision": rec.get("revision"),
        "status": rec.get("status"),
        "files": rec.get("files"),
        "to": rec.get("to"),
        "unit": rec.get("unit"),
        "objects": rec.get("objects"),
    }
    fbx = captured["fbx"]
    if fbx and os.path.exists(fbx):
        RESULT["send"]["fbx_bytes"] = os.path.getsize(fbx)
    log(f"transfer={captured['tid']} kind={rec.get('kind')} "
        f"revision={rec.get('revision')} fbx={fbx} "
        f"bytes={RESULT['send'].get('fbx_bytes')}")

    check("blender_发送返回FINISHED", True, bool(ret and "FINISHED" in ret))
    check("blender_资产名正确", asset_name, rec.get("asset_name"))
    return captured["tid"]


# --------------------------------------------------------------------------- 接收
def do_recv():
    """认领发给 blender 的资产并用插件 import_fbx 导入，量尺寸。"""
    from asset_bridge_blender import bridge_state, io_fbx
    from asset_bridge_blender.bridge_client import BridgeClient

    client = BridgeClient(base_url=SERVER, app="blender",
                          version=bpy.app.version_string, project=PROJECT)
    client.register()
    records = client.inbox(limit=20)
    log(f"blender 收件箱 {len(records)} 条: "
        f"{[(r.get('asset_name'), r.get('revision')) for r in records]}")

    target = None
    for r in records:
        if (r.get("from") or {}).get("app") == "unreal":
            target = r
            break
    if target is None and records:
        target = records[0]
    if target is None:
        RESULT["recv"] = {"ok": False, "error": "收件箱为空"}
        check("blender_收到UE资产", True, False)
        return

    full = client.claim(target["id"])
    files = [f for f in (full.get("_files_abs") or [])]
    log(f"已认领 {full['id']} rev={full.get('revision')} files_abs={files}")

    fbx = None
    for f in files:
        if str(f).lower().endswith((".fbx", ".obj")) and os.path.isfile(f):
            fbx = str(f)
            break
    if fbx is None:
        RESULT["recv"] = {"ok": False, "error": f"没有可导入文件: {files}"}
        check("blender_收到UE资产", True, False)
        return

    profile = bridge_state.route_profile((full.get("from") or {}).get("app", "unreal"),
                                         "blender")
    imp = profile.get("import", {})
    log(f"unreal>blender 档案 import={imp}")

    before = set(bpy.data.objects)
    imported = io_fbx.import_fbx(fbx, profile)
    meshes = [o for o in imported if o.type == "MESH"]
    dims = [world_bbox_size(o) for o in meshes]

    RESULT["recv"] = {
        "ok": True,
        "transfer_id": full["id"],
        "asset_name": full.get("asset_name"),
        "revision": full.get("revision"),
        "profile_import": imp,
        "objects": [o.name for o in imported],
        "dimensions_m": dims,
        "report": io_fbx.last_import_report(imported),
        "objects_before": len(before), "objects_after": len(bpy.data.objects),
    }
    log(f"导入 {len(imported)} 个对象，网格尺寸(米) = {dims}")

    # 期望：UE 侧 200x100x50 cm -> 回到 Blender 应为 2x1x0.5 m
    expect = [2.0, 1.0, 0.5]
    got = None
    if dims:
        got = [max(max(d[i] for d in dims) - min(d[i] for d in dims), 0.0)
               for i in range(3)]
        got = [round(g, 4) for g in got]
    # 单网格时直接用它自己的尺寸
    if len(dims) == 1:
        got = [round(v, 4) for v in dims[0]]
    RESULT["recv"]["measured_m"] = got
    log(f"实测回到 Blender 的尺寸 = {got}（期望 {expect}）")
    check("blender_回程尺寸_2x1x0.5米", expect, got,
          ok=bool(got) and all(abs(a - b) <= 0.02 * max(1.0, b)
                               for a, b in zip(sorted(got), sorted(expect))))
    check("blender_回程非200米量级", True, bool(got) and max(got) < 20.0)
    check("blender_回程非0.02米量级", True, bool(got) and max(got) > 0.2)

    try:
        client.finish(full["id"], "done", {
            "objects": [o.name for o in imported],
            "message": io_fbx.last_import_report(imported),
            "dimensions_m": got,
        })
    except Exception as exc:
        err("finish", exc)


# --------------------------------------------------------------------------- main
def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        load_addon()
        if CASE == "box":
            objs = build_box()
            send_objects(objs, "SM_QaBox", "QA 用例2：静态网格完整链路")
        elif CASE == "rig":
            objs = build_rig()
            send_objects(objs, "SK_QaRig", "QA 用例4：骨骼网格分支补测")
        elif CASE == "recv":
            do_recv()
        else:
            raise ValueError(f"未知 QA_CASE={CASE}")
    except Exception as exc:
        err("main", exc)

    try:
        OUT_JSON.write_text(
            json.dumps(RESULT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log(f"结果已写入 {OUT_JSON}")
    except Exception as exc:
        print(f"写结果失败: {exc}", flush=True)

    # 原始日志
    try:
        (LOG_DIR / f"qa_blender_{CASE}.log").write_text(
            "\n".join(_log_lines), encoding="utf-8")
    except Exception:
        pass


main()
