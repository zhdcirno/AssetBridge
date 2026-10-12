# -*- coding: utf-8 -*-
"""Maya 适配器桩驱动测试
============================================================================
为什么需要它：
    Maya 2023 在这台机器上 mayapy / mayabatch 都起不来（standalone.pyd
    DLL load failed、mayabatch 退出码 0xC0000142），所以没法用真 Maya 跑单测。
    但适配器里的**绝大部分逻辑与 Maya 无关**：HTTP 协议、transfer 元数据、
    发布/认领/回执、idle 节流、路由档案读取……这些都可以用「假 maya 模块」
    把真代码驱动起来，打真中转服务验证。

做法：
    1. 在导入适配器之前，往 sys.modules 塞入假的 maya / maya.cmds /
       maya.mel / maya.utils；
    2. 假 cmds 记录被调用的参数，假 mel.eval 解析 FBXExport 命令并落一个
       假 FBX 文件；
    3. 然后正常 import asset_bridge_maya，调用它的 start / send_selection /
       _on_idle，用真实中转服务做端到端校验。

这个脚本验证的是**适配器逻辑**，不能替代真 Maya 里的实机验证
（真机验证走 tests/maya_commandport_verify.py + commandPort）。

用法：
    python tests/maya_stub_harness.py [--server http://127.0.0.1:27631]
============================================================================
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(ROOT, "maya", "plugin")
LOGS = os.path.join(ROOT, "logs")

PASSED, FAILED = [], []
CALLS = {"warning": [], "mel": [], "command_port": []}
FAKE_NODES = {"|persp", "|top", "|front", "|side"}   # 假装场景里已有这些 assembly


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print("  [{0}] {1}{2}".format("PASS" if cond else "FAIL", name,
                                  ("  <- " + str(detail)) if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 假 maya 模块
# ---------------------------------------------------------------------------
def _install_fake_maya():
    maya = types.ModuleType("maya")
    cmds = types.ModuleType("maya.cmds")
    mel = types.ModuleType("maya.mel")
    utils = types.ModuleType("maya.utils")

    state = {"selection": [], "scriptjobs": [], "next_job": 1}

    def about(**kw):
        return "2023"

    def ls(**kw):
        if kw.get("selection"):
            return list(state["selection"])
        if kw.get("assemblies"):
            return sorted(FAKE_NODES)
        return []

    def nodeType(node, **kw):
        return "transform"

    def listRelatives(node, **kw):
        return []   # 桩里没有 joint，send_selection 会判成 static_mesh

    def warning(msg):
        CALLS["warning"].append(msg)

    def pluginInfo(name, **kw):
        return True

    def loadPlugin(name, **kw):
        return None

    def commandPort(**kw):
        CALLS["command_port"].append(kw)
        return kw.get("name", "")

    def scriptJob(**kw):
        jid = state["next_job"]
        state["next_job"] += 1
        state["scriptjobs"].append(kw)
        return jid

    def maya_file(path, **kw):
        """假导入：往场景里加一个新 assembly，供 import_fbx 的 diff 逻辑用。"""
        base = os.path.basename(path).split(".")[0]
        FAKE_NODES.add("|" + base)
        return ["|" + base]

    def mel_eval(cmd):
        CALLS["mel"].append(cmd)
        if cmd.startswith("FBXExport "):
            # 形式：FBXExport -f "路径" -s
            parts = cmd.split('"')
            if len(parts) >= 2:
                out = parts[1].replace("/", os.sep)
                d = os.path.dirname(out)
                if d and not os.path.isdir(d):
                    os.makedirs(d)
                with open(out, "wb") as fh:
                    fh.write(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\x00" * 256)
        return None

    cmds.about = about
    cmds.ls = ls
    cmds.nodeType = nodeType
    cmds.listRelatives = listRelatives
    cmds.warning = warning
    cmds.pluginInfo = pluginInfo
    cmds.loadPlugin = loadPlugin
    cmds.commandPort = commandPort
    cmds.scriptJob = scriptJob
    cmds.file = maya_file
    cmds.select = lambda *a, **k: state["selection"].clear()
    mel.eval = mel_eval
    utils.executeDeferred = lambda fn: fn()

    maya.cmds = cmds
    maya.mel = mel
    maya.utils = utils

    sys.modules["maya"] = maya
    sys.modules["maya.cmds"] = cmds
    sys.modules["maya.mel"] = mel
    sys.modules["maya.utils"] = utils
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:27631")
    args = ap.parse_args()

    state = _install_fake_maya()
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)

    import asset_bridge_maya as bridge   # noqa: E402  必须在装好假模块之后

    bridge.CONFIG["server_url"] = args.server.rstrip("/")
    bridge.CONFIG["project"] = "MayaStubTest"
    # 目标用一次性 app 名，避免污染 unreal / blender 的收件箱
    PROBE_APP = "maya_stub_probe"
    RESULT = {"server": bridge.CONFIG["server_url"]}

    print("== 1. 语法与版本兼容 ==")
    import ast
    src = open(os.path.join(PLUGIN_DIR, "asset_bridge_maya", "__init__.py"),
               "rb").read()
    for label, ver in (("py3.9 (Maya 2023)", (3, 9)), ("py3.11 (Maya 2024+)", (3, 11))):
        try:
            ast.parse(src, feature_version=ver)
            check("语法兼容 " + label, True)
        except SyntaxError as exc:
            check("语法兼容 " + label, False, exc)
    text = src.decode("utf-8")
    check("无 f-string（py2.7 会语法错误）", 'f"' not in text and "f'" not in text)
    check("无 pathlib（py2.7 没有）", "import pathlib" not in text)

    print("\n== 2. start()：注册 + 挂 idle + 开 commandPort ==")
    job = bridge.start()
    check("scriptJob 已挂上", job is not None and len(state["scriptjobs"]) >= 1, state["scriptjobs"])
    check("commandPort 已开启 7001",
          any("7001" in str(c.get("name", "")) for c in CALLS["command_port"]),
          CALLS["command_port"])
    check("会话已注册（拿到 session_id）", bool(bridge._session_id), bridge._session_id)
    check("启动有中文提示日志",
          any("适配器已启动" in w for w in CALLS["warning"]), CALLS["warning"][-3:])
    RESULT["session_id"] = bridge._session_id

    print("\n== 3. send_selection()：导出 + 建记录 + 发布 ==")
    state["selection"] = ["|pCube1"]
    n = bridge.send_selection(target_app=PROBE_APP, note="stub harness")
    check("send_selection 返回 1", n == 1, n)
    check("MEL 里调过 FBXExportScaleFactor", any("FBXExportScaleFactor" in m for m in CALLS["mel"]))
    check("MEL 导出用的是正斜杠路径",
          all("\\" not in m.split('"')[1] for m in CALLS["mel"]
              if m.startswith("FBXExport ") and len(m.split('"')) >= 2))
    check("MEL 里没有把缩放写成 100",
          not any("FBXExportScaleFactor 100" in m or "FBXExportScaleFactor -v 100" in m
                  for m in CALLS["mel"]), CALLS["mel"][:4])

    # 查服务端这条记录
    pd = bridge.payload_dir()
    check("能拿到负载目录", bool(pd), pd)
    RESULT["payload_dir"] = pd

    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(method, path, body=None):
        url = bridge.CONFIG["server_url"] + "/api/v1" + path
        req = urllib.request.Request(url=url, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode("utf-8")
        with op.open(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    hist = api("GET", "/transfers?limit=20")["transfers"]
    mine = [t for t in hist if t["to"]["app"] == PROBE_APP
            and t["asset_name"] == "pCube1"]
    check("服务端能查到这条 transfer", bool(mine), [t["id"] for t in hist[:3]])
    if mine:
        rec = mine[0]
        RESULT["transfer_id"] = rec["id"]
        RESULT["transfer_status"] = rec["status"]
        check("状态已是 queued", rec["status"] == "queued", rec["status"])
        check("unit 元数据是 cm / Y-up",
              rec["unit"].get("scene_unit") == "cm" and str(rec["unit"].get("up_axis")).upper() == "Y",
              rec["unit"])
        check("objects 元数据带上了选中对象", len(rec.get("objects") or []) == 1, rec.get("objects"))
        fdir = os.path.join(pd, rec["id"])
        files = os.listdir(fdir) if os.path.isdir(fdir) else []
        check("FBX 真的落在负载目录", files == ["pCube1.fbx"], files)
        if files:
            size = os.path.getsize(os.path.join(fdir, files[0]))
            RESULT["fbx_bytes"] = size
            check("FBX 字节数 > 0", size > 100, size)

    print("\n== 4. idle 回调不做网络 IO（Bug3 回归：网络已移到后台线程） ==")
    counter = {"n": 0}
    real_req = bridge._req

    def counting_req(method, path, *a, **kw):
        counter["n"] += 1
        return real_req(method, path, *a, **kw)

    bridge._req = counting_req
    for _ in range(200):
        bridge._on_idle()
    bridge._req = real_req
    check("200 次 idle 回调不发任何 HTTP（网络 IO 全在后台 daemon 线程）",
          counter["n"] == 0, "实际 {0} 次 HTTP".format(counter["n"]))
    check("后台轮询线程已启动且存活",
          bridge._worker is not None and bridge._worker.is_alive(),
          bridge._worker)
    RESULT["idle_http_calls_for_200_ticks"] = counter["n"]

    print("\n== 5. 接收方向：认领 + 导入 + 回执 ==")
    # 真实 Maya 若在线，会与桩测试竞争 app=maya 收件箱（适配器的 inbox 查询硬编码
    # app=maya，无法用 project 隔离；两个消费者谁先 claim 谁拿到）。此时接收用例
    # 结果不可靠（transfer 可能被真实 Maya 抢走），故检测到真实 Maya 就跳过并说明。
    _sessions = api("GET", "/sessions").get("sessions", [])
    _real_maya = [s for s in _sessions
                  if s.get("app") == "maya" and s.get("status") == "online"
                  and s.get("project") != "MayaStubTest"
                  and s.get("session_id") != bridge._session_id]
    if _real_maya:
        print("  [SKIP] 检测到真实 Maya 适配器在线（session={0}, project={1}）".format(
            _real_maya[0].get("session_id"), _real_maya[0].get("project")))
        print("         它会与桩测试竞争 app=maya 收件箱；接收用例需在停止真实 Maya 后运行，本次跳过。")
        RESULT["receive_case"] = "skipped: real maya online"
    else:
        dummy = os.path.join(HERE, "fixtures", "maya_stub_incoming.fbx")
        if not os.path.isdir(os.path.dirname(dummy)):
            os.makedirs(os.path.dirname(dummy))
        with open(dummy, "wb") as fh:
            fh.write(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\x00" * 256)

        rec = api("POST", "/transfers", {
            "asset_name": "SM_StubIncoming", "kind": "static_mesh", "format": "fbx",
            "to": {"app": "maya", "project": "MayaStubTest"},
            "from": {"app": "unreal"},
        })["transfer"]
        tid = rec["id"]
        upload = urllib.request.Request(
            url="{0}/api/v1/transfers/{1}/files/{2}".format(
                bridge.CONFIG["server_url"], tid, "SM_StubIncoming.fbx"),
            method="PUT",
            data=open(dummy, "rb").read(),
            headers={"Content-Type": "application/octet-stream"})
        op.open(upload, timeout=10).read()
        api("POST", "/transfers/{0}/publish".format(tid), {})

        nodes_before = set(FAKE_NODES)
        # 新架构：认领发生在后台 daemon 线程（按 poll_interval 节奏轮询收件箱），
        # 主线程 idle 回调只负责消费队列并执行导入。所以 publish 之后**不能**只调
        # 一次 _on_idle 就断言——那时后台线程大概率还没认领。这里按 0.2s 间隔轮询、
        # 总超时 15s，边调 _on_idle() 消费边查 status，断言用「等待后的最终状态」。
        import time as _time
        done = api("GET", "/transfers/{0}".format(tid))
        waited = 0.0
        for _ in range(75):                        # 0.2s * 75 = 15s 上限
            bridge._on_idle()                      # 主线程消费队列、执行导入
            done = api("GET", "/transfers/{0}".format(tid))
            if done["status"] == "done":
                break
            _time.sleep(0.2)
            waited += 0.2
        if done["status"] != "done":
            print("  [诊断] 等待接收超时（已等 {0:.1f}s）：status={1}  "
                  "_fail_streak={2}  _circuit_open={3}".format(
                      waited, done.get("status"),
                      bridge._fail_streak, bridge._circuit_open))
        check("transfer 状态变成 done", done["status"] == "done", done["status"])
        check("回执带上了导入的层级名",
              "SM_StubIncoming" in json.dumps(done.get("result") or {}),
              done.get("result"))
        check("适配器确实往场景里加了节点", len(FAKE_NODES - nodes_before) == 1,
              sorted(FAKE_NODES - nodes_before))
        check("idle 里认领后不会再被重复认领",
              done["claimed_by"] == bridge._session_id, done.get("claimed_by"))
        RESULT["incoming_transfer_id"] = tid
        RESULT["incoming_status"] = done["status"]

    print("\n== 6. 异常路径：服务不可达时不能抛 ==")
    bridge.CONFIG["server_url"] = "http://127.0.0.1:1"   # 必然连不上
    try:
        bridge._on_idle()
        check("服务不可达时 _on_idle 静默返回（不抛）", True)
    except Exception as exc:
        check("服务不可达时 _on_idle 静默返回（不抛）", False,
              "{0}: {1}".format(type(exc).__name__, exc))
    try:
        bridge.register()
        check("服务不可达时 register 抛 BridgeError 供调用方处理", False, "没抛")
    except bridge.BridgeError as exc:
        check("服务不可达时 register 抛 BridgeError 供调用方处理", True)
    except Exception as exc:
        check("服务不可达时 register 抛 BridgeError 供调用方处理", False, type(exc).__name__)
    bridge.CONFIG["server_url"] = args.server.rstrip("/")
    # 收尾：停掉后台轮询线程与 scriptJob，避免桩进程退出前继续轮询收件箱
    try:
        bridge.stop()
    except Exception:
        pass

    # -----------------------------------------------------------------
    RESULT["passed"] = len(PASSED)
    RESULT["failed"] = len(FAILED)
    RESULT["failed_names"] = FAILED
    if not os.path.isdir(LOGS):
        os.makedirs(LOGS)
    with open(os.path.join(LOGS, "maya_stub_result.json"), "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("通过 {0} 项，失败 {1} 项".format(len(PASSED), len(FAILED)))
    if FAILED:
        print("失败清单: " + ", ".join(FAILED))
    print("结构化结果: logs/maya_stub_result.json")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
