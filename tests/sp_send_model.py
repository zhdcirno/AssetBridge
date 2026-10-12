# -*- coding: utf-8 -*-
"""Blender -> Substance Painter 实机发送脚本（headless）

用途：验证「Blender 发模型 → SP 自动收下并开项目」这条链路。
SP 插件是 QML/JS 的、没有外部命令行接口，但它每 1000ms 轮询一次收件箱，
所以只要把模型发进 SP 的收件箱，SP 会**自己**去认领、开项目、回执——
整个过程不需要人工点按钮。

用法：
    "D:\\DCC\\Blender5.0.1\\blender.exe" --background --python tests/sp_send_model.py

发完在外部用 tests/sp_receive_check.py 查状态即可。
"""
import json
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDON_DIR = os.path.join(ROOT, "blender", "addon")
SERVER = "http://127.0.0.1:27631"

ASSET_NAME = "SM_SpCube"
PROJECT = "SPTest"

RESULT = {}


def log(msg):
    print("[sp_send] " + str(msg), flush=True)


def world_bbox_size(obj):
    import mathutils

    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return [round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6),
            round(max(zs) - min(zs), 6)]


def main():
    log("Blender " + bpy.app.version_string)
    RESULT["blender"] = bpy.app.version_string

    bpy.ops.wm.read_factory_settings(use_empty=True)
    if ADDON_DIR not in sys.path:
        sys.path.insert(0, ADDON_DIR)
    try:
        bpy.ops.preferences.addon_enable(module="asset_bridge_blender")
    except Exception as exc:
        log("addon_enable 失败（改用直接 register）：{0}".format(exc))
        import asset_bridge_blender
        asset_bridge_blender.register()

    prefs = bpy.context.preferences.addons["asset_bridge_blender"].preferences
    prefs.server_url = SERVER
    prefs.target_app = "substance_painter"
    prefs.project = PROJECT
    prefs.content_path = ""          # SP 不需要 UE 的 content_path
    prefs.send_mode = "update"
    prefs.use_prefix = False         # 名字已经在场景属性里写全了
    log("target_app={0} project={1}".format(prefs.target_app, prefs.project))

    # 1m 立方体 + 两个材质槽 + UV（SP 开项目需要 UV 才能画）
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    cube = bpy.context.view_layer.objects.active
    cube.name = "SpCube"
    for name in ("M_SpA", "M_SpB"):
        mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        cube.data.materials.append(mat)
    if not cube.data.uv_layers:
        cube.data.uv_layers.new(name="UVMap")
    bpy.context.view_layer.update()
    log("尺寸 m = {0}  材质槽 = {1}  UV = {2}".format(
        world_bbox_size(cube),
        [ms.material.name for ms in cube.material_slots],
        [uv.name for uv in cube.data.uv_layers]))
    RESULT["cube_size_m"] = world_bbox_size(cube)
    RESULT["materials"] = [ms.material.name for ms in cube.material_slots]

    bpy.ops.object.select_all(action="DESELECT")
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube

    scene = bpy.context.scene
    # 注意：bridge_asset_name 是注册在 Scene 上的 RNA 属性，
    # 必须用属性赋值；用 scene["bridge_asset_name"] = ... 写的是 ID 自定义属性，
    # operator 里 getattr 读不到（这个坑之前踩过）。
    scene.bridge_asset_name = ASSET_NAME
    log("asset_name = {0!r}（getattr 回读 {1!r}）".format(
        ASSET_NAME, getattr(scene, "bridge_asset_name", "<读不到>")))

    try:
        res = bpy.ops.bridge.send_selection()
        log("send_selection 返回 {0}".format(res))
        RESULT["operator_result"] = list(res)
    except Exception as exc:
        log("send_selection 抛异常: {0}: {1}".format(type(exc).__name__, exc))
        RESULT["operator_error"] = "{0}: {1}".format(type(exc).__name__, exc)

    # 查这条 transfer 的 id，交给外部脚本盯状态
    try:
        sys.path.insert(0, ADDON_DIR)
        from asset_bridge_blender import bridge_client

        client = bridge_client.BridgeClient(SERVER, project=PROJECT)
        hist = client.history(limit=20)
        mine = [t for t in hist if t.get("asset_name") == ASSET_NAME]
        if mine:
            RESULT["transfer_id"] = mine[0]["id"]
            RESULT["transfer_status"] = mine[0]["status"]
            log("transfer {0} 状态 {1}".format(mine[0]["id"], mine[0]["status"]))
    except Exception as exc:
        log("查记录失败（不影响发送结论）: {0}".format(exc))

    out = os.path.join(ROOT, "logs", "sp_send_result.json")
    if not os.path.isdir(os.path.dirname(out)):
        os.makedirs(os.path.dirname(out))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2)
    log("结果写入 {0}".format(out))


main()
