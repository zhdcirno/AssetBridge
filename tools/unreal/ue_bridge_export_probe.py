# -*- coding: utf-8 -*-
"""Asset Bridge :: UE 5.8 发送端（导出）+ 贴图回传路径冒烟测试。

主自检见 ue_bridge_selftest.py，本脚本只补两块主自检没覆盖的代码路径：
    1. exporter.export_assets()   —— UE -> Blender/Maya 的回程
    2. importer.import_textures() —— Substance Painter -> UE 的贴图回传

同样用 commandlet 跑，结果写到 logs/ue_export_probe.json。
"""
import json
import os
import sys
import traceback

import unreal

PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
PROBE_DIR = os.path.join(LOG_DIR, "_ue_probe")
RESULT_JSON = os.path.join(LOG_DIR, "ue_export_probe.json")
MESH_PATH = "/Game/Bridge/SM_CalibCube"
RT_NAME = "SM_CalibCube_RoundTrip"
TEST_PNG = os.path.join(PROBE_DIR, "T_CalibTest_BaseColor.png")

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

R = {"engine_version": "", "export": {}, "roundtrip": {}, "textures": {},
     "api": {}, "errors": [], "log": []}


def log(msg):
    text = str(msg)
    unreal.log("[export-probe] " + text)
    R["log"].append(text)


def err(ctx, exc):
    text = "%s -> %s: %s" % (ctx, type(exc).__name__, exc)
    log("!! " + text)
    log(traceback.format_exc().replace("\n", " | "))
    R["errors"].append(text)


def measure(mesh):
    """与 selftest 保持一致：优先 StaticMesh.get_bounds()。"""
    try:
        b = mesh.get_bounds()
        e = b.box_extent
        return {"method": "StaticMesh.get_bounds()",
                "size": [abs(e.x) * 2.0, abs(e.y) * 2.0, abs(e.z) * 2.0]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    try:
        R["engine_version"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        err("get_engine_version", exc)
    log("engine = %s" % R["engine_version"])

    # ---------------- API 探测
    for name in ("FbxExportOption", "AssetExportTask", "Exporter",
                 "EditorUtilityLibrary", "TextureFactory"):
        R["api"][name] = hasattr(unreal, name)
    try:
        opts = unreal.FbxExportOption()
        R["api"]["FbxExportOption.props"] = sorted(
            p for p in dir(opts) if not p.startswith("_"))[:60]
        missing = []
        for k, v in (("ascii", False), ("collision", False),
                     ("export_morph_targets", True), ("export_materials", True),
                     ("export_texture", True), ("vertex_color", True),
                     ("level_of_detail", False)):
            try:
                opts.set_editor_property(k, v)
            except Exception:
                missing.append(k)
        R["api"]["FbxExportOption.missing"] = missing
        R["api"]["FbxExportOption.set_props"] = True
    except Exception as exc:
        err("FbxExportOption 探测", exc)
        R["api"]["FbxExportOption_error"] = "%s: %s" % (type(exc).__name__, exc)
    R["api"]["Exporter.run_asset_export_tasks"] = hasattr(
        unreal.Exporter, "run_asset_export_tasks")
    try:
        R["api"]["get_selected_assets"] = hasattr(unreal.EditorUtilityLibrary,
                                                  "get_selected_assets")
    except Exception as exc:
        R["api"]["get_selected_assets"] = "ERR %s" % exc
    try:
        tf = unreal.TextureFactory()
        tf.set_editor_property("no_alpha", True)
        R["api"]["TextureFactory.no_alpha"] = True
    except Exception as exc:
        err("TextureFactory 探测", exc)
        R["api"]["TextureFactory_error"] = "%s: %s" % (type(exc).__name__, exc)
    log("api = %s" % json.dumps({k: v for k, v in R["api"].items()
                                 if not isinstance(v, list)}, ensure_ascii=False))

    # ---------------- 1. 导出
    from asset_bridge_unreal import exporter, importer

    os.makedirs(PROBE_DIR, exist_ok=True)
    out_dir = os.path.join(PROBE_DIR, "out")
    try:
        if not unreal.EditorAssetLibrary.does_asset_exist(MESH_PATH):
            log("!! %s 不存在，跳过导出" % MESH_PATH)
        else:
            files = exporter.export_assets([MESH_PATH], out_dir, {})
            R["export"]["files"] = files
            R["export"]["sizes"] = {f: os.path.getsize(f) for f in files
                                    if os.path.isfile(f)}
            log("导出文件 = %s" % files)
            log("文件大小 = %s" % R["export"]["sizes"])
    except Exception as exc:
        err("export_assets", exc)

    # ---------------- 2. 往返：把导出的 FBX 再导入回来量尺寸（期望仍是 100）
    fbx_out = os.path.join(out_dir, "SM_CalibCube.fbx")
    if os.path.isfile(fbx_out):
        try:
            record = {
                "asset_name": RT_NAME,
                "kind": "static_mesh",
                "to": {"app": "unreal", "content_path": "/Game/Bridge"},
                "options": {"mode": "update", "import_materials": False},
            }
            profile = {"import": {"convert_scene": True, "convert_scene_unit": True}}
            imported = importer.import_model(record, fbx_out, profile)
            paths = list(imported.get("all") or [])
            R["roundtrip"]["paths"] = paths
            R["roundtrip"]["mesh"] = imported.get("mesh") or ""
            # 优先用结构化结果里已经挑好的网格，拿不到再退回遍历
            mesh_path = imported.get("mesh") or ""
            for p in ([mesh_path] if mesh_path else paths):
                obj = unreal.EditorAssetLibrary.load_asset(p)
                if isinstance(obj, unreal.StaticMesh):
                    R["roundtrip"]["size"] = measure(obj)
                    break
            log("往返导入 = %s，网格 = %s，实测 = %s"
                % (paths, mesh_path, R["roundtrip"].get("size")))
        except Exception as exc:
            err("往返导入", exc)
        try:
            unreal.EditorAssetLibrary.delete_asset("/Game/Bridge/" + RT_NAME)
        except Exception:
            pass

    # ---------------- 3. 贴图回传路径
    if os.path.isfile(TEST_PNG):
        try:
            record = {
                "asset_name": "SM_CalibCube",
                "to": {"app": "unreal", "content_path": "/Game/Bridge"},
            }
            paths = importer.import_textures(record, [TEST_PNG])
            R["textures"]["paths"] = paths
            R["textures"]["exists"] = [unreal.EditorAssetLibrary.does_asset_exist(p)
                                       for p in paths]
            log("贴图导入 = %s exists=%s" % (paths, R["textures"]["exists"]))
        except Exception as exc:
            err("import_textures", exc)
    else:
        log("!! 测试贴图不存在，跳过：%s" % TEST_PNG)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(R, fh, ensure_ascii=False, indent=2)
        log("结果已写入 %s" % RESULT_JSON)
    except Exception as exc:
        err("写结果文件", exc)

    log("SUMMARY " + json.dumps({
        "engine_version": R["engine_version"],
        "export_files": R["export"].get("files"),
        "roundtrip_size": R["roundtrip"].get("size"),
        "textures": R["textures"].get("paths"),
        "errors": R["errors"],
    }, ensure_ascii=False))


try:
    main()
except Exception as exc:
    err("main", exc)
    try:
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(R, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

log("[export-probe] 结束")
