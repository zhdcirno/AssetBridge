#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 commandPort 远程驱动**已经打开的** Maya，跑 Asset Bridge 实机自检。

为什么走这条路
--------------------------------------------------------------------------
Maya 2023 在这台机器上 mayapy / mayabatch 都起不来（standalone.pyd
`DLL load failed`、mayabatch 退出码 0xC0000142，清 PYTHONPATH、绕沙箱、
补 MAYA_LOCATION 全无效）——自动化进程跑在没有桌面会话的环境里，Maya 的
GUI 相关库初始化不了。交互式 Maya 本身完全正常，所以「GUI Maya +
commandPort」是唯一可行的自动化通道，这也正是生产环境该用的接法
（外部工具不必和 Maya 同进程）。

两个工程细节（都是踩过坑才这么写的）
--------------------------------------------------------------------------
1. **写文件 + 短命令 exec**：早期用 ~10KB 单行 base64 直接 exec，会被
   Maya commandPort 的接收缓冲区截断，导致解释器卡在「未完成的语句」上、
   之后所有命令都不再执行。改成把源码写成文件、只发一条很短的
   `exec(compile(open(file, encoding="utf-8").read(),"p","exec"))`，
   彻底绕开长度限制（中文由 utf-8 读取兜底）。
2. **结果走文件回传**：commandPort 的 echoOutput 只回显「最后一条表达式
   的结果」，exec 里面 print 的东西不保证回来。所以 Maya 侧把结构化结果
   直接写 JSON 文件，外部脚本轮询文件。中文也不会在路上被吃掉。

场景安全
--------------------------------------------------------------------------
**全程非破坏性**：不调用 `cmds.file(new=True)`（那会丢掉用户正在做的活）。
测试用的立方体是「新建 → 量 → 删」，导入的资产也是「导入 → 量 → 删」。
所以对着任何一个打开的 Maya 场景跑都不会有损失。

用法
--------------------------------------------------------------------------
    python tests/maya_commandport_verify.py                 # 默认 127.0.0.1:7001
    python tests/maya_commandport_verify.py --port 7002
    python tests/maya_commandport_verify.py --diag-only     # 只取环境诊断
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
WORK_DIR = LOG_DIR / "_maya_cmdport"
OUT_JSON = LOG_DIR / "maya_commandport_result.json"
REMOTE_JSON = WORK_DIR / "remote_result.json"
SELFCHECK_SRC = WORK_DIR / "maya_selfcheck_source.py"

# 在 Maya 里执行的自检代码。用 r-string 保持原样，交给 Maya 的解释器跑。
MAYA_CODE = r'''
import json, os, sys, traceback
import maya.cmds as cmds

ROOT = r"E:\CirnoWorkSpace\AssetBridge"
PLUGIN_DIR = os.path.join(ROOT, "maya", "plugin")
WORK_DIR = os.path.join(ROOT, "logs", "_maya_cmdport")
RESULT_PATH = os.path.join(WORK_DIR, "remote_result.json")
CALIB_FBX = os.path.join(ROOT, "runtime", "payloads",
                         "083b36b8913f4d7a96e226bef454b228", "SM_CalibCube.fbx")
TARGET_APP = "maya_cmdport_probe"
PROJECT = "MayaQaTest"
CAMERAS = ("persp", "top", "front", "side")

R = {"steps": [], "issues": [], "diagnostics": {}, "skipped": []}

def step(name, ok, detail=""):
    R["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)})

def skip(name, why):
    R["skipped"].append({"name": name, "why": str(why)})

def bbox(n):
    bb = cmds.exactWorldBoundingBox(n)
    return [round(bb[3] - bb[0], 4), round(bb[4] - bb[1], 4), round(bb[5] - bb[2], 4)]

def dump():
    try:
        if not os.path.isdir(WORK_DIR):
            os.makedirs(WORK_DIR)
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            json.dump(R, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

try:
    # ---------------- 环境诊断（顺便回答「userSetup 到底该装哪」）----------
    try:
        d = R["diagnostics"]
        d["maya_version"] = cmds.about(version=True)
        d["maya_api"] = cmds.about(api=True)
        d["python"] = sys.version.split()[0]
        d["user_app_dir"] = cmds.internalVar(userAppDir=True)
        d["user_script_dir"] = cmds.internalVar(userScriptDir=True)
        d["user_pref_dir"] = cmds.internalVar(userPrefDir=True)
        sp = os.environ.get("MAYA_SCRIPT_PATH", "")
        entries = []
        for p in sp.split(os.pathsep):
            if not p:
                continue
            us = os.path.join(p, "userSetup.py")
            entries.append({
                "path": p,
                "exists": os.path.isdir(p),
                "has_userSetup": os.path.isfile(us),
            })
        d["maya_script_path"] = entries
        # 用户自己的脚本目录里到底有没有 userSetup（决定我们装哪）
        d["vaccine_loaded"] = "vaccine" in sys.modules
        d["adapter_preloaded"] = "asset_bridge_maya" in sys.modules
        d["scene"] = cmds.file(query=True, sceneName=True) or "(未命名)"
        d["scene_modified"] = bool(cmds.file(query=True, modified=True))
        d["assemblies"] = cmds.ls(assemblies=True) or []
    except Exception as exc:
        R["issues"].append("诊断失败: %r" % (exc,))

    step("Maya 版本可读", bool(R.get("diagnostics", {}).get("maya_version")),
         R.get("diagnostics", {}).get("maya_version"))
    dump()  # 阶段性落盘：即使后面 FBX/网络步骤卡住，诊断信息也已留存

    # ---------------- 适配器加载 --------------------------------------
    injected = []
    try:
        if PLUGIN_DIR not in sys.path:
            sys.path.insert(0, PLUGIN_DIR)
        import asset_bridge_maya as bridge
        step("导入 asset_bridge_maya", True, bridge.__file__)
        step("适配器已注册会话", bool(bridge._session_id), bridge._session_id or "未注册")
    except Exception as exc:
        step("导入 asset_bridge_maya", False, traceback.format_exc())
        dump()
        raise SystemExit(0)

    # ---------------- fbxmaya ------------------------------------------
    try:
        if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
            cmds.loadPlugin("fbxmaya")
        step("fbxmaya 插件已载入", cmds.pluginInfo("fbxmaya", query=True, loaded=True))
    except Exception as exc:
        step("fbxmaya 插件已载入", False, exc)

    # ---------------- 导出：新建 100cm 立方体（非破坏性）----------------
    cube = None
    fbx_out = os.path.join(WORK_DIR, "BridgeCube.fbx")
    try:
        if not os.path.isdir(WORK_DIR):
            os.makedirs(WORK_DIR)
        cube = cmds.polyCube(w=100, h=100, d=100, name="BridgeCube_tmp")[0]
        cmds.select(cube)
        size = bbox(cube)
        step("建 100cm 立方体并量到 100x100x100", size == [100.0, 100.0, 100.0], str(size))
    except Exception as exc:
        step("建 100cm 立方体并量到 100x100x100", False, traceback.format_exc())

    if cube:
        try:
            bridge.export_selection(fbx_out, bridge.route_profile("maya"))
            n = os.path.getsize(fbx_out) if os.path.exists(fbx_out) else 0
            R["export"] = {"path": fbx_out, "bytes": n}
            step("导出选中为 FBX", n > 0, "%d 字节" % n)
        except Exception as exc:
            step("导出选中为 FBX", False, traceback.format_exc())
        dump()  # 阶段性落盘：导出结果已记录

        # 发送到一次性目标 app，绝不污染 unreal / blender 的收件箱
        try:
            bridge.CONFIG["project"] = PROJECT
            rec = bridge._req("POST", "/transfers", {
                "asset_name": "SM_MayaCube", "kind": "static_mesh", "format": "fbx",
                "to": {"app": TARGET_APP, "project": PROJECT, "content_path": ""},
                "unit": {"scene_unit": "cm", "up_axis": "Y", "forward_axis": "Z",
                         "unit_scale": 1.0},
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
                shutil.copy2(fbx_out, os.path.join(dst, "SM_MayaCube.fbx"))
            else:
                bridge._req("PUT", "/transfers/%s/files/SM_MayaCube.fbx" % tid,
                            data=open(fbx_out, "rb").read(), timeout=30.0)
            rec = bridge._req("POST", "/transfers/%s/publish" % tid, {})["transfer"]
            R["send"] = {"transfer_id": tid, "status": rec["status"],
                         "revision": rec["revision"]}
            step("发送到中转服务", rec["status"] == "queued", "rev=%s" % rec["revision"])
        except Exception as exc:
            step("发送到中转服务", False, traceback.format_exc())

        # 清理测试立方体（还原用户场景）
        try:
            cmds.delete(cube)
            step("清理测试立方体", not cmds.ls(cube), "已删除 %s" % cube)
        except Exception as exc:
            R["issues"].append("清理立方体失败: %r" % (exc,))

    # ---------------- 导入：Blender 的 1m 立方体 -> 期望 100cm -----------
    if not os.path.exists(CALIB_FBX):
        skip("导入 1m 立方体 -> 100cm", "校准 FBX 不存在: %s" % CALIB_FBX)
    else:
        before = set(cmds.ls(assemblies=True) or [])
        try:
            nodes = bridge.import_fbx(CALIB_FBX, bridge.route_profile("blender"))
            new_nodes = sorted(set(cmds.ls(assemblies=True) or []) - before)
            if new_nodes:
                size = bbox(new_nodes[0])
                ok = all(abs(v - 100.0) < 1.0 for v in size)
                R["conversion"] = {"expect_cm": 100.0, "actual_cm": size, "ok": ok,
                                   "imported": new_nodes}
                step("导入 1m 立方体 -> 100cm", ok, str(size))
            else:
                step("导入 1m 立方体 -> 100cm", False, "没有新增节点，import_fbx 返回 %r" % (nodes,))
        except Exception as exc:
            step("导入 1m 立方体 -> 100cm", False, traceback.format_exc())
        finally:
            # 还原场景：把导入进来的节点删掉
            try:
                leftovers = sorted(set(cmds.ls(assemblies=True) or []) - before)
                if leftovers:
                    cmds.delete(leftovers)
                step("清理导入的测试资产", not (set(cmds.ls(assemblies=True) or []) - before),
                     "删除 %s" % leftovers)
            except Exception as exc:
                R["issues"].append("清理导入资产失败: %r" % (exc,))

except SystemExit:
    raise
except Exception:
    R["issues"].append(traceback.format_exc())
finally:
    R["passed"] = sum(1 for s in R["steps"] if s["ok"])
    R["total"] = len(R["steps"])
    R["done"] = True
    dump()
print("SELFCHECK_DONE")
'''


def write_selfcheck_src(code: str) -> Path:
    """把自检源码写成文件（utf-8）。Maya 侧直接读文件 exec，绕开长度限制。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    SELFCHECK_SRC.write_text(code, encoding="utf-8")
    return SELFCHECK_SRC


def push(src_path: str, host: str, port: int, drain: float = 3.0) -> str:
    """投递一条**极短的启动器命令**：把自检源码放进 Maya 的**后台线程**跑，
    启动器本身立刻 ``print("LAUNCHED")`` 返回，socket 干净关闭。

    为什么不能直接在前台 exec 长任务
    ----------------------------------------------------------------------
    之前把「建方体→FBX 导出→网络收发→删除」这种耗时操作直接放在 commandPort
    主线程里跑，外部脚本在任务还没跑完时就关掉了 socket，Maya 会因此把端口弄进
    「半执行」坏状态——之后所有命令空回显 / 10053 中止（前面两次就是这么卡的）。
    改成后台线程后，启动器立即返回、socket 随即干净关闭，自检在后台跑完再把结果
    写文件，commandPort 主线程不再被长任务占用，彻底规避卡死。
    """
    launcher = (
        'import threading\n'
        'def _ab_run():\n'
        '    try:\n'
        '        exec(compile(open(r"%s", encoding="utf-8").read(), "p", "exec"))\n'
        '    except Exception as _ab_e:\n'
        '        try:\n'
        '            import json as _j\n'
        '            with open(r"%s", "w", encoding="utf-8") as _fh:\n'
        '                _j.dump({"crash": repr(_ab_e)}, _fh, ensure_ascii=False)\n'
        '        except Exception: pass\n'
        'threading.Thread(target=_ab_run, daemon=True).start()\n'
        'print("LAUNCHED")\n'
    ) % (src_path, str(REMOTE_JSON))
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(drain)
    buf = b""
    try:
        sock.sendall(launcher.encode("ascii"))
        deadline = time.time() + drain
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    return buf.decode("utf-8", "ignore")


def main() -> int:
    ap = argparse.ArgumentParser(description="通过 commandPort 远程验证 Maya 适配器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7001)
    ap.add_argument("--wait", type=float, default=120.0, help="等待 Maya 回结果的秒数")
    ap.add_argument("--diag-only", action="store_true", help="只跑环境诊断")
    ap.add_argument("--gui", action="store_true",
                    help="不通过 commandPort 推送；假定自检已由用户在 Maya GUI "
                         "Script Editor 里跑过并写好了结果文件，本脚本只轮询结果")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if REMOTE_JSON.exists():
        REMOTE_JSON.unlink()   # 清掉上一次的结果，避免读到陈旧数据

    code = MAYA_CODE
    if args.diag_only:
        code = MAYA_CODE.replace("    # ---------------- 适配器加载",
                                 "    raise SystemExit(0)\n    # ---------------- 适配器加载")

    src = write_selfcheck_src(code)
    print("自检源码已写出: %s (%d 字节)" % (src, src.stat().st_size))

    if args.gui:
        print("GUI 模式：假定自检已在 Maya GUI Script Editor 后台线程跑过，"
              "只轮询结果文件 %s ..." % REMOTE_JSON)
    else:
        print("投递执行命令到 %s:%d ..." % (args.host, args.port))
        try:
            echo = push(str(src), args.host, args.port)
        except Exception as exc:
            print("连不上 Maya 的 commandPort %s:%d —— %s" % (args.host, args.port, exc))
            print("\n请先在 Maya 的 Script Editor 里执行下面这一行（Python 页签）：")
            print('  exec(open(r"E:/CirnoWorkSpace/AssetBridge/maya/plugin/userSetup.py", encoding="utf-8").read())')
            print("\n它会挂上适配器并打开 commandPort 7001。执行后 Maya 输出窗口应有：")
            print("  [bridge] commandPort 已开启: 127.0.0.1:7001 (python)")
            return 2

        if "LAUNCHED" in echo:
            print("自检已在 Maya 后台线程启动（commandPort 主线程已释放，不会再卡死），等待结果文件 ...")
        elif echo.strip():
            print("commandPort 回显（前 500 字）：\n" + echo[:500])

    # 轮询时**必须等自检真正跑完**再收尾：结果文件是「阶段性 dump」的，
    # 文件一存在就读取会抢到早期版本（停在导出步）。只有 R["done"]==True
    # 才是 finally 块写出的完整结果。GUI 模式尤其如此。
    deadline = time.time() + args.wait
    result = None
    while time.time() < deadline:
        if REMOTE_JSON.exists():
            try:
                candidate = json.loads(REMOTE_JSON.read_text(encoding="utf-8"))
                if candidate.get("done") is True:
                    result = candidate
                    break
            except Exception:
                pass
        time.sleep(0.5)
    else:
        print("等不到 Maya 自检完成的标志（超时 %.0fs）" % args.wait)
        # 超时也尽量用当前已有内容，便于排查
        if REMOTE_JSON.exists():
            try:
                result = json.loads(REMOTE_JSON.read_text(encoding="utf-8"))
            except Exception:
                result = None
        if result is None:
            print("可能原因：Maya 正忙 / 弹了模态对话框 / commandPort 通道卡死")
            return 3

    diag = result.get("diagnostics") or {}
    print("\n=== Maya 环境诊断 ===")
    for k in ("maya_version", "maya_api", "python", "user_app_dir", "user_script_dir",
              "vaccine_loaded", "adapter_preloaded", "scene", "scene_modified"):
        if k in diag:
            print("  %-18s %s" % (k, diag[k]))
    if diag.get("maya_script_path"):
        print("  MAYA_SCRIPT_PATH（顺序即优先级，标 * 的目录里有 userSetup.py）：")
        for e in diag["maya_script_path"]:
            print("    %s %s%s" % ("*" if e["has_userSetup"] else " ",
                                   "" if e["exists"] else "(不存在) ", e["path"]))
    if diag.get("assemblies"):
        print("  场景节点: %s" % diag["assemblies"])

    print("\n=== 自检步骤 ===")
    for s in result.get("steps", []):
        print("  [%s] %s %s" % ("OK" if s["ok"] else "FAIL", s["name"], s.get("detail", "")))
    for s in result.get("skipped", []):
        print("  [SKIP] %s —— %s" % (s["name"], s["why"]))
    print("\n%d/%d 通过" % (result.get("passed", 0), result.get("total", 0)))
    if result.get("conversion"):
        print("换算结论: %s" % result["conversion"])
    for issue in result.get("issues", []):
        print("!! %s" % issue)

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n结果已写入 %s" % OUT_JSON)
    return 0 if result.get("passed") == result.get("total") else 1


if __name__ == "__main__":
    sys.exit(main())
