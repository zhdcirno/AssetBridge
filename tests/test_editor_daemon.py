#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PS / SAI2 外部编辑器守护进程无头测试。

不启动真实的 Photoshop / SAI2（GUI 在无显示会话里起不来，也不想弹窗），
而是：
  1) 起一个真的中转服务（独立端口 + 临时数据根）
  2) 用 importlib 从文件加载 tools/editor_daemon.py
  3) monkeypatch subprocess.Popen（记录启动参数、返回假进程），
     并把 SETTLE/GRACE 常量改小加速
  4) 灌入发往 photoshop / sai2 的贴图传输，驱动周期，断言：
     - 原地保存 → 回传一条 kind=textures 给原发送端（return_of/source_file），
       原传输 done，负载目录里是保存后的内容
     - 另存为新文件（同目录新图片）→ 同样回传
     - kind 不是 textures / 缺 from.app → 明确 error 不卡死
     - 编辑器没保存就退出 → 宽限期后 cancelled
     - 服务不可达 → cycle() 不抛异常

用法： python tests/test_editor_daemon.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import server as bridge_server  # noqa: E402

PORT = 27698
BASE = "http://127.0.0.1:{0}".format(PORT)
DAEMON = ROOT / "tools" / "editor_daemon.py"

PASSED, FAILED = [], []


# ---------------------------------------------------------------------------
# HTTP 辅助（绕过系统代理）
# ---------------------------------------------------------------------------

import urllib.request  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=10):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(BASE + "/api/v1" + path, data=data,
                               headers=headers, method=method)
    with _OPENER.open(r, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def check(name, fn):
    try:
        info = fn()
        PASSED.append(name)
        print("  [OK]   {0}{1}".format(name, "  -- " + str(info) if info else ""))
        return True
    except Exception as exc:
        FAILED.append((name, exc))
        print("  [FAIL] {0}  -- {1}: {2}".format(name, type(exc).__name__, exc))
        return False


def wait_port(timeout=10.0):
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.1)
    return False


def load_daemon():
    spec = importlib.util.spec_from_file_location("ab_editor_daemon", str(DAEMON))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, exit_code=None):
        self._code = exit_code

    def poll(self):
        return self._code


class PopenSpy:
    """记录 subprocess.Popen 的调用；exit_code=None 表示进程仍在运行。"""

    def __init__(self, exit_code=None):
        self.calls = []
        self.exit_code = exit_code

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return _FakeProc(self.exit_code)


def publish_texture(tmp: Path, to_app: str, from_app: str = "blender",
                    project: str = "TestProj", name: str = "T_Tex",
                    kind: str = "textures", omit_from=False, options=None):
    """建一条贴图 transfer 并发布，返回 (tid, payload_dir, png_path)。"""
    body = {
        "asset_name": name,
        "kind": kind,
        "format": "png",
        "to": {"app": to_app, "project": "", "content_path": ""},
        "options": options or {},
        "note": "test",
    }
    if not omit_from:
        body["from"] = {"app": from_app, "project": project}
    rec = req("POST", "/transfers", body)
    tid = rec["transfer"]["id"]
    cfg = req("GET", "/config")
    dest = Path(cfg["payload_dir"]) / tid
    dest.mkdir(parents=True, exist_ok=True)
    png = tmp / "{0}.png".format(name)
    if not png.exists():
        png.write_bytes(b"original-png-bytes")
    shutil.copy2(str(png), str(dest / png.name))
    req("POST", "/transfers/{0}/publish".format(tid), {})
    return tid, dest, dest / png.name


def transfer(tid):
    return req("GET", "/transfers/{0}".format(tid))


def all_transfers():
    return req("GET", "/transfers?limit=100")["transfers"]


def make_daemon(mod, state_dir: Path):
    cfg = {
        "server": BASE,
        "state_dir": state_dir,
        "apps": {"photoshop": str(state_dir / "fake_ps.exe"),
                 "sai2": str(state_dir / "fake_sai2.exe")},
    }
    # 假 exe 只要存在即可（Popen 被 spy 掉了）
    for p in cfg["apps"].values():
        Path(p).write_bytes(b"MZ-fake")
    d = mod.Daemon(cfg, interval=0.2, once=False, verbose=False)
    return d, cfg


def run_cycles(d, until=None, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d.cycle()
        d.advance_watches()
        if until and until():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    print("\n=== PS/SAI2 编辑器守护进程无头测试 ({0}) ===\n".format(DAEMON))

    tmp = Path(tempfile.mkdtemp(prefix="ab_editor_test_"))
    t = threading.Thread(target=bridge_server.run_server,
                         args=(tmp, "127.0.0.1", PORT), daemon=True)
    t.start()
    if not wait_port():
        print("服务没能起来")
        return 1

    mod = load_daemon()
    # 加速：稳定判定/宽限期/委托判定全部改小
    mod.SETTLE_SEC = 0.05
    mod.EXIT_GRACE_SEC = 0.05
    mod.LAUNCH_DELEGATE_SEC = 0.0

    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    d, cfg = make_daemon(mod, state_dir)

    spy = PopenSpy(exit_code=None)     # 进程保持运行
    orig_popen = subprocess.Popen
    subprocess.Popen = spy
    try:
        # -- 1. PS 原地保存 → 回传原发送端 ---------------------------------
        # 模拟 UE 发送端：options 里带 asset_paths / source_files
        tid, dest, png = publish_texture(
            tmp, "photoshop", from_app="unreal", name="T_PsCase",
            options={"editor": "photoshop",
                     "asset_paths": ["/Game/Bridge/Textures/T_PsCase"],
                     "source_files": ["T_PsCase.png"]})

        def _ps_save_ok():
            # 第一步：守护进程应 claim 并用 PS 打开文件（状态离开 queued）
            ok = run_cycles(d, lambda: transfer(tid)["status"] != "queued")
            assert ok, "PS transfer 未被守护进程领走"
            assert transfer(tid)["status"] == "claimed", transfer(tid)["status"]
            # 保存动作：原地改写文件
            png.write_bytes(b"photoshop-edited-bytes")
            ok2 = run_cycles(d, lambda: transfer(tid)["status"] == "done"
                             and any(x["to"]["app"] == "unreal"
                                     and x["kind"] == "textures"
                                     and (x.get("options") or {}).get("return_of") == tid
                                     for x in all_transfers()))
            assert ok2, "保存后没有产生回传 transfer"
            back = [x for x in all_transfers()
                    if x["to"]["app"] == "unreal" and x["kind"] == "textures"
                    and (x.get("options") or {}).get("return_of") == tid][0]
            assert back["status"] == "queued", "回传应已发布: {0}".format(back["status"])
            assert back["to"]["project"] == "TestProj", "回传 project 应为发送端工程"
            assert back["options"].get("source_file") == "T_PsCase.png", "缺 source_file"
            # 原 options 必须合并回去（UE 靠 asset_paths 定位原资产）
            assert back["options"].get("asset_paths") == ["/Game/Bridge/Textures/T_PsCase"], \
                back["options"]
            assert "T_PsCase.png" in back["files"], back["files"]
            # 回传负载里必须是保存后的内容
            back_dir = Path(req("GET", "/config")["payload_dir"]) / back["id"]
            data = (back_dir / "T_PsCase.png").read_bytes()
            assert data == b"photoshop-edited-bytes", "回传的不是保存后的内容: {0!r}".format(data)
            assert spy.calls, "subprocess.Popen 没被调用（没尝试打开 PS）"
            assert spy.calls[-1][0].endswith("fake_ps.exe"), spy.calls[-1]
            assert str(png) in spy.calls[-1], "PS 启动参数里没有待编辑文件"
            rec = transfer(tid)
            assert rec["status"] == "done", rec["status"]
            assert rec.get("result", {}).get("returned") is True, rec.get("result")
            return "回传 -> unreal（options 合并 + return_of/source_file 齐全，负载为保存后内容）"

        check("PS 原地保存后回传发送端", _ps_save_ok)

        # -- 2. SAI2 另存为新文件（同目录新图片） ---------------------------
        tid2, dest2, png2 = publish_texture(tmp, "sai2", name="T_SaiCase")

        def _sai_saveas_ok():
            ok = run_cycles(d, lambda: transfer(tid2)["status"] != "queued")
            assert ok, "SAI2 transfer 未被处理"
            # 另存为：原文件不动，同目录出现新文件
            (dest2 / "T_SaiCase_v2.png").write_bytes(b"sai2-saveas-bytes")
            ok2 = run_cycles(d, lambda: any(
                x["to"]["app"] == "blender" and x["kind"] == "textures"
                and (x.get("options") or {}).get("return_of") == tid2
                for x in all_transfers()))
            assert ok2, "另存为新文件没有触发回传"
            back = [x for x in all_transfers()
                    if (x.get("options") or {}).get("return_of") == tid2][0]
            assert "T_SaiCase_v2.png" in back["files"], back["files"]
            assert "T_SaiCase.png" not in back["files"], "未改动的原文件不应回传"
            assert transfer(tid2)["status"] == "done"
            return "另存为 T_SaiCase_v2.png 已回传"

        check("SAI2 另存为新文件同样回传", _sai_saveas_ok)

        # -- 3. kind 不是 textures → 明确 error ------------------------------
        tid3, _, _ = publish_texture(tmp, "photoshop", kind="static_mesh",
                                     name="T_WrongKind")

        def _wrong_kind():
            ok = run_cycles(d, lambda: transfer(tid3)["status"] in ("done", "error", "cancelled"))
            assert ok
            r = transfer(tid3)
            assert r["status"] == "error", "应为 error: {0}".format(r["status"])
            msg = (r.get("result") or {}).get("message", "")
            assert "贴图" in msg, "错误信息应说明只收贴图: {0}".format(msg)
            return msg

        check("收到非贴图类型：明确报错", _wrong_kind)

        # -- 4. 缺 from.app → 无法回传，明确 error ---------------------------
        tid4, _, _ = publish_texture(tmp, "photoshop", omit_from=True,
                                     name="T_NoFrom")

        def _no_from():
            ok = run_cycles(d, lambda: transfer(tid4)["status"] in ("done", "error", "cancelled"))
            assert ok
            r = transfer(tid4)
            assert r["status"] == "error", "应为 error: {0}".format(r["status"])
            msg = (r.get("result") or {}).get("message", "")
            assert "from.app" in msg, msg
            return msg

        check("发送端未标注来源：明确报错", _no_from)

        # -- 5. 没保存就关编辑器 → 宽限期后 cancelled ------------------------
        spy.exit_code = 1       # 进程立即退出
        tid5, _, _ = publish_texture(tmp, "photoshop", name="T_NoSave")

        def _abandon():
            ok = run_cycles(d, lambda: transfer(tid5)["status"] in ("done", "error", "cancelled"))
            assert ok
            r = transfer(tid5)
            assert r["status"] == "cancelled", "应为 cancelled: {0} / {1}".format(
                r["status"], r.get("result"))
            # 不应产生任何回传
            assert not any((x.get("options") or {}).get("return_of") == tid5
                           for x in all_transfers()), "未保存却产生了回传"
            return "已按 cancelled 收尾"

        check("编辑器未保存即关闭：cancelled", _abandon)
        spy.exit_code = None

        # -- 6. 服务不可达：cycle 不抛异常 -----------------------------------
        def _down_safe():
            saved = d.server
            d.server = "http://127.0.0.1:1"
            try:
                for _ in range(3):
                    d.cycle()
                    d.advance_watches()
                return "3 个周期无异常"
            finally:
                d.server = saved

        check("服务不可达：静默退避不抛异常", _down_safe)

        # -- 7. --stop 子命令（未运行时返回 0） ------------------------------
        def _stop_idle():
            rc = mod.do_stop(cfg)
            assert rc == 0, "未运行时应返回 0，实际 {0}".format(rc)
            return "rc=0"

        check("--stop 对未运行的守护返回 0", _stop_idle)

    finally:
        subprocess.Popen = orig_popen

    print("\n=== 结果: {0} 通过 / {1} 失败 ===\n".format(len(PASSED), len(FAILED)))
    for name, exc in FAILED:
        print("  x {0}: {1}".format(name, exc))
    try:
        req("POST", "/shutdown", {})
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
