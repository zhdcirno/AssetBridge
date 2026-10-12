# -*- coding: utf-8 -*-
"""
Maya 2023（批量模式）下的 Asset Bridge 实机自检。

由 maya2023_batch.mel 通过 runpy 拉起，运行在真正的 Maya 进程里，
所以 maya.cmds 是完整可用的（不需要 maya.standalone）。
"""
from __future__ import print_function

import json
import os
import sys
import traceback

import maya.cmds as cmds

ROOT = r"E:\CirnoWorkSpace\AssetBridge"
PLUGIN_DIR = os.path.join(ROOT, "maya", "plugin")
LOG_DIR = os.path.join(ROOT, "logs")
WORK_DIR = os.path.join(LOG_DIR, "_maya2023_probe")
OUT_JSON = os.path.join(LOG_DIR, "maya2023_test_result.json")

# Blender 产出的 1m 校准立方体（FBX 内有效单位 = cm）
CALIB_FBX = os.path.join(
    ROOT, "runtime", "payloads", "083b36b8913f4d7a96e226bef454b228", "SM_CalibCube.fbx"
)

TARGET_APP = "maya2023_probe"   # 一次性目标，不污染 UE 收件箱
PROJECT = "MayaQaTest"
CUBE_CM = 100.0                 # 1m 立方体在 Maya(cm) 里的期望边长

STEPS = []
RESULT = {
    "app": "maya",
    "maya_version": None,
    "python_version": sys.version.split()[0],
    "steps": STEPS,
    "export": {},
    "send": {},
    "import_calib": {},
    "conversion": {},
    "issues": [],
}


def step(name, ok, detail=""):
    STEPS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print("[%s] %s %s" % ("OK" if ok else "FAIL", name, detail), flush=True)


def bbox(node):
    """量世界包围盒尺寸（cm）。"""
    bb = cmds.exactWorldBoundingBox(node)
    return [round(bb[3] - bb[0], 4), round(bb[4] - bb[1], 4), round(bb[5] - bb[2], 4)]


def main():
    if not os.path.isdir(WORK_DIR):
        os.makedirs(WORK_DIR)

    # 1 版本
    RESULT["maya_version"] = cmds.about(version=True)
    step("maya 版本", True, cmds.about(version=True) + " / py" + RESULT["python_version"])

    # 2 FBX 插件
    try:
        if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
            cmds.loadPlugin("fbxmaya")
        ok = cmds.pluginInfo("fbxmaya", q=True, loaded=True)
    except Exception as exc:
        ok = False
        RESULT["issues"].append("fbxmaya 加载失败: %s" % exc)
    step("fbxmaya 插件", ok)

    # 3 适配器可导入
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)
    try:
        import asset_bridge_maya as bridge
        step("导入 asset_bridge_maya", True)
    except Exception as exc:
        RESULT["issues"].append("适配器导入失败: %s" % exc)
        step("导入 asset_bridge_maya", False, exc)
        return

    # 4 建立方体（Maya 侧 100cm）
    cmds.file(new=True, force=True)
    cube = cmds.polyCube(w=CUBE_CM, h=CUBE_CM, d=CUBE_CM, name="MayaCube")[0]
    cmds.select(cube)
    size = bbox(cube)
    step("建立方体并选中", size == [CUBE_CM] * 3, str(size))

    # 5 导出 FBX
    fbx_out = os.path.join(WORK_DIR, "MayaCube.fbx")
    try:
        bridge.export_selection(fbx_out, bridge.route_profile("maya"))
        nbytes = os.path.getsize(fbx_out) if os.path.exists(fbx_out) else 0
        RESULT["export"] = {"path": fbx_out, "bytes": nbytes}
        step("export_selection 导出 FBX", nbytes > 0, "%d 字节" % nbytes)
    except Exception as exc:
        RESULT["issues"].append("导出失败: %s" % exc)
        step("export_selection 导出 FBX", False, traceback.format_exc())

    # 6 发送（发往一次性目标，不干扰 UE）
    try:
        bridge.CONFIG["project"] = PROJECT
        bridge.register()
        rec = bridge._req("POST", "/transfers", {
            "asset_name": "SM_Maya2023Cube",
            "kind": "static_mesh",
            "format": "fbx",
            "to": {"app": TARGET_APP, "project": PROJECT, "content_path": ""},
            "unit": {"scene_unit": "cm", "up_axis": "Y", "forward_axis": "Z", "unit_scale": 1.0},
            "options": {"mode": "update"},
            "objects": [{"name": cube, "type": "MESH"}],
        })["transfer"]
        tid = rec["id"]
        pd = bridge.payload_dir()
        if pd:
            dst = os.path.join(pd, tid)
            if not os.path.isdir(dst):
                os.makedirs(dst)
            import shutil
            shutil.copy2(fbx_out, os.path.join(dst, "SM_Maya2023Cube.fbx"))
        else:
            bridge._req("PUT", "/transfers/%s/files/SM_Maya2023Cube.fbx" % tid,
                        data=open(fbx_out, "rb").read(), timeout=30.0)
        rec = bridge._req("POST", "/transfers/%s/publish" % tid, {})["transfer"]
        RESULT["send"] = {"transfer_id": tid, "status": rec["status"],
                          "revision": rec["revision"], "target": TARGET_APP}
        step("send 到中转服务", rec["status"] == "queued",
             "rev=%s -> %s" % (rec["revision"], TARGET_APP))
    except Exception as exc:
        RESULT["issues"].append("发送失败: %s" % exc)
        step("send 到中转服务", False, traceback.format_exc())

    # 7 导入 Blender 的 1m 立方体，校验 cm/m 换算
    if os.path.exists(CALIB_FBX):
        try:
            cmds.file(new=True, force=True)
            newnodes = bridge.import_fbx(CALIB_FBX, bridge.route_profile("blender"))
            if newnodes:
                imported = newnodes[0]
                size = bbox(imported)
                RESULT["import_calib"] = {"fbx": CALIB_FBX, "nodes": newnodes, "size_cm": size}
                ok = all(abs(v - 100.0) < 1.0 for v in size)
                RESULT["conversion"] = {
                    "expect_cm": 100.0, "actual_cm": size,
                    "conclusion": "1m 立方体 -> Maya 100cm，换算正确（scale_factor=1.0）" if ok
                                  else "换算不符，需要调 blender>maya 的 scale_factor",
                }
                step("导入 1m 立方体并量尺寸", ok, "实测 %s cm" % size)
            else:
                step("导入 1m 立方体并量尺寸", False, "没拿到新节点")
        except Exception as exc:
            RESULT["issues"].append("导入失败: %s" % exc)
            step("导入 1m 立方体并量尺寸", False, traceback.format_exc())
    else:
        step("导入 1m 立方体并量尺寸", False, "校准 FBX 不存在: %s" % CALIB_FBX)

    passed = sum(1 for s in STEPS if s["ok"])
    RESULT["summary"] = {"passed": passed, "total": len(STEPS)}
    print("\n=== %d/%d 通过 ===" % (passed, len(STEPS)), flush=True)

    with open(OUT_JSON, "w") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2)
    print("结果已写入 " + OUT_JSON, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("FATAL", flush=True)
