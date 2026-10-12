# -*- coding: utf-8 -*-
"""Asset Bridge :: QA 回归 —— UE 5.8.3 侧驱动脚本（commandlet）。

用法：
    UnrealEditor-Cmd.exe "E:\\CirnoWorkSpace\\AssetBridge\\unreal\\project\\AssetBridgeTest\\AssetBridgeTest.uproject"
        -run=pythonscript -script="E:\\CirnoWorkSpace\\AssetBridge\\tests\\qa_ue_case.py"
        -unattended -nosplash -NullRHI -stdout -FullStdOutLogOutput

用例由环境变量 QA_CASE 选择：
    box    -> 收件箱认领 SM_QaBox 并导入，校验 200x100x50 cm / 2 材质槽
    box2   -> 第二次收件导入（增量更新），校验无 _1 后缀且 revision=2
    rig    -> 收件导入 SK_QaRig（骨骼分支），校验生成 SkeletalMesh 且骨骼数>0
    export -> 导出 /Game/Bridge/SM_QaBox 发回 Blender（用例5 反向链路）

结果 JSON 写到 logs/qa_ue_<case>.json（QA 只读，不修改任何插件源码）。
"""
import json
import os
import sys
import traceback

import unreal

# --------------------------------------------------------------------------- 常量
PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"
CONTENT_PATH = "/Game/Bridge"

CASE = os.environ.get("QA_CASE", "box").strip().lower()
ASSET_NAME = os.environ.get("QA_ASSET", "SM_QaBox").strip()
ASSET_PATH = f"{CONTENT_PATH}/{ASSET_NAME}"

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

RESULT = {
    "case": CASE,
    "asset_name": ASSET_NAME,
    "engine_version": "",
    "steps": [],
    "errors": [],
    "checks": {},
}
_lines = []


def log(msg, tag="INFO"):
    text = f"[{tag}] {msg}"
    unreal.log("[qa] " + text)
    RESULT["steps"].append(text)
    _lines.append(text)


def err(context, exc):
    text = f"{context} -> {type(exc).__name__}: {exc}"
    log(text, "ERROR")
    try:
        log(traceback.format_exc().replace("\n", " | "), "ERROR")
    except Exception:
        pass
    RESULT["errors"].append(text)


def check(name, expected, actual, ok=None):
    passed = (expected == actual) if ok is None else bool(ok)
    RESULT["checks"][name] = {
        "expected": expected, "actual": actual, "pass": bool(passed),
    }
    log(f"CHECK {name}: {'PASS' if passed else 'FAIL'} "
        f"(expected={expected!r} actual={actual!r})", "CHECK")
    return passed


# --------------------------------------------------------------------------- 工具
def _v3(v):
    return [float(v.x), float(v.y), float(v.z)]


def measure(asset):
    """尽量量出资产的包围盒尺寸（UE 单位 = cm）。"""
    out = {"ok": False, "attempts": []}
    try:
        b = asset.get_bounds()
        ext = _v3(b.box_extent)
        out.update(ok=True, method="get_bounds()",
                   extent=ext,
                   size=[round(abs(e) * 2.0, 4) for e in ext])
        out["attempts"].append("get_bounds: OK")
        return out
    except Exception as exc:
        out["attempts"].append(f"get_bounds: {type(exc).__name__}: {exc}")
    try:
        bb = asset.get_bounding_box()
        mn, mx = _v3(bb.min), _v3(bb.max)
        out.update(ok=True, method="get_bounding_box()", min=mn, max=mx,
                   size=[round(mx[i] - mn[i], 4) for i in range(3)])
        out["attempts"].append("get_bounding_box: OK")
        return out
    except Exception as exc:
        out["attempts"].append(f"get_bounding_box: {type(exc).__name__}: {exc}")
    return out


def material_slot_count(asset):
    """静态网格用 static_materials，骨骼网格试几种属性名。"""
    info = {"attempts": []}
    count = None
    names = []
    for prop in ("static_materials", "materials", "skeletal_materials"):
        try:
            v = asset.get_editor_property(prop)
            n = len(v)
            nm = []
            for sm in v:
                try:
                    mi = sm.get_editor_property("material_interface")
                    nm.append(mi.get_name() if mi else "")
                except Exception:
                    try:
                        nm.append(str(sm.get_name()))
                    except Exception:
                        nm.append("?")
            info["attempts"].append(f"{prop}: {n}")
            if count is None or n > count:
                count, names = n, nm
            info[prop] = n
        except Exception as exc:
            info["attempts"].append(f"{prop}: {type(exc).__name__}: {exc}")
    info["count"] = count
    info["names"] = names
    return info


def _node_name(n):
    """BoneNode 在 UE 5.8 的 Python 暴露里不直接给 name 属性，逐个试。"""
    for attr in ("name", "bone_name", "get_name", "get_fname"):
        try:
            v = getattr(n, attr)
            v = v() if callable(v) else v
            if isinstance(v, str) and v:
                return v
        except Exception:
            continue
    for prop in ("name", "bone_name"):
        try:
            v = n.get_editor_property(prop)
            if isinstance(v, str) and v:
                return v
        except Exception:
            continue
    # UE 5.8 的 BoneNode 只暴露 struct 通用方法，得靠 to_dict 反查字段
    try:
        d = n.to_dict()
        if isinstance(d, dict):
            for k in ("name", "bone_name"):
                if isinstance(d.get(k), str) and d[k]:
                    return d[k]
    except Exception:
        pass
    return None


def bone_info(asset):
    """骨骼网格：拿到骨骼数量与骨骼名。

    注意：数量以 bone_tree 长度为准，名字取不到也要把数量记下来 ——
    名字只是辅助信息，取不到不代表没骨骼。
    """
    info = {"attempts": []}
    count = None
    names = []
    tree = None
    try:
        skel = asset.get_editor_property("skeleton")
        info["skeleton"] = skel.get_path_name() if skel else None
        try:
            tree = skel.get_editor_property("bone_tree")
            count = len(tree)
            info["attempts"].append(f"skeleton.bone_tree: len={count}")
        except Exception as exc:
            info["attempts"].append(f"skeleton.bone_tree: {type(exc).__name__}: {exc}")
    except Exception as exc:
        info["attempts"].append(f"get skeleton: {type(exc).__name__}: {exc}")

    if tree is not None:
        for n in tree:
            nm = _node_name(n)
            names.append(nm if nm else f"<无名#{len(names)}>")
        if names and names[0].startswith("<无名"):
            try:
                info["bonenode_dir"] = sorted(
                    p for p in dir(tree[0]) if not p.startswith("_"))[:40]
            except Exception as exc:
                info["bonenode_dir"] = f"ERR {exc}"

    if count is None:
        for prop in ("bone_tree", "ref_skeleton", "skeleton_bone_names"):
            try:
                v = asset.get_editor_property(prop)
                if isinstance(v, (list, tuple)):
                    count = len(v)
                    names = [str(_node_name(x) or x) for x in v]
                    info["attempts"].append(f"{prop}: {count}")
                    break
            except Exception as exc:
                info["attempts"].append(f"{prop}: {type(exc).__name__}: {exc}")

    info["count"] = count
    info["names"] = sorted(names)
    return info


def list_assets(root=CONTENT_PATH):
    try:
        return sorted(unreal.EditorAssetLibrary.list_assets(
            root, recursive=True, include_folder=False))
    except Exception as exc:
        err(f"list_assets({root})", exc)
        return []


# --------------------------------------------------------------------------- 导入用例
def pull_and_import(asset_name, require_kind=None):
    """从收件箱认领指定资产并导入。返回 (record, 资产对象, 导入路径列表)。"""
    from asset_bridge_unreal import importer
    from asset_bridge_unreal.client import BridgeClient

    c = BridgeClient(base_url=SERVER, app="unreal",
                     version=unreal.SystemLibrary.get_engine_version(),
                     project=PROJECT)
    sess = c.register()
    log(f"已注册 UE 会话 {sess.get('session_id')}")

    records = c.inbox(limit=30)
    log(f"unreal 收件箱 {len(records)} 条: "
        f"{[(r.get('asset_name'), r.get('status'), r.get('revision')) for r in records]}")

    target = None
    for r in records:
        if r.get("asset_name") == asset_name:
            target = r
            break
    if target is None:
        log(f"!! 收件箱里没有 {asset_name}", "ERROR")
        return None, None, []

    full = c.claim(target["id"])
    RESULT["transfer"] = {
        "id": full.get("id"),
        "asset_name": full.get("asset_name"),
        "kind": full.get("kind"),
        "revision": full.get("revision"),
        "status": full.get("status"),
        "from": full.get("from"),
        "files_abs": full.get("_files_abs"),
        "options": full.get("options"),
    }
    log(f"已认领 {full.get('id')} rev={full.get('revision')} "
        f"kind={full.get('kind')} files={full.get('_files_abs')}")

    fbx = None
    for f in (full.get("_files_abs") or []):
        if str(f).lower().endswith((".fbx", ".obj")) and os.path.isfile(f):
            fbx = str(f)
            break
    if fbx is None:
        log(f"!! 没有可用的 FBX: {full.get('_files_abs')}", "ERROR")
        return full, None, []

    import asset_bridge_unreal
    src_app = (full.get("from") or {}).get("app", "blender")
    profile = asset_bridge_unreal.route_profile(src_app)
    log(f"路由档案 = {src_app}>unreal : {json.dumps(profile, ensure_ascii=False)}")

    try:
        imported = importer.import_model(full, fbx, profile)
    except Exception as exc:
        err("import_model", exc)
        imported = {"mesh": "", "materials": [], "all": [], "others": []}
    paths = list(imported.get("all") or [])
    log(f"import_model 返回 = {paths}  网格={imported.get('mesh')} "
        f"材质={imported.get('materials')}")
    RESULT["imported_paths"] = paths
    RESULT["imported_mesh"] = imported.get("mesh") or ""
    RESULT["imported_materials"] = list(imported.get("materials") or [])

    # import_model 会把材质资产一起返回，必须挑出「名字对得上」的那个网格资产，
    # 否则会拿 M_QaBox_A 这种材质资产去量包围盒，量出来是 None。
    asset = None
    for p in (paths or []):
        obj = unreal.EditorAssetLibrary.load_asset(p)
        if obj is None:
            continue
        short = str(p).rsplit("/", 1)[-1].split(".")[0]
        if short == ASSET_NAME:
            asset = obj
            break
        if asset is None and isinstance(obj, (unreal.StaticMesh, unreal.SkeletalMesh)):
            asset = obj
    if asset is None and unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH):
        asset = unreal.EditorAssetLibrary.load_asset(ASSET_PATH)

    RESULT["client"] = c
    return full, asset, list(paths or [])


def finish(c, tid, payload):
    try:
        rec = c.finish(tid, "done", payload)
        log(f"finish -> {rec.get('status')}")
    except Exception as exc:
        err("finish", exc)


# --------------------------------------------------------------------------- 用例
def case_import_static(second_run=False):
    # QA_IMPORT=0 时只做「只读校验」：不去收件箱认领、不重新导入，
    # 直接量当前已存在的资产（用于把第一次导入的结果补记成用例2的结论）。
    if os.environ.get("QA_IMPORT", "1").strip().lower() in ("0", "false", "no"):
        log("QA_IMPORT=0：跳过导入，只校验已存在的资产")
        full = None
        asset = (unreal.EditorAssetLibrary.load_asset(ASSET_PATH)
                 if unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH) else None)
        paths = []
    else:
        full, asset, paths = pull_and_import(ASSET_NAME)
        if full is None:
            check("ue_资产存在", True, False)
            return
    revision = (full or {}).get("revision")

    exists = unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH)
    check("ue_资产存在", True, bool(exists))
    check("ue_资产路径正确", ASSET_PATH,
          asset.get_path_name().split(".")[0] if asset else None)

    is_static = isinstance(asset, unreal.StaticMesh)
    RESULT["asset_class"] = type(asset).__name__ if asset else None
    check("ue_类型为StaticMesh", True, bool(is_static))

    m = measure(asset) if asset else {"ok": False}
    RESULT["measure"] = m
    log(f"实测包围盒 = {m.get('size')} (method={m.get('method')})")
    size = m.get("size")
    expect = [200.0, 100.0, 50.0]
    size_ok = bool(size) and all(
        abs(a - b) <= 0.5 for a, b in zip(sorted(size), sorted(expect))
    )
    check("ue_包围盒_200x100x50cm", expect, size, ok=size_ok)

    mi = material_slot_count(asset) if asset else {"count": None}
    RESULT["materials"] = mi
    log(f"材质槽 = {mi.get('count')} 名称 = {mi.get('names')}")
    check("ue_材质槽数_2", 2, mi.get("count"))

    # 增量更新相关
    siblings = [p for p in list_assets() if ASSET_NAME in p.rsplit("/", 1)[-1]]
    RESULT["sibling_assets"] = siblings
    has_dup = any(p.rsplit("/", 1)[-1].startswith(ASSET_NAME + "_")
                  for p in siblings)
    RESULT["has_numbered_duplicate"] = bool(has_dup)
    log(f"Bridge 下相关资产 = {siblings}")
    check("ue_无_1后缀副本", False, bool(has_dup))

    if second_run:
        check("ue_revision递增到2", 2, revision)
        check("ue_二次导入仍有资产", True, bool(exists))

    if full and RESULT.get("client"):
        finish(RESULT.get("client"), full["id"], {
            "assets": [ASSET_PATH] if exists else [],
            "bounding_box_size": size,
            "material_slots": mi.get("count"),
            "asset_class": type(asset).__name__ if asset else None,
            "message": f"QA {CASE} 完成",
        })


def case_import_skeletal():
    if os.environ.get("QA_IMPORT", "1").strip().lower() in ("0", "false", "no"):
        log("QA_IMPORT=0：跳过导入，只校验已存在的资产")
        full = None
        asset = (unreal.EditorAssetLibrary.load_asset(ASSET_PATH)
                 if unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH) else None)
        paths = []
    else:
        full, asset, paths = pull_and_import(ASSET_NAME)
        if full is None:
            check("ue_骨骼资产认领", True, False)
            return

    exists = unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH)
    check("ue_骨骼资产存在", True, bool(exists))
    RESULT["asset_class"] = type(asset).__name__ if asset else None
    log(f"资产类型 = {RESULT['asset_class']}  路径 = "
        f"{asset.get_path_name() if asset else None}")

    is_skel = isinstance(asset, unreal.SkeletalMesh)
    check("ue_类型为SkeletalMesh", True, bool(is_skel))
    check("ue_不是StaticMesh", False, isinstance(asset, unreal.StaticMesh))

    bi = bone_info(asset) if asset else {"count": 0}
    RESULT["bones"] = bi
    log(f"骨骼数 = {bi.get('count')} 骨骼名 = {bi.get('names')}")
    check("ue_骨骼数大于0", True, (bi.get("count") or 0) > 0)

    m = measure(asset) if asset else {"ok": False}
    RESULT["measure"] = m
    log(f"骨骼网格包围盒 = {m.get('size')} (method={m.get('method')})")

    mi = material_slot_count(asset) if asset else {"count": None}
    RESULT["materials"] = mi

    RESULT["bridge_assets"] = list_assets()
    log(f"Bridge 下资产 = {RESULT['bridge_assets']}")

    if full and RESULT.get("client"):
        finish(RESULT.get("client"), full["id"], {
            "assets": [ASSET_PATH] if exists else [],
            "asset_class": type(asset).__name__ if asset else None,
            "bone_count": bi.get("count"),
            "bone_names": bi.get("names"),
            "bounding_box_size": m.get("size"),
            "errors": RESULT["errors"],
            "message": f"QA {CASE} 完成",
        })


def case_export_to_blender():
    from asset_bridge_unreal import exporter
    from asset_bridge_unreal.client import BridgeClient

    out_dir = os.path.join(LOG_DIR, "qa_ue_export")
    os.makedirs(out_dir, exist_ok=True)

    exists = unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH)
    check("ue_导出源资产存在", True, bool(exists))
    if not exists:
        return

    profile = {}
    try:
        import asset_bridge_unreal
        profile = asset_bridge_unreal._profiles().get("routes", {}).get(
            "unreal>blender", {})
    except Exception as exc:
        err("取 unreal>blender 档案", exc)
    log(f"unreal>blender 档案 = {json.dumps(profile, ensure_ascii=False)}")

    files = exporter.export_assets([ASSET_PATH], out_dir, profile)
    sizes = {f: os.path.getsize(f) for f in files if os.path.isfile(f)}
    RESULT["export"] = {"files": files, "sizes": sizes, "out_dir": out_dir}
    log(f"导出文件 = {files} 大小 = {sizes}")
    check("ue_导出FBX成功", True, len(files) > 0 and all(s > 0 for s in sizes.values()))

    if not files:
        return

    c = BridgeClient(base_url=SERVER, app="unreal",
                     version=unreal.SystemLibrary.get_engine_version(),
                     project=PROJECT)
    c.register()
    meta = {
        "asset_name": ASSET_NAME,
        "kind": "static_mesh",
        "format": "fbx",
        "to": {"app": "blender", "project": PROJECT, "content_path": ""},
        "unit": {"scene_unit": "cm", "up_axis": "Z", "forward_axis": "X",
                 "unit_scale": 1.0},
        "options": {"mode": "update"},
        "objects": [{"name": ASSET_NAME, "type": "ASSET"}],
        "note": "QA 用例5：UE -> Blender 单位闭环",
    }
    try:
        rec = c.send(meta, [__import__("pathlib").Path(f) for f in files])
        RESULT["sent"] = {
            "transfer_id": rec.get("id"), "revision": rec.get("revision"),
            "status": rec.get("status"), "files": rec.get("files"),
        }
        log(f"已发送给 blender：{RESULT['sent']}")
        check("ue_发送给blender成功", True, rec.get("status") == "queued")
    except Exception as exc:
        err("send -> blender", exc)
        check("ue_发送给blender成功", True, False)


# --------------------------------------------------------------------------- main
def main():
    try:
        RESULT["engine_version"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        err("get_engine_version", exc)
    log(f"engine = {RESULT['engine_version']}  python = {sys.version.split()[0]}")

    try:
        if CASE == "box":
            case_import_static(second_run=False)
        elif CASE == "box2":
            case_import_static(second_run=True)
        elif CASE == "rig":
            case_import_skeletal()
        elif CASE == "export":
            case_export_to_blender()
        else:
            raise ValueError(f"未知 QA_CASE={CASE}")
    except Exception as exc:
        err("main", exc)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        out = os.path.join(LOG_DIR, f"qa_ue_{CASE}.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
        log(f"结果已写入 {out}")
        with open(os.path.join(LOG_DIR, f"qa_ue_{CASE}.log"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(_lines))
    except Exception as exc:
        err("写结果文件", exc)

    log("SUMMARY " + json.dumps({
        "case": CASE,
        "checks": {k: v["pass"] for k, v in RESULT["checks"].items()},
        "errors": RESULT["errors"],
    }, ensure_ascii=False))


try:
    main()
except Exception as exc:
    err("top-level", exc)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"qa_ue_{CASE}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

unreal.log("[qa] 结束")
