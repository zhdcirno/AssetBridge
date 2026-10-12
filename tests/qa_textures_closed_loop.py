# -*- coding: utf-8 -*-
"""Asset Bridge :: 贴图闭环 —— Blender 5.0.1 侧驱动（headless）。

用途：造一个 **1m 立方体（2 个材质槽）** 命名 SM_CalibCube 发到 UE 的
`/Game/Bridge`，之后再让同名的 3 张贴图进 `/Game/Bridge/Textures`，
用来验证「网格 + 贴图能在同一个 /Game/Bridge 内容树里共存、互不覆盖」。

用法（headless）：
    "D:\\DCC\\Blender5.0.1\\blender.exe" --background ^
        --python "E:\\CirnoWorkSpace\\AssetBridge\\tests\\qa_textures_closed_loop.py"

结果写入 logs/qa_textures_closed_loop.json，并打印 transfer_id。
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
RESULT_JSON = LOG_DIR / "qa_textures_closed_loop.json"
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"
CONTENT_PATH = "/Game/Bridge"
ASSET_NAME = "SM_CalibCube"

RESULT = {
    "blender_version": bpy.app.version_string,
    "python": sys.version.split()[0],
    "asset_name": ASSET_NAME,
    "steps": [],
    "errors": [],
    "checks": {},
}
_LINES = []


def log(msg, tag="INFO"):
    line = f"[{tag}] {msg}"
    print(line, flush=True)
    RESULT["steps"].append(line)
    _LINES.append(line)


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
    RESULT["checks"][name] = {"expected": expected, "actual": actual,
                              "pass": bool(passed)}
    log(f"CHECK {name}: {'PASS' if passed else 'FAIL'} "
        f"(expected={expected!r} actual={actual!r})", "CHECK")
    return passed


# --------------------------------------------------------------------------- 工具
def world_bbox_size(ob):
    bpy.context.view_layer.update()
    corners = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    return [round(max(c[i] for c in corners) - min(c[i] for c in corners), 6)
            for i in range(3)]


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
    prefs.use_prefix = False
    prefs.import_materials = True
    prefs.import_textures = True
    prefs.combine_meshes = False
    prefs.import_animations = False
    prefs.auto_receive = False      # 关掉后台轮询，别和本脚本抢收件箱
    prefs.direct_write = True
    log(f"prefs: server={prefs.server_url} project={prefs.project} "
        f"target={prefs.target_app} content={prefs.content_path}")
    return prefs


def build_cube():
    """1m x 1m x 1m 立方体 + 2 个材质槽，命名 SM_CalibCube。"""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    cube = bpy.context.view_layer.objects.active
    cube.name = ASSET_NAME
    cube.data.name = ASSET_NAME + "_Mesh"

    mat_a = bpy.data.materials.get("M_CalibCube_A") or bpy.data.materials.new("M_CalibCube_A")
    mat_b = bpy.data.materials.get("M_CalibCube_B") or bpy.data.materials.new("M_CalibCube_B")
    cube.data.materials.append(mat_a)
    cube.data.materials.append(mat_b)
    # 两个槽都要有面用到，否则 UE 导入会把空槽丢掉
    for i, poly in enumerate(cube.data.polygons):
        poly.material_index = 0 if i < 3 else 1

    if not cube.data.uv_layers:
        cube.data.uv_layers.new(name="UVMap")

    dims = world_bbox_size(cube)
    slots = [ms.material.name if ms.material else None for ms in cube.material_slots]
    RESULT["source"] = {"name": cube.name, "dimensions_m": dims,
                        "material_slots": slots,
                        "verts": len(cube.data.vertices),
                        "polys": len(cube.data.polygons)}
    log(f"SM_CalibCube 尺寸={dims} 材质槽={slots}")
    check("blender_源尺寸_1x1x1", [1.0, 1.0, 1.0], dims,
          ok=all(abs(a - b) < 1e-3 for a, b in zip(dims, [1.0, 1.0, 1.0])))
    check("blender_材质槽数_2", 2, len(cube.material_slots))
    return [cube]


def send_objects(objects, asset_name, note=""):
    """走真实 operator 路径 bpy.ops.bridge.send_selection()，抓下 transfer id。"""
    from asset_bridge_blender import io_fbx, operators
    from asset_bridge_blender.bridge_client import BridgeClient

    bpy.ops.object.select_all(action="DESELECT")
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    bpy.context.scene.bridge_asset_name = asset_name
    bpy.context.scene.bridge_note = note or "贴图闭环：网格"

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
    }
    fbx = captured["fbx"]
    if fbx and os.path.exists(fbx):
        RESULT["send"]["fbx_bytes"] = os.path.getsize(fbx)
    log(f"transfer={captured['tid']} kind={rec.get('kind')} "
        f"revision={rec.get('revision')} fbx={fbx}")
    check("blender_发送返回FINISHED", True, bool(ret and "FINISHED" in ret))
    check("blender_资产名正确", asset_name, rec.get("asset_name"))


def main():
    load_addon()
    objects = build_cube()
    send_objects(objects, ASSET_NAME, note="贴图闭环：1m 立方体 -> /Game/Bridge")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULT_JSON, "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
    with open(LOG_DIR / "qa_textures_closed_loop.log", "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LINES))
    log(f"结果已写入 {RESULT_JSON}")
    log("SUMMARY " + json.dumps({
        "transfer_id": RESULT.get("send", {}).get("transfer_id"),
        "checks": {k: v["pass"] for k, v in RESULT["checks"].items()},
        "errors": RESULT["errors"],
    }, ensure_ascii=False))


try:
    main()
except Exception as exc:
    err("main", exc)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass
