# -*- coding: utf-8 -*-
"""Asset Bridge :: UE 5.8 接收端实机自检。

用 commandlet 跑：
    UnrealEditor-Cmd.exe "<project>.uproject" -run=pythonscript -script="<本文件>" ^
        -unattended -nosplash -NullRHI -stdout -FullStdOutLogOutput

验证顺序：
    a. 引擎版本
    b. 关键 Python API 可用性
    c. 走真实收件流程（register -> inbox -> claim）并调用适配器 import_model()
    d. 单位校准：convert_scene_unit = True / False 各导一次，量实际包围盒
    e. 材质槽 / 材质资产
    f. 资产是否落到 /Game/Bridge/SM_CalibCube
    g. 增量更新（再导一次，确认覆盖而非 SM_CalibCube_1）
"""
import json
import os
import sys
import traceback

import unreal

# --------------------------------------------------------------------------- 常量
PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
RESULT_JSON = os.path.join(LOG_DIR, "ue_test_result.json")
FBX_PATH = r"E:\CirnoWorkSpace\AssetBridge\runtime\payloads\083b36b8913f4d7a96e226bef454b228\SM_CalibCube.fbx"
TRANSFER_ID = "083b36b8913f4d7a96e226bef454b228"
CONTENT_PATH = "/Game/Bridge"
ASSET_PATH = "/Game/Bridge/SM_CalibCube"
PROBE_NAME = "SM_CalibCube_CSU_False"   # convert_scene_unit=False 的对照资产

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

RESULT = {
    "engine_version": "",
    "project": "",
    "import_success": False,
    "asset_path": "",
    "asset_exists": False,
    "asset_file_on_disk": "",
    "api": {},
    "measurements": {},
    "convert_scene_unit": {},
    "materials": {},
    "incremental_update": {},
    "service": {},
    "levels": {},
    "errors": [],
    "log": [],
}


def log(msg):
    """同时写 UE 日志和结果数组（便于落盘后复盘）。"""
    text = str(msg)
    unreal.log("[selftest] " + text)
    RESULT["log"].append(text)


def sec(title):
    log("")
    log("=============== %s ===============" % title)


def err(context, exc):
    text = "%s -> %s: %s" % (context, type(exc).__name__, exc)
    log("!! " + text)
    try:
        log(traceback.format_exc().replace("\n", " | "))
    except Exception:
        pass
    RESULT["errors"].append(text)


# --------------------------------------------------------------------------- 测量
def _vec3(v):
    return [float(v.x), float(v.y), float(v.z)]


def measure(mesh):
    """量 StaticMesh 的实际尺寸（UE 单位 = cm）。依次尝试三种 API。"""
    out = {"ok": False, "attempts": []}

    # 方式 1：StaticMesh.get_bounds() -> BoxSphereBounds
    try:
        b = mesh.get_bounds()
        extent = _vec3(b.box_extent)
        out.update({
            "ok": True,
            "method": "StaticMesh.get_bounds()",
            "extent": extent,
            "origin": _vec3(b.origin),
            "sphere_radius": float(b.sphere_radius),
            "size": [abs(extent[0]) * 2.0, abs(extent[1]) * 2.0, abs(extent[2]) * 2.0],
        })
        out["attempts"].append("get_bounds: OK")
        return out
    except Exception as exc:
        out["attempts"].append("get_bounds: %s %s" % (type(exc).__name__, exc))

    # 方式 2：StaticMesh.get_bounding_box() -> Box
    try:
        bb = mesh.get_bounding_box()
        mn, mx = _vec3(bb.min), _vec3(bb.max)
        out.update({
            "ok": True,
            "method": "StaticMesh.get_bounding_box()",
            "min": mn,
            "max": mx,
            "size": [mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]],
        })
        out["attempts"].append("get_bounding_box: OK")
        return out
    except Exception as exc:
        out["attempts"].append("get_bounding_box: %s %s" % (type(exc).__name__, exc))

    # 方式 3：StaticMeshEditorSubsystem / EditorStaticMeshLibrary 的 LOD 顶点
    try:
        sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        verts = sub.get_number_verts(mesh, 0)
        out["attempts"].append("LOD vertex read: not-implemented (%s)" % verts)
    except Exception as exc:
        out["attempts"].append("LOD vertex read: %s %s" % (type(exc).__name__, exc))

    # 方式 4：extended_bounds（默认常为 0，仅作兜底记录）
    try:
        eb = mesh.get_editor_property("extended_bounds")
        out["extended_bounds"] = {
            "min": _vec3(eb.min), "max": _vec3(eb.max), "is_valid": bool(eb.is_valid)
        }
        out["attempts"].append("extended_bounds: read")
    except Exception as exc:
        out["attempts"].append("extended_bounds: %s %s" % (type(exc).__name__, exc))

    return out


def describe(mesh):
    """资产的一些辅助信息，便于判断导入是否完整。"""
    info = {}
    for prop in ("static_materials", "light_map_resolution", "nanite_settings"):
        try:
            v = mesh.get_editor_property(prop)
            if prop == "static_materials":
                info["static_materials_count"] = len(v)
                names = []
                for sm in v:
                    try:
                        names.append(sm.get_editor_property("material_interface").get_name()
                                     if sm.get_editor_property("material_interface") else "")
                    except Exception:
                        names.append("?")
                info["static_material_names"] = names
            else:
                info[prop] = str(v)[:200]
        except Exception as exc:
            info[prop] = "ERR %s" % exc
    try:
        info["num_tris"] = unreal.EditorStaticMeshLibrary.get_number_of_triangles(mesh, 0)
    except Exception:
        pass
    return info


def list_assets(root):
    try:
        return sorted(unreal.EditorAssetLibrary.list_assets(root, recursive=True, include_folder=False))
    except Exception as exc:
        err("list_assets(%s)" % root, exc)
        return []


def import_once(record, fbx, convert_scene_unit, asset_name):
    """用适配器导入一次，返回 (资产路径列表, 异常)。"""
    from asset_bridge_unreal import importer

    rec = json.loads(json.dumps(record))          # 深拷贝，避免污染原始记录
    rec["asset_name"] = asset_name
    rec.setdefault("options", {})
    rec["options"]["mode"] = "update"
    profile = {
        "import": {
            "convert_scene": True,
            "convert_scene_unit": convert_scene_unit,
            "force_front_x_axis": False,
            "import_uniform_scale": 1.0,
            "combine_meshes": False,
        }
    }
    imported = importer.import_model(rec, fbx, profile)
    # import_model 现在返回结构化结果（mesh / materials / all），
    # 这里只关心「有没有导入出东西」，所以取 all
    return list(imported.get("all") or [])


# --------------------------------------------------------------------------- 主流程
def main():
    # ---------------- a. 引擎版本
    sec("a. 引擎版本")
    try:
        RESULT["engine_version"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        err("get_engine_version", exc)
    # 注意：UE 5.8 的 SystemLibrary 没有 get_project_name()，别踩
    try:
        RESULT["project"] = unreal.SystemLibrary.get_game_name()
    except Exception as exc:
        err("get_game_name", exc)
        try:
            RESULT["project"] = unreal.Paths.get_project_file_path()
        except Exception:
            pass
    log("engine_version = %s" % RESULT["engine_version"])
    log("project = %s" % RESULT["project"])
    log("python = %s" % sys.version)

    # ---------------- b. API 可用性
    sec("b. 关键 API 可用性")
    api = {}
    for name in ("AssetImportTask", "FbxImportUI", "EditorAssetLibrary",
                 "EditorStaticMeshLibrary", "EditorActorSubsystem",
                 "StaticMeshEditorSubsystem", "AssetToolsHelpers",
                 "FbxStaticMeshImportData", "LevelEditorSubsystem"):
        api[name] = hasattr(unreal, name)
    try:
        ui = unreal.FbxImportUI()
        data = ui.get_editor_property("static_mesh_import_data")
        api["FbxStaticMeshImportData.convert_scene_unit"] = True
        data.set_editor_property("convert_scene_unit", True)
        data.set_editor_property("convert_scene_unit", False)
        api["static_mesh_import_data.props"] = sorted(
            p for p in dir(data) if "convert" in p or "combine" in p or "collision" in p
        )
        api["FbxImportUI.props"] = sorted(
            p for p in dir(ui) if "import" in p or "mesh" in p or "automated" in p
        )[:40]
    except Exception as exc:
        err("FbxImportUI 探测", exc)
        api["FbxImportUI_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", FBX_PATH)
        task.set_editor_property("destination_path", CONTENT_PATH)
        task.set_editor_property("destination_name", "probe")
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("automated", True)
        task.set_editor_property("save", True)
        api["AssetImportTask.set_editor_property"] = True
    except Exception as exc:
        err("AssetImportTask 探测", exc)
        api["AssetImportTask_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        api["EditorActorSubsystem.instance"] = bool(unreal.get_editor_subsystem(unreal.EditorActorSubsystem))
    except Exception as exc:
        api["EditorActorSubsystem.instance"] = "ERR %s" % exc
    RESULT["api"] = api
    log("api = %s" % json.dumps({k: v for k, v in api.items() if not isinstance(v, list)},
                                 ensure_ascii=False))
    log("FbxImportUI props = %s" % api.get("FbxImportUI.props"))
    log("static_mesh_import_data props = %s" % api.get("static_mesh_import_data.props"))

    # ---------------- c. 真实收件流程 + 导入
    sec("c. 服务收件 + 适配器导入")
    record = None
    try:
        from asset_bridge_unreal.client import BridgeClient, BridgeError
        c = BridgeClient(base_url="http://127.0.0.1:27631", app="unreal",
                         version=RESULT["engine_version"], project="AssetBridgeTest")
        sess = c.register()
        RESULT["service"]["session_id"] = sess.get("session_id")
        log("已注册会话 %s" % sess.get("session_id"))
        inbox = c.inbox(limit=10)
        RESULT["service"]["inbox_count"] = len(inbox)
        log("收件箱 %d 条" % len(inbox))
        target = None
        for r in inbox:
            if r.get("id") == TRANSFER_ID:
                target = r
                break
        if target is None and inbox:
            target = inbox[0]
        if target is not None:
            record = c.claim(target["id"])
            RESULT["service"]["claimed"] = target["id"]
            log("已认领 %s，_files_abs = %s" % (target["id"], record.get("_files_abs")))
    except Exception as exc:
        err("收件流程", exc)
        RESULT["service"]["error"] = "%s: %s" % (type(exc).__name__, exc)

    if record is None:
        # 服务不可用时的兜底记录（保证校准部分仍可跑）
        log("!! 未拿到服务记录，使用本地兜底 record")
        record = {
            "id": TRANSFER_ID,
            "asset_name": "SM_CalibCube",
            "kind": "static_mesh",
            "format": "fbx",
            "from": {"app": "blender", "version": "5.0.1"},
            "to": {"app": "unreal", "project": "AssetBridgeTest",
                   "content_path": CONTENT_PATH},
            "unit": {"scene_unit": "m", "up_axis": "Z", "forward_axis": "-Y",
                     "unit_scale": 1.0},
            "options": {"mode": "update", "import_materials": True,
                        "import_textures": True, "combine_meshes": False,
                        "import_animations": False},
            "_files_abs": [FBX_PATH],
        }

    files_abs = [f for f in (record.get("_files_abs") or [])]
    fbx = FBX_PATH
    for f in files_abs:
        if str(f).lower().endswith(".fbx") and os.path.isfile(f):
            fbx = str(f)
            break
    RESULT["fbx_used"] = fbx
    RESULT["fbx_exists"] = os.path.isfile(fbx)
    log("使用 FBX = %s (exists=%s)" % (fbx, RESULT["fbx_exists"]))

    # ---- c/d-1：convert_scene_unit = True（当前档案默认值）
    paths_true = []
    try:
        paths_true = import_once(record, fbx, True, "SM_CalibCube")
        log("CSU=True 导入返回: %s" % paths_true)
        RESULT["convert_scene_unit"]["true_paths"] = paths_true
    except Exception as exc:
        err("import(convert_scene_unit=True)", exc)
        RESULT["convert_scene_unit"]["true_error"] = "%s: %s" % (type(exc).__name__, exc)

    mesh_true = None
    for p in paths_true:
        obj = unreal.EditorAssetLibrary.load_asset(p)
        if isinstance(obj, unreal.StaticMesh):
            mesh_true = obj
            RESULT["asset_path"] = p
            break
    if mesh_true is None:
        obj = unreal.EditorAssetLibrary.load_asset(ASSET_PATH)
        if isinstance(obj, unreal.StaticMesh):
            mesh_true = obj
            RESULT["asset_path"] = ASSET_PATH

    # ---------------- d. 单位校准
    sec("d. 单位校准（UE 单位 = cm，1m 立方体期望 100）")
    if mesh_true is not None:
        m = measure(mesh_true)
        RESULT["measurements"]["mesh_true"] = m
        RESULT["convert_scene_unit"]["true"] = m
        RESULT["import_success"] = True
        log("CSU=True  实测 = %s  (method=%s)" % (m.get("size"), m.get("method")))
    else:
        log("!! CSU=True 没拿到 StaticMesh")

    # ---- d-2：convert_scene_unit = False 对照
    try:
        paths_false = import_once(record, fbx, False, PROBE_NAME)
        log("CSU=False 导入返回: %s" % paths_false)
        mesh_false = None
        for p in paths_false:
            obj = unreal.EditorAssetLibrary.load_asset(p)
            if isinstance(obj, unreal.StaticMesh):
                mesh_false = obj
                break
        if mesh_false is None:
            obj = unreal.EditorAssetLibrary.load_asset(CONTENT_PATH + "/" + PROBE_NAME)
            if isinstance(obj, unreal.StaticMesh):
                mesh_false = obj
        if mesh_false is not None:
            m2 = measure(mesh_false)
            RESULT["convert_scene_unit"]["false"] = m2
            RESULT["measurements"]["mesh_false"] = m2
            log("CSU=False 实测 = %s  (method=%s)" % (m2.get("size"), m2.get("method")))
        else:
            log("!! CSU=False 没拿到 StaticMesh")
    except Exception as exc:
        err("import(convert_scene_unit=False)", exc)
        RESULT["convert_scene_unit"]["false_error"] = "%s: %s" % (type(exc).__name__, exc)

    # ---------------- e. 材质槽
    sec("e. 材质槽 / 材质资产")
    if mesh_true is not None:
        try:
            info = describe(mesh_true)
            RESULT["materials"]["mesh_info"] = info
            log("static_materials = %s" % info.get("static_materials_count"))
            log("material names = %s" % info.get("static_material_names"))
            log("mesh info = %s" % json.dumps(info, ensure_ascii=False))
        except Exception as exc:
            err("describe(mesh)", exc)
    assets_after = list_assets(CONTENT_PATH)
    RESULT["materials"]["assets_under_bridge"] = assets_after
    mats = [p for p in assets_after
            if "/Material" in p or p.rsplit("/", 1)[-1].startswith("M_")]
    RESULT["materials"]["material_assets"] = mats
    log("Bridge 下资产 = %s" % assets_after)
    log("其中材质资产 = %s" % mats)

    # ---------------- f. 资产落盘
    sec("f. 资产落盘确认")
    try:
        exists = unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH)
        RESULT["asset_exists"] = bool(exists)
        log("does_asset_exist(%s) = %s" % (ASSET_PATH, exists))
    except Exception as exc:
        err("does_asset_exist", exc)
    try:
        proj_dir = unreal.Paths.project_dir()
        uasset = os.path.join(str(proj_dir).replace("/", "\\").lstrip("\\"),
                              "Content", "Bridge", "SM_CalibCube.uasset")
        if not os.path.isfile(uasset):
            uasset = os.path.join(str(unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_dir())), "Content", "Bridge", "SM_CalibCube.uasset")
        RESULT["asset_file_on_disk"] = uasset
        RESULT["asset_file_exists"] = os.path.isfile(uasset)
        log("磁盘文件 %s exists=%s" % (uasset, os.path.isfile(uasset)))
    except Exception as exc:
        err("磁盘路径检查", exc)

    # ---------------- g. 增量更新
    sec("g. 增量更新（再导一次，应覆盖而非生成 _1）")
    try:
        before = set(list_assets(CONTENT_PATH))
        paths2 = import_once(record, fbx, True, "SM_CalibCube")
        after = list_assets(CONTENT_PATH)
        new_assets = sorted(set(after) - before)
        RESULT["incremental_update"] = {
            "paths": paths2,
            "new_assets": new_assets,
            "has_suffix_1": any(p.endswith("SM_CalibCube_1") for p in after),
            "assets_after": after,
        }
        log("二次导入返回 = %s" % paths2)
        log("新增资产 = %s" % new_assets)
        log("是否出现 SM_CalibCube_1 = %s" % RESULT["incremental_update"]["has_suffix_1"])
    except Exception as exc:
        err("增量更新", exc)
        RESULT["incremental_update"]["error"] = "%s: %s" % (type(exc).__name__, exc)

    # ---------------- 额外：关卡摆放（EditorActorSubsystem 兼容性）
    sec("h. 关卡摆放（EditorActorSubsystem 兼容性，附加项）")
    try:
        from asset_bridge_unreal import importer as _imp
        level_path = CONTENT_PATH + "/Levels/TestLevel"
        level_ok = None
        try:
            les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
            if unreal.EditorAssetLibrary.does_asset_exist(level_path):
                level_ok = les.load_level(level_path)   # 已存在就打开，别重复 new
                RESULT["levels"]["load_level"] = bool(level_ok)
            else:
                level_ok = les.new_level(level_path)
                RESULT["levels"]["new_level"] = bool(level_ok)
        except Exception as exc:
            RESULT["levels"]["level_error"] = "%s: %s" % (type(exc).__name__, exc)
            err("关卡准备", exc)
        if RESULT["asset_path"]:
            placed = _imp.spawn_into_level([RESULT["asset_path"]])
            RESULT["levels"]["placed"] = placed
            log("摆放 actor 数 = %s" % placed)
            # 交叉验证：真去关卡里数一下 StaticMeshActor，防止适配器虚报
            # 注意 UE 5.8：EditorLevelLibrary 已废弃，优先用 EditorActorSubsystem
            try:
                sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
                actors = sub.get_all_level_actors()
            except Exception:
                actors = unreal.EditorLevelLibrary.get_all_level_actors()
            try:
                sm_actors = [a for a in actors
                             if isinstance(a, unreal.StaticMeshActor)]
                RESULT["levels"]["actors_in_level"] = len(actors)
                RESULT["levels"]["static_mesh_actors"] = len(sm_actors)
                log("关卡内 actor 总数 = %d，StaticMeshActor = %d"
                    % (len(actors), len(sm_actors)))
            except Exception as exc:
                err("统计关卡 actor", exc)
    except Exception as exc:
        err("关卡摆放", exc)

    # ---------------- 清理对照资产
    try:
        probe = CONTENT_PATH + "/" + PROBE_NAME
        if unreal.EditorAssetLibrary.does_asset_exist(probe):
            unreal.EditorAssetLibrary.delete_asset(probe)
            log("已删除对照资产 %s" % probe)
    except Exception as exc:
        err("删除对照资产", exc)

    # ---------------- 服务回执
    sec("i. 服务回执")
    try:
        from asset_bridge_unreal.client import BridgeClient
        c = BridgeClient(base_url="http://127.0.0.1:27631", app="unreal",
                         version=RESULT["engine_version"], project="AssetBridgeTest")
        c.register()
        size_true = (RESULT["convert_scene_unit"].get("true") or {}).get("size")
        size_false = (RESULT["convert_scene_unit"].get("false") or {}).get("size")
        receipt = c.finish(TRANSFER_ID, "done", {
            "assets": [RESULT["asset_path"]] if RESULT["asset_path"] else [],
            "bounding_box_size": size_true,
            "bounding_box_size_csu_false": size_false,
            "material_slots": (RESULT["materials"].get("mesh_info") or {}).get("static_materials_count"),
            "message": "UE %s 实机导入完成" % RESULT["engine_version"],
        })
        RESULT["service"]["finish_status"] = (receipt or {}).get("status")
        log("finish -> %s" % RESULT["service"]["finish_status"])
    except Exception as exc:
        err("finish", exc)

    # ---------------- 落盘
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2)
        log("结果已写入 %s" % RESULT_JSON)
    except Exception as exc:
        err("写结果文件", exc)

    sec("SUMMARY")
    log(json.dumps({
        "engine_version": RESULT["engine_version"],
        "import_success": RESULT["import_success"],
        "asset_path": RESULT["asset_path"],
        "asset_exists": RESULT["asset_exists"],
        "size_csu_true": (RESULT["convert_scene_unit"].get("true") or {}).get("size"),
        "size_csu_false": (RESULT["convert_scene_unit"].get("false") or {}).get("size"),
        "material_slots": (RESULT["materials"].get("mesh_info") or {}).get("static_materials_count"),
        "incremental_ok": not RESULT["incremental_update"].get("has_suffix_1"),
        "errors": RESULT["errors"],
    }, ensure_ascii=False))


try:
    main()
except Exception as exc:  # 兜底：任何异常都要把已有结果写出来
    err("main", exc)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

log("[selftest] 结束")
