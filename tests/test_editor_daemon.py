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
     - 同一文件第二次保存 → 再次回传（watch 保留 + settle 重新计时，
       2026-10-14 P0 修复：旧逻辑一个 watch 只能 finalize 一次）
     - 超时兜底：delegated 进程 + 空闲超 WATCH_IDLE_SEC → expire → cancelled
     - 另存为新文件（同目录新图片）→ 同样回传
     - 去重键带 asset_paths：同名不同资产（不同 asset_paths）两条 watch
       并存、各自回传（2026-10-15 P0-1 返工）
     - 回退分支（无 asset_paths，Blender 发 PS/SAI2 的真实形态）：
       同名即同一窗口（内容不进键；旧「叠 sha256」在「改一版发一版」
       下不复用、又堆窗口，已被 P0-1 用户复核返工修复）；不同目录同名
       资产串台为已知缺口（已写 release notes）
     - settle 连续双采样：慢速分块写入期间不 finalize、停笔稳定后才回传
       完整内容，同长度覆写（仅 mtime 变）同样等停笔（P1-3 返工）
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
                    kind: str = "textures", omit_from=False, options=None,
                    content: bytes = None):
    """建一条贴图 transfer 并发布，返回 (tid, payload_dir, png_path)。

    content：指定源 png 内容（覆盖复用）。回退分支去重键只认主文件名
    （不含 sha256）——不同 content 的同名文件在「我改一版发一版」工作流
    下仍复用同一窗口，故 content 仅用于区分各用例的稳定路径内容。
    """
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
    if not png.exists() or content is not None:
        png.write_bytes(content if content is not None else b"original-png-bytes")
    shutil.copy2(str(png), str(dest / png.name))
    req("POST", "/transfers/{0}/publish".format(tid), {})
    return tid, dest, dest / png.name


def transfer(tid):
    return req("GET", "/transfers/{0}".format(tid))


def all_transfers():
    return req("GET", "/transfers?limit=100")["transfers"]


def claimed_transfers():
    """status=claimed 的传输（watch 泄漏的直接信号：没人收尾的悬挂）。"""
    return req("GET", "/transfers?limit=200&status=claimed")["transfers"]


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

    # 2026-10-15 三轮 P0：复用分支先探测窗口状态（开着->聚焦去重 / 已关->重开）。
    # 无头测试里没有真实编辑器窗口，统一注入「窗口开着」-> 走 keep-open 去重
    # 分支（superseded 语义），与既有断言一致；「已关 -> 重开」分支由
    # _p0_reopen_when_closed 单独覆盖（其内部临时切换为 False，结束恢复 True）。
    d._is_file_open_in_editor = lambda *a, **k: True

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
            # 保存动作：原地改写文件（守护进程现在监听「稳定打开路径」，
            # 编辑器打开的就是这条路径，必须写它才触发回传）
            live = [w for w in d.watches if w.tid == tid]
            assert live, "守护进程未建立 watch（稳定路径打开失败）"
            Path(live[0].files[0]).write_bytes(b"photoshop-edited-bytes")
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
            # 打开的是「稳定打开路径」（与 tid 无关，只认逻辑文件名），不是 per-tid 路径
            assert any("T_PsCase.png" in c for c in spy.calls[-1]), \
                "PS 启动参数里没有待编辑文件（稳定路径）: {0}".format(spy.calls[-1])
            rec = transfer(tid)
            assert rec["status"] == "done", rec["status"]
            assert rec.get("result", {}).get("returned") is True, rec.get("result")
            return "回传 -> unreal（options 合并 + return_of/source_file 齐全，负载为保存后内容）"

        check("PS 原地保存后回传发送端", _ps_save_ok)

        # -- 1b. 同一文件二次编辑 -> 再次回传（2026-10-14 P0 修复验证） -----
        # 背景：旧逻辑 finalize 成功即 remove watch，且 baseline/dirty 不复位，
        # 导致「SAI2/PS 里文件还开着，改完再保存一次」永不回传。
        def _second_save_returns():
            # 第一次回传后 watch 必须保留（编辑器进程还活着）
            live = [w for w in d.watches if w.tid == tid]
            assert live, "回传后 watch 被无条件移除（P0 bug 复现）"
            assert live[0].returns == 1, live[0].returns
            # 第二次修改同一文件（同样写到稳定打开路径）
            live = [w for w in d.watches if w.tid == tid]
            assert live, "回传后 watch 被移除"
            Path(live[0].files[0]).write_bytes(b"photoshop-edited-again")
            # 推进一个周期：settle 必须重新计时（检测到新变更 -> 记录起始时刻、
            # 清空稳定计时），此刻不应 finalize（旧 bug 是复用旧 settle 计时立即回传）
            d.advance_watches()
            w2 = [w for w in d.watches if w.tid == tid][0]
            assert w2.settle_start_mono is not None and w2.stable_since_mono is None, \
                "settle 未重新计时（应记起始时刻并清空稳定计时）: {0}".format(
                    (w2.settle_start_mono, w2.stable_since_mono))
            n_back = len([x for x in all_transfers()
                          if (x.get("options") or {}).get("return_of") == tid])
            assert n_back == 1, "第二次保存被立即回传（缓冲失效）"
            # 停笔稳定（连续 2 个采样周期）后应产生第二条回传
            ok = run_cycles(d, lambda: len([
                x for x in all_transfers()
                if (x.get("options") or {}).get("return_of") == tid]) >= 2)
            assert ok, "第二次保存没有触发回传"
            # 注意：all_transfers 按 seq 倒序，[0] 才是最新一条
            backs = [x for x in all_transfers()
                     if (x.get("options") or {}).get("return_of") == tid]
            assert backs[0]["status"] == "queued", backs[0]["status"]
            w3 = [w for w in d.watches if w.tid == tid][0]
            assert w3.returns == 2, w3.returns
            # 最新回传负载是第二次编辑后的内容
            back_dir = Path(req("GET", "/config")["payload_dir"]) / backs[0]["id"]
            data = (back_dir / "T_PsCase.png").read_bytes()
            assert data == b"photoshop-edited-again", data
            return "二次保存再次回传；settle 重新计时；watch 保留 returns=2"

        check("同一文件二次保存：再次回传（P0 修复）", _second_save_returns)

        # -- 1c. 超时兜底解 watch（delegated 场景：PS 委托已有实例） ---------
        def _expire():
            mod.WATCH_IDLE_SEC = 0.05
            mod.LAUNCH_DELEGATE_SEC = 999.0   # 进程秒退 -> 判 delegated 不 abandon
            spy.exit_code = 1
            try:
                tid_e, _, _ = publish_texture(tmp, "photoshop", name="T_Expire")
                ok = run_cycles(d, lambda: transfer(tid_e)["status"] != "queued")
                assert ok, "T_Expire 未被守护进程领走"
                ok2 = run_cycles(d, lambda: transfer(tid_e)["status"] == "cancelled"
                                 and not any(w.tid == tid_e for w in d.watches))
                assert ok2, "超时兜底未触发: {0} watches={1}".format(
                    transfer(tid_e)["status"], [w.tid for w in d.watches])
                assert not any((x.get("options") or {}).get("return_of") == tid_e
                               for x in all_transfers()), "未保存却产生了回传"
                return "delegated + 空闲超时 -> expire -> cancelled + 移除 watch"
            finally:
                mod.WATCH_IDLE_SEC = 600.0
                mod.LAUNCH_DELEGATE_SEC = 0.0
                spy.exit_code = None

        check("超时兜底：空闲 watch 自动解除", _expire)

        # -- 2. SAI2 另存为新文件（同目录新图片） ---------------------------
        tid2, dest2, png2 = publish_texture(tmp, "sai2", name="T_SaiCase")

        def _sai_saveas_ok():
            ok = run_cycles(d, lambda: transfer(tid2)["status"] != "queued")
            assert ok, "SAI2 transfer 未被处理"
            live = [w for w in d.watches if w.tid == tid2]
            assert live, "守护进程未建立 watch（稳定路径打开失败）"
            # 另存为：守护进程监视的是「稳定打开目录」，模拟 SAI2 同目录另存新文件
            (Path(live[0].dir) / "T_SaiCase_v2.png").write_bytes(b"sai2-saveas-bytes")
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

        # -- 8. P0 watch 去重：同一文件连发，watching 只留 1 条 -------------
        # 背景（用户实测）：连发 5 次同一文件，watching 堆了 5 条同文件 watch
        # （server 按 tid 分 payload 目录，绝对路径各不同），堆叠 watch 每轮
        # 重复探测/快照同一批文件——SAI2 卡顿的放大器。修复：同 app + 同主
        # 文件名只保留最新一条，旧传输收尾不得悬挂 claimed。
        # 2026-10-15 P0-1 阻塞级修复后的语义：同内容连发 -> 仅首轮 Popen 开
        # 1 个窗口，后续重发命中「存活窗口」判定跳过 Popen，新传输 done 且
        # superseded_by 指向首窗口 tid；watch 恒 1 条（首传输 tid，存活窗口）。
        def _dedup():
            # 新键（2026-10-15 用户复核返工）：回退分支只认主文件名，内容不进键
            # ——「我改一版发一版」= 同名不同内容仍复用同一窗口，不复用、不堆窗口。
            key = ("sai2", "unreal", ("file_name", "t_dedup.png"))
            tids = []
            for i in range(3):
                t, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                          name="T_Dedup")
                tids.append(t)
                ok = run_cycles(d, lambda tt=t: transfer(tt)["status"] != "queued")
                assert ok, "第 {0} 次发送未被守护进程领走".format(i + 1)
            # 同名文件（同内容）-> 复用同一窗口：watch 恒 1 条（首传输 tid）
            ws = [w for w in d.watches if w.dedup_key() == key]
            assert len(ws) == 1, "去重失败：同文件 watch {0} 条 {1}".format(
                len(ws), [w.tid for w in ws])
            assert ws[0].tid == tids[0], "存活窗口应是首传输（复用，不更新为最新）"
            # 首传输仍 claimed（正在被监听），不被 superseded
            assert transfer(tids[0])["status"] == "claimed", \
                "首传输应仍 claimed（窗口存活）: {0}".format(
                    transfer(tids[0])["status"])
            # 后续 2 次传输 done + superseded_by=首 tid（复用窗口）
            for i, t in enumerate(tids[1:], start=1):
                r = transfer(t)
                assert r["status"] == "done", \
                    "重发 {0} 状态 {1}".format(t, r["status"])
                # 确认-5 实证：server 把整个 result dict 原样持久化，GET 能读回
                assert (r.get("result") or {}).get("superseded_by") == tids[0], \
                    "重发 {0} 未 superseded 到首窗口: {1}".format(i, r.get("result"))
            # 保存首窗口的「稳定打开路径」-> 只应产生 1 条回传，return_of 是首 tid
            stable = ws[0].files[0]
            Path(stable).write_bytes(b"dedup-edited")
            ok2 = run_cycles(d, lambda: (
                transfer(tids[0])["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get(
                                 "return_of") == tids[0]]) == 1))
            assert ok2, "保存后未产生恰好 1 条回传"
            assert len([w for w in d.watches if w.dedup_key() == key]) == 1
            # 三条传输全部收尾（首=已回传、后两条=被 superseded），
            # 不得有 claimed 悬挂——这才是 watch 泄漏的直接信号
            assert not claimed_transfers(), \
                "有传输挂在 claimed: {0}".format([x["id"] for x in claimed_transfers()])
            return "连发 3 次 -> 同文件 watch 恒 1 条（首 tid={0}…）；重发全 done；回传 1 条".format(
                tids[0][:8])

        check("P0 watch 去重：同文件连发只留 1 条监视", _dedup)

        # -- 8a. P0-1 返工：去重键用 asset_paths，同名不同资产不误杀 ---------
        def _dedup_asset_paths():
            # 同名文件 + 不同 asset_paths（UE 里两个不同目录的同名资产）
            # -> 两条 watch 并存、互不接管，各自保存后各自回传。
            opts_a = {"editor": "sai2",
                      "asset_paths": ["/Game/Bridge/Textures/T_SameName"],
                      "source_files": ["T_SameName.png"]}
            opts_b = {"editor": "sai2",
                      "asset_paths": ["/Game/Bridge/Textures/Props/T_SameName"],
                      "source_files": ["T_SameName.png"]}
            ta, da, pa = publish_texture(tmp, "sai2", from_app="unreal",
                                         name="T_SameName", options=opts_a)
            tb, db, pb = publish_texture(tmp, "sai2", from_app="unreal",
                                         name="T_SameName", options=opts_b)
            ok = run_cycles(d, lambda: all(transfer(t)["status"] != "queued"
                                           for t in (ta, tb)))
            assert ok, "两条传输都被领走"
            wa = [w for w in d.watches if w.tid == ta]
            wb = [w for w in d.watches if w.tid == tb]
            assert wa and wb, "同名不同 asset_paths 被误杀: watches={0}".format(
                [w.tid for w in d.watches])
            assert wa[0].dedup_key() != wb[0].dedup_key(), "键碰撞"
            assert transfer(ta)["status"] == "claimed", transfer(ta)["status"]
            assert transfer(tb)["status"] == "claimed", transfer(tb)["status"]
            # 各自保存 -> 各自回传（return_of 各对应各的 tid）
            # 注意：守护进程现在监听「稳定打开路径」，必须写那条路径才触发回传
            Path(wa[0].files[0]).write_bytes(b"edited-A")
            ok2 = run_cycles(d, lambda: (
                transfer(ta)["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == ta]) == 1
                and not any((x.get("options") or {}).get("return_of") == tb
                            for x in all_transfers())), timeout=5)
            assert ok2, "ta 保存后未恰好回传 1 条（或误伤了 tb）"
            Path(wb[0].files[0]).write_bytes(b"edited-B")
            ok3 = run_cycles(d, lambda: (
                transfer(tb)["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == tb]) == 1),
                timeout=5)
            assert ok3, "tb 保存后未恰好回传 1 条"
            for t, want in ((ta, b"edited-A"), (tb, b"edited-B")):
                back = [x for x in all_transfers()
                        if (x.get("options") or {}).get("return_of") == t][0]
                bdir = Path(req("GET", "/config")["payload_dir"]) / back["id"]
                data = (bdir / "T_SameName.png").read_bytes()
                assert data == want, "tid={0} 回传内容不对: {1!r}".format(t[:8], data)
            assert any(w.tid == ta for w in d.watches), "ta watch 被误删"
            assert any(w.tid == tb for w in d.watches), "tb watch 被误删"
            return "同名不同 asset_paths -> 2 条 watch 并存，各自回传内容正确"

        check("P0-1 去重键：同名不同资产（asset_paths）不误杀", _dedup_asset_paths)

        # -- 8a-2. P0-1 二轮返工：回退分支同名不同内容不互杀 ---------------
        # 用户评审必答：Blender 发贴图到 PS/SAI2 是真实流量（ui.py L72-77
        # 「PS/SAI2 只能收发贴图」），options 只有 {"editor": target}——
        # 没有 asset_paths，走回退分支。若回退键只凭主文件名，两张不同
        # 目录的同名贴图会互杀（旧 watch 被移除、回传永久丢失）= 真 P0。
        # 修复（P0-1 用户复核返工）：回退键只认主文件名，内容不进键——
        # 「我改一版发一版」= 同名不同内容仍复用同一窗口（旧叠 sha256 反而不复用）。
        # -- 8a-2. P0-1 用户复核返工：回退分支「同名即同一窗口」（含不同内容）----
        # Blender/Maya -> PS/SAI2 是真实流量（无 asset_paths），用户工作流是
        # 「我改一版发一版」= 同名不同内容。旧实现键含 sha256 -> 不同内容不同键
        # -> 不复用 -> 又堆窗口（P0-1 阻塞级没修到的分支）。现改为内容不进键，
        # 同名即复用同一窗口：连发两次同名不同内容只开 1 个窗口，新内容覆盖稳定
        # 路径 + 重新聚焦。残余风险（已写 release notes）：不同目录同名的不同资产
        # 也会被并成同一窗口（串台）；UE 有 asset_paths 不受影响。
        def _fallback_same_name_one_window():
            ta, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_Fallback", content=b"fallback-A")
            tb, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_Fallback", content=b"fallback-B")
            ok = run_cycles(d, lambda: all(transfer(t)["status"] != "queued"
                                           for t in (ta, tb)))
            assert ok, "两次传输都被领走"
            # 同名（无 asset_paths）-> 复用同一窗口：只有 1 条 watch
            ws = [w for w in d.watches if w.app == "sai2" and w.from_app == "blender"
                  and w.identity[0] == "file_name" and w.identity[1] == "t_fallback.png"]
            assert len(ws) == 1, "同名不同内容应复用同一窗口，实际 {0} 条: {1}".format(
                len(ws), [w.tid for w in ws])
            key = ("sai2", "blender", ("file_name", "t_fallback.png"))
            assert ws[0].dedup_key() == key, ws[0].dedup_key()
            # 第二条（新内容）被 superseded 到首窗口（复用），稳定路径已是新内容
            assert transfer(tb)["status"] == "done", transfer(tb)["status"]
            assert (transfer(tb).get("result") or {}).get("superseded_by") == ta, \
                "第二条未 superseded 到首窗口: {0}".format(transfer(tb).get("result"))
            # 在稳定路径上保存 -> 恰好 1 条回传，return_of=首 tid，内容为最新 B
            stable = ws[0].files[0]
            Path(stable).write_bytes(b"edited-fb-B")
            ok2 = run_cycles(d, lambda: (
                transfer(ta)["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == ta]) == 1),
                timeout=5)
            assert ok2, "保存后未恰好回传 1 条"
            back = [x for x in all_transfers()
                    if (x.get("options") or {}).get("return_of") == ta][0]
            bdir = Path(req("GET", "/config")["payload_dir"]) / back["id"]
            data = (bdir / "T_Fallback.png").read_bytes()
            assert data == b"edited-fb-B", "回传内容应为最新内容: {0!r}".format(data)
            return ("回退分支同名不同内容 -> 复用 1 窗口（首 tid）；新内容覆盖稳定路径；"
                    "保存后回传 1 条且为最新内容（串台风险已写 release notes）")

        check("P0-1 用户复核：回退分支同名（含不同内容）复用 1 窗口", _fallback_same_name_one_window)

        # -- 8a-3. 定性用例：编辑→发→编辑→再发（不同内容，高频）vs 同内容连发
        # 用户评审精确措辞：同一文件「编辑→发→编辑→再发」字节不同 => 摘要
        # 不同 => 去重键不同 => 应保留 N 条 watch（不同编辑会话，绝不因同名
        # 把第一次编辑的回传悄悄丢掉）；而「同内容连发」= 同一编辑会话重复
        # 发送 => 应去重成 1 条（原 P0 修复保持：旧传输 done+superseded_by）。
        # 确定性驱动 handle()/Watch 路径（publish_texture + 自定义键断言），
        # 不依赖真实编辑器进程。
        def _edit_send_edit_resend():
            # ① 同文件两次发送、字节不同（回退分支，Blender 形态：无 asset_paths）
            #    => 复用同一窗口（1 条 watch）。这是「我改一版发一版」的真实形态：
            #    内容不进键 -> 同名即同一窗口，不同内容也复用，不堆窗口、不丢回传。
            ta, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_Edit", content=b"edit-session-v1")
            tb, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_Edit", content=b"edit-session-v2")
            ok = run_cycles(d, lambda: all(transfer(t)["status"] != "queued"
                                           for t in (ta, tb)))
            assert ok, "两次传输都被领走"
            ws = [w for w in d.watches if w.app == "sai2" and w.from_app == "blender"
                  and w.identity[0] == "file_name" and w.identity[1] == "t_edit.png"]
            assert len(ws) == 1, "同名不同内容应复用同一窗口，实际 {0} 条: {1}".format(
                len(ws), [w.tid for w in ws])
            key = ("sai2", "blender", ("file_name", "t_edit.png"))
            assert ws[0].dedup_key() == key, ws[0].dedup_key()
            # 第二条被 superseded 到首窗口（复用）；稳定路径已是 v2 内容
            assert transfer(tb)["status"] == "done", transfer(tb)["status"]
            assert (transfer(tb).get("result") or {}).get("superseded_by") == ta, \
                "第二条未 superseded 到首窗口: {0}".format(transfer(tb).get("result"))
            # 在稳定路径保存最新内容 -> 恰好 1 条回传，return_of=首 tid，内容最新
            stable = ws[0].files[0]
            Path(stable).write_bytes(b"edited-v2")
            ok2 = run_cycles(d, lambda: (
                transfer(ta)["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == ta]) == 1),
                timeout=5)
            assert ok2, "保存后未恰好回传 1 条"
            back = [x for x in all_transfers()
                    if (x.get("options") or {}).get("return_of") == ta][0]
            bdir = Path(req("GET", "/config")["payload_dir"]) / back["id"]
            data = (bdir / "T_Edit.png").read_bytes()
            assert data == b"edited-v2", "回传内容应为最新内容: {0!r}".format(data)

            # ② 同文件两次发送、字节相同（同一编辑会话重复发送）=>
            # 同样复用首窗口（内容不进键，同名即同窗口），第 2 发 superseded。
            tc, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_EditSame", content=b"edit-same-bytes")
            td, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                      name="T_EditSame", content=b"edit-same-bytes")
            ok3 = run_cycles(d, lambda: all(transfer(t)["status"] != "queued"
                                            for t in (tc, td)))
            assert ok3, "同内容两次传输都被领走"
            key_same = ("sai2", "blender", ("file_name", "t_editsame.png"))
            ws2 = [w for w in d.watches if w.dedup_key() == key_same]
            assert len(ws2) == 1, "同内容连发应去重成 1 条: {0}".format(
                [w.tid for w in ws2])
            assert ws2[0].tid == tc, "存活窗口应是首传输（复用，不更新为最新）"
            r = transfer(tc)
            assert r["status"] == "claimed", "首传输应仍 claimed（窗口存活）: {0}".format(
                r["status"])
            rd = transfer(td)
            assert rd["status"] == "done", "重发应 done: {0}".format(rd["status"])
            assert (rd.get("result") or {}).get("superseded_by") == tc, \
                "superseded_by 未写回: {0}".format(rd.get("result"))
            stable2 = ws2[0].files[0]
            Path(stable2).write_bytes(b"edited-same")
            ok4 = run_cycles(d, lambda: (
                transfer(tc)["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == tc]) == 1),
                timeout=5)
            assert ok4, "幸存 watch 保存后未恰好回传 1 条"
            # 清理本测试建立的 watch（T_Edit/T_EditSame，blender->sai2），
            # 避免遗留 claimed 传输污染后续用例的全局 claimed 断言。
            for w in list(d.watches):
                if (w.app == "sai2" and w.from_app == "blender"
                        and w.identity[0] == "file_name"
                        and w.identity[1] in ("t_edit.png", "t_editsame.png")
                        and w.proc is not None):
                    w.proc._code = 1
            run_cycles(d, lambda: not any(
                w.app == "sai2" and w.from_app == "blender"
                and w.identity[0] == "file_name"
                and w.identity[1] in ("t_edit.png", "t_editsame.png")
                for w in d.watches), timeout=5)
            return ("编辑→发→编辑→再发（不同内容）-> 复用 1 窗口（不堆窗口、回传最新）；"
                    "同内容连发 -> 复用首窗口，第 2 发 superseded")

        check("编辑→发→编辑→再发：不同内容留 2 条 / 同内容去重 1 条",
              _edit_send_edit_resend)

        # -- 8a-4. P0-1 阻塞级修复：同文件连发只开 1 个窗口（跳过 Popen 复用）--
        # 用户实测：连发 5 次同文件，旧逻辑每次都 Popen 新开窗口（SAI2/PS 不
        # 聚焦已开窗口而新开同名标签页），daemon 只监听最新 watch，旧窗口
        # 编辑保存的回传永久丢失。修复：Popen 前判同键「存活窗口」，命中则
        # 跳过 Popen + superseded 新传输，复用现有窗口继续监听。
        def _p0_1_skip_popen():
            spy.calls.clear()
            # 2026-10-15 三轮 P0 修复后，复用分支先探测窗口状态；这里模拟
            # 「窗口开着」-> 走 keep-open 去重分支（不重开），验证不弹 SAI2 确认框。
            d._is_file_open_in_editor = lambda *a, **k: True
            key = ("sai2", "unreal", ("file_name", "t_p01.png"))
            tids = []
            for i in range(5):
                t, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                          name="T_P01")
                tids.append(t)
                ok = run_cycles(d, lambda tt=t: transfer(tt)["status"] != "queued")
                assert ok, "第 {0} 次发送未被守护进程领走".format(i + 1)
            # 验收（新 P0 修复证据，2026-10-15 实机复验）：同名连发只应在「首次
            # 打开」时 Popen 一次稳定路径开窗口；后续 4 次复用（重发）绝不再次
            # Popen——否则 SAI2 会弹「文件已打开，是否放弃当前编辑重新打开」确认框。
            popen_file = [c for c in spy.calls
                          if any("T_P01.png" in str(a)
                                 for a in (c if isinstance(c, (list, tuple)) else [c]))]
            assert len(popen_file) == 1, \
                "同名连发应只 Popen 开窗口 1 次（复用不重开），实际 {0} 次".format(
                    len(popen_file))
            # 关键断言：同文件连发 5 次，Popen 指向的「distinct 打开路径」恒 1 个
            # （= 稳定路径）；单实例据此复用同一窗口，不堆窗口。
            open_paths = set(c[1] for c in spy.calls)
            assert len(open_paths) == 1, \
                "同文件连发应只开 1 个窗口（稳定路径），实际 {0} 个: {1}".format(
                    len(open_paths), open_paths)
            assert all(p.endswith("T_P01.png") for p in open_paths), \
                "稳定打开路径不符: {0}".format(open_paths)
            # watch 恒 1 条（首 tid，复用窗口）
            ws = [w for w in d.watches if w.dedup_key() == key]
            assert len(ws) == 1, "同文件 watch {0} 条（应为 1）".format(len(ws))
            assert ws[0].tid == tids[0], "存活窗口应为首传输 tid"
            # 5 个传输全部收尾：首传输 claimed（在监听）、新 4 个 done 且
            # superseded_by 指向首 tid
            assert transfer(tids[0])["status"] == "claimed", \
                "首传输应 claimed（在监听）: {0}".format(transfer(tids[0])["status"])
            for i, t in enumerate(tids[1:], start=1):
                r = transfer(t)
                assert r["status"] == "done", "重发 {0} 未 done: {1}".format(i, r["status"])
                assert (r.get("result") or {}).get("superseded_by") == tids[0], \
                    "重发 {0} 未 superseded 到首窗口: {1}".format(i, r.get("result"))
            # 保存首窗口的稳定路径 -> 1 条回传，return_of=首 tid（旧窗口不再丢失）
            stable = ws[0].files[0]
            Path(stable).write_bytes(b"p01-edited")
            ok2 = run_cycles(d, lambda: (
                transfer(tids[0])["status"] == "done"
                and len([x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == tids[0]]) == 1))
            assert ok2, "保存首窗口未产生 1 条回传"
            # 本测试 5 个传输不得挂在 claimed（首=已回传、其余 superseded）
            assert not [x["id"] for x in claimed_transfers()
                        if x["id"] in tids], "有 claimed 悬挂: {0}".format(
                [x["id"] for x in claimed_transfers()])
            # 清理：模拟编辑器关闭，让存活 watch 兜底 abandon（transfer 已 done）
            for w in d.watches:
                if w.dedup_key() == key and w.proc is not None:
                    w.proc._code = 1
            run_cycles(d, lambda: not [w for w in d.watches
                                       if w.dedup_key() == key], timeout=5)
            return "同文件连发 5 次 -> 稳定路径窗口 1 个 / watch 1 条（首 tid）；5 传输全 done；回传 1 条"

        check("P0-1 阻塞级：同文件连发只开 1 窗口（跳过 Popen 复用）", _p0_1_skip_popen)

        # -- 8a-5. P0-1 回归护栏：同名（不论内容）复用同一窗口；不同文件名各自开窗口
        # 修复（P0-1 用户复核返工）：去重键只认主文件名、内容不进键，故同名不同内容
        # 也复用同一窗口（不再因叠 sha256 而各开窗口）；不同文件名则各自开窗口不串台。
        def _p0_1_same_name_reuses_window():
            spy.calls.clear()
            d._is_file_open_in_editor = lambda *a, **k: True   # 模拟窗口开着（keep-open 分支）
            # 同名不同内容：4 次发送同一文件名 T_P01b（内容各异）
            tids = []
            for i in range(4):
                content = ("diff-v{0}".format(i)).encode()
                t, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                          name="T_P01b", content=content)
                tids.append(t)
                ok = run_cycles(d, lambda tt=t: transfer(tt)["status"] != "queued")
                assert ok, "第 {0} 次发送未被领走".format(i + 1)
            # 同名 -> 复用同一窗口：distinct 打开路径 = 1，watch = 1（不堆窗口）
            open_paths = set(c[1] for c in spy.calls)
            assert len(open_paths) == 1, \
                "同名不同内容应复用 1 窗口，实际 {0} 个: {1}".format(
                    len(open_paths), open_paths)
            ws = [w for w in d.watches if w.app == "sai2"
                  and w.from_app == "blender"
                  and w.identity[0] == "file_name"
                  and w.identity[1] == "t_p01b.png"]
            assert len(ws) == 1, "同名不同内容 watch {0} 条（应为 1）".format(len(ws))
            # 重发的 3 条必须 done+superseded（不得挂在 claimed）；首条与 T_Other 是
            # 正在监听的合法 claimed，不算泄漏。
            for i, t in enumerate(tids[1:], start=1):
                r = transfer(t)
                assert r["status"] == "done", "重发 {0} 未 done: {1}".format(i, r["status"])
                assert (r.get("result") or {}).get("superseded_by") == tids[0], \
                    "重发 {0} 未 superseded 到首窗口: {1}".format(i, r.get("result"))
            assert not [x["id"] for x in claimed_transfers()
                        if x["id"] in tids[1:]], "重发有 claimed 悬挂: {0}".format(
                [x["id"] for x in claimed_transfers()])
            # 回归：复用是按「文件名」不是「内容」——不同文件名各自开窗口
            t_other, _, _ = publish_texture(tmp, "sai2", from_app="blender",
                                          name="T_Other", content=b"x")
            ok = run_cycles(d, lambda tt=t_other: transfer(tt)["status"] != "queued")
            assert ok
            open_paths2 = set(c[1] for c in spy.calls)
            assert len(open_paths2) == 2, \
                "不同文件名应各自开窗口，distinct 路径应=2，实际 {0}".format(
                    len(open_paths2))
            tids.append(t_other)
            # 清理：模拟编辑器关闭，让 watch 兜底 abandon，避免污染后续用例
            for w in list(d.watches):
                if w.tid in set(tids) and w.proc is not None:
                    w.proc._code = 1
            run_cycles(d, lambda: not any(w.tid in set(tids) for w in d.watches),
                       timeout=5)
            return "同名不同内容连发 4 次 -> 复用 1 窗口（distinct 路径 1，watch 1）；不同文件名各自开窗口"

        check("P0-1 护栏：同名（不论内容）复用同一窗口；不同名各自窗口",
              _p0_1_same_name_reuses_window)

        # -- 8a-6. P0（2026-10-15 三轮）：复用分支必须区分「窗口开着 / 已关」----
        # 用户 P0 追问：复用完全不 Popen 后，「关窗口再发」在 600s 空闲窗口内
        # 会被判成复用 -> 不重开 -> 发送静默丢失。修复后：窗口已关 -> 重新 Popen
        # 打开（不丢发送）；窗口开着 -> 只聚焦去重（不弹 SAI2 确认框）。
        def _p0_reopen_when_closed():
            spy.calls.clear()
            d._is_file_open_in_editor = lambda *a, **k: False   # 模拟窗口已关
            try:
                key = ("sai2", "unreal", ("file_name", "t_reop.png"))
                t1, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                           name="T_Reop")
                ok = run_cycles(d, lambda: transfer(t1)["status"] != "queued")
                assert ok, "首传未领走"
                assert transfer(t1)["status"] == "claimed", transfer(t1)["status"]
                popen1 = [c for c in spy.calls
                          if any("T_Reop.png" in str(a) for a in
                                 (c if isinstance(c, (list, tuple)) else [c]))]
                assert len(popen1) == 1, \
                    "首开应 Popen 1 次，实际 {0}".format(len(popen1))
                # 用户关窗口后 10 分钟内重发 -> 必须重新 Popen（不静默丢）
                t2, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                           name="T_Reop", content=b"reopen-v2")
                ok = run_cycles(d, lambda: transfer(t2)["status"] != "queued")
                assert ok, "重发未领走"
                r2 = transfer(t2)
                assert r2["status"] == "done", r2["status"]
                msg = (r2.get("result") or {}).get("message", "")
                assert "重新打开" in msg, \
                    "重发应走重新打开分支: {0}".format(msg)
                popen2 = [c for c in spy.calls
                          if any("T_Reop.png" in str(a) for a in
                                 (c if isinstance(c, (list, tuple)) else [c]))]
                assert len(popen2) == 2, \
                    "窗口已关必须重新 Popen（不静默丢发送），实际 {0} 次".format(
                        len(popen2))
                # 不追加新 watch（watch 恒 1 条，不堆窗口）
                ws = [w for w in d.watches if w.dedup_key() == key]
                assert len(ws) == 1, "重开不应追加 watch: {0}".format(len(ws))
                # 重开后保存 -> 正常回传（窗口可用，链路未断）
                Path(ws[0].files[0]).write_bytes(b"reopened-edited")
                ok2 = run_cycles(d, lambda: any(
                    (x.get("options") or {}).get("return_of") == t1
                    for x in all_transfers()))
                assert ok2, "重开后保存未回传（重开链路断了）"
                for w in d.watches:
                    if w.dedup_key() == key and w.proc is not None:
                        w.proc._code = 1
                run_cycles(d, lambda: not [w for w in d.watches
                                           if w.dedup_key() == key], timeout=5)
                return "关窗再发 -> 重新 Popen 打开（Popen 2 次、watch 1 条、回传不丢）"
            finally:
                d._is_file_open_in_editor = lambda *a, **k: True   # 恢复全局「窗口开着」

        check("P0（三轮）：关窗再发必须重新打开（不静默丢发送）", _p0_reopen_when_closed)

        # -- 8b. P1-3 返工：settle 连续双采样，慢速分块写入不拦腰截断 --------
        def _settle_dual_sampling():
            import threading as _threading
            tid_s, dest_s, png_s = publish_texture(
                tmp, "sai2", from_app="unreal", name="T_SlowSave")
            ok = run_cycles(d, lambda: transfer(tid_s)["status"] != "queued")
            assert ok, "未被领走"
            # 守护进程现在监视「稳定打开路径」，写入必须打这条路径才会触发回传
            ws_s = [w for w in d.watches if w.tid == tid_s]
            assert ws_s, "守护进程未建立 watch（稳定路径打开失败）"
            png_s = ws_s[0].files[0]
            try:
                # 阶段 1：大文件（>16MB，落到 16-64MB 档 -> 兜底上限 6s）分块写入，
                # 全程 ~1.6s << 6s 兜底上限，因此停笔稳定后才 finalize，绝不拦腰截断。
                # （2026-10-16 P1-C：尺寸分档兜底只针对「持续变化超预期」；>16MB 慢写
                # 属正常大文件保存，稳定判定优先，半写回传为回归红线。）
                CHUNK = b"Y" * (2 * 1024 * 1024)   # 2MB/块
                chunks = [CHUNK for _ in range(9)]  # 18MB
                expected = b"".join(chunks)
                done = _threading.Event()

                def _writer():
                    try:
                        with open(png_s, "r+b") as fh:
                            fh.seek(0)
                            fh.truncate()
                            for c in chunks:
                                fh.write(c)
                                fh.flush()
                                os.fsync(fh.fileno())
                                time.sleep(0.1)
                    finally:
                        done.set()

                th = _threading.Thread(target=_writer)
                th.start()
                t0 = time.time()
                while not done.is_set() and time.time() - t0 < 5.0:
                    d.cycle()
                    d.advance_watches()
                    assert not any((x.get("options") or {}).get(
                        "return_of") == tid_s for x in all_transfers()), \
                        "持续写入期间被 finalize（半写回传）"
                    time.sleep(0.03)
                assert done.is_set(), "写入线程异常退出"
                # 停笔稳定 settle 后才 finalize，且回传完整内容
                ok2 = run_cycles(d, lambda: (
                    transfer(tid_s)["status"] == "done"
                    and len([x for x in all_transfers()
                             if (x.get("options") or {}).get(
                                 "return_of") == tid_s]) == 1), timeout=5)
                assert ok2, "停笔稳定后未 finalize"
                back = [x for x in all_transfers()
                        if (x.get("options") or {}).get("return_of") == tid_s][0]
                bdir = Path(req("GET", "/config")["payload_dir"]) / back["id"]
                data = (bdir / "T_SlowSave.png").read_bytes()
                assert data == expected, \
                    "回传内容不是完整内容: {0!r}".format(data[:40])

                # 阶段 2：同长度覆写（size 不变、只有 mtime 变），同样必须
                # 等停笔——覆盖 (mtime, size) 双采样里的 mtime 分支
                alt = [b"A" * len(expected), b"B" * len(expected)]
                done2 = _threading.Event()

                def _writer2():
                    try:
                        with open(png_s, "r+b") as fh:
                            for c in alt:
                                fh.seek(0)
                                fh.write(c)
                                fh.flush()
                                os.fsync(fh.fileno())
                                time.sleep(0.1)
                    finally:
                        done2.set()

                th2 = _threading.Thread(target=_writer2)
                th2.start()
                t0 = time.time()
                while not done2.is_set() and time.time() - t0 < 5.0:
                    d.cycle()
                    d.advance_watches()
                    assert len([x for x in all_transfers()
                                if (x.get("options") or {}).get(
                                    "return_of") == tid_s]) == 1, \
                        "同长度覆写期间被 finalize（旧版本内容被回传）"
                    time.sleep(0.03)
                assert done2.is_set(), "覆写线程异常退出"
                ok3 = run_cycles(d, lambda: (
                    len([x for x in all_transfers()
                         if (x.get("options") or {}).get(
                             "return_of") == tid_s]) == 2), timeout=5)
                assert ok3, "同长度覆写停笔后未 finalize"
                backs = [x for x in all_transfers()
                         if (x.get("options") or {}).get("return_of") == tid_s]
                bdir = Path(req("GET", "/config")["payload_dir"]) / backs[0]["id"]
                data = (bdir / "T_SlowSave.png").read_bytes()
                assert data == b"B" * len(expected), \
                    "回传的不是最终覆写内容: {0!r}".format(data[:40])
                return ("大文件(18MB)分块写 ~1.6s 0 次误 finalize；"
                        "同长度覆写（仅 mtime 变）同样等停笔；回传内容完整")
            finally:
                pass

        check("P1-3 settle 双采样：慢速分块写入不拦腰截断", _settle_dual_sampling)

        # -- 8c. 2026-10-16 P1-C：settle 尺寸分档兜底（小文件持续写入必发布）--
        # 验收：小文件（<256KB）持续写入永不稳定 -> 在尺寸分档兜底上限（0.8s）
        # 发布，避免「迟迟不稳定就永远不回传」。大文件半写风险单独由 8b 覆盖。
        def _settle_fallback_small_file():
            rec = {"id": "tf", "to": {"app": "sai2"}, "from": {"app": "ue"},
                   "asset_name": "F", "options": {}}
            f = str(state_dir / "tf_small.png")
            w = mod.Watch(rec, [f], None)
            w.baseline = w.last_snap = {"tf_small.png": (0.0, 0)}
            # 持续写入：每次采样 size 都变大（始终 < 256KB），永不稳定
            gi = {"i": 0}
            def fake():
                gi["i"] += 1
                return {"tf_small.png": (float(gi["i"]), 1000 + gi["i"] * 500)}
            w.snapshot = fake
            t = 0.0
            res = None
            for _ in range(20):
                res = w.advance(t)
                t += 0.15
                if res == "finalize":
                    break
            assert res == "finalize", "持续写入未在尺寸兜底上限发布"
            assert 0.8 - 1e-6 <= t <= 1.2, \
                "<256KB 档应在 ~0.8s 兜底发布，实际 t={0}".format(t)
            assert w.settle_max_wait == 0.8, w.settle_max_wait
            return "小文件(<256KB)持续写入 -> ~0.8s 兜底发布（settle_max_wait=0.8）"
        check("P1-C settle 尺寸分档兜底：小文件持续写入兜底发布", _settle_fallback_small_file)

        # -- 8d. 2026-10-16 P1-A'：自适应轮询间隔 --------------------------------
        # 验收：无 watch -> 2.0s（默认，CPU ~0.38%）；有活跃 PS/SAI2 watch（未写入）
        # -> 0.5s；有 Blender watch -> 2.0s（只对 ps/sai 加速）；正在 settle
        # （dirty_names 非空）-> 0.15s（settle 细粒度采样）。
        def _adaptive_poll_interval():
            d_poll = mod.Daemon(cfg, interval=mod.POLL_INTERVAL_SEC,
                                once=False, verbose=False)
            d_poll.watches = []
            assert abs(d_poll._current_poll_interval() - mod.POLL_INTERVAL_SEC) < 1e-9, \
                d_poll._current_poll_interval()
            assert d_poll._poll_reason() == "idle"
            # 有 PS 活跃 watch（无待发布变更）-> 0.5s
            wps = mod.Watch({"id": "tp", "to": {"app": "photoshop"},
                             "from": {"app": "ue"}, "asset_name": "A",
                             "options": {}}, [str(state_dir / "tp.png")], None)
            d_poll.watches = [wps]
            assert abs(d_poll._current_poll_interval() - mod.FAST_POLL_SEC) < 1e-9, \
                d_poll._current_poll_interval()
            assert d_poll._poll_reason() == "active_watch"
            # 有 Blender 活跃 watch -> 2.0s（不加速）
            wb = mod.Watch({"id": "tb", "to": {"app": "blender"},
                            "from": {"app": "ue"}, "asset_name": "B",
                            "options": {}}, [str(state_dir / "tb.png")], None)
            d_poll.watches = [wb]
            assert abs(d_poll._current_poll_interval() - mod.POLL_INTERVAL_SEC) < 1e-9, \
                d_poll._current_poll_interval()
            # 正在 settle（dirty_names 非空，任意 app）-> 0.15s
            wps.dirty_names = ["tp.png"]
            d_poll.watches = [wps]
            assert abs(d_poll._current_poll_interval() - mod.SETTLE_SAMPLE_SEC) < 1e-9, \
                d_poll._current_poll_interval()
            assert d_poll._poll_reason() == "settling"
            return "无watch=2.0 / PS活跃=0.5 / Blender=2.0 / settle=0.15"
        check("P1-A' 自适应轮询间隔（无watch=2.0 / PS=0.5 / Blender=2.0 / settle=0.15）",
              _adaptive_poll_interval)

        # -- 8e. 2026-10-16 P1-C：写入后停笔 -> ~SETTLE_STABLE_SEC 内发布 -------
        # 验收：模拟「写一次后停笔」，应稳定 SETTLE_STABLE_SEC（~0.3s）后发布
        # （远快于旧 SETTLE_SEC=1.5s），且走的是「稳定时长」分支而非尺寸兜底。
        def _settle_stable_publish():
            w = mod.Watch({"id": "ts", "to": {"app": "sai2"},
                           "from": {"app": "ue"}, "asset_name": "S",
                           "options": {}}, [str(state_dir / "ts.png")], None)
            w.baseline = w.last_snap = {"ts.png": (1000.0, 100)}
            base = {"ts.png": (1000.0, 100)}
            changed = {"ts.png": (2000.0, 200)}
            # 未变：复位
            w.snapshot = lambda: dict(base)
            assert w.advance(0.0) is None, "初始未变不应 finalize"
            # 写一次（变更被采样）
            w.snapshot = lambda: dict(changed)
            assert w.advance(0.10) is None, "变更采样后即刻不应 finalize"
            assert w.settle_start_mono is not None and w.stable_since_mono is None
            # 停笔首个不变采样落在 0.25s（stable_since_mono=0.25）；
            # 连续稳定达到 SETTLE_STABLE_SEC（0.3s）才发布 -> 0.25 + 0.30 = 0.55s
            r1 = w.advance(0.25)
            assert r1 is None, "仅稳定 150ms 不应 finalize"
            r2 = w.advance(0.55)
            assert r2 == "finalize", "停笔 ~0.3s 未发布（实际 {0}）".format(r2)
            assert w.finalize_at > 0
            assert w.last_settle_reason == "stable", \
                "应走稳定时长分支，实际 {0}".format(w.last_settle_reason)
            return "写入后停笔 -> ~0.3s 内发布（稳定时长分支，非尺寸兜底，settle_reason=stable）"
        check("P1-C settle 自适应：写入后停笔 ~0.3s 内发布", _settle_stable_publish)

        # -- 8f. 2026-10-16 用户纠正：PS tmp->rename 保存 -> 立即发布 ----------
        # 验收：snapshot 元信息含 ctime_ns，rename（文件被新文件取代 -> ctime 变）
        # 视为「保存完成（原子）」，跳过稳定等待立即 finalize，save->detect 压到
        # 一次采样（~0.15s，< 0.3s）；settle_reason=rename。
        def _settle_rename_publish():
            w = mod.Watch({"id": "trn", "to": {"app": "photoshop"},
                          "from": {"app": "ue"}, "asset_name": "R",
                          "options": {}}, [str(state_dir / "trn.png")], None)
            # 元信息 = (mtime_ns, size, ctime_ns)
            base = {"trn.png": (1000, 100, 5000)}
            w.baseline = w.last_snap = dict(base)
            w.snapshot = lambda: dict(base)
            assert w.advance(0.0) is None, "初始未变不应 finalize"
            # rename 完成：新文件取代（mtime/size/ctime 全变，ctime 不同 = 替换）
            renamed = {"trn.png": (2000, 200, 9999)}
            w.snapshot = lambda: dict(renamed)
            r = w.advance(0.15)
            assert r == "finalize", "rename 后应立即 finalize（实际 {0}）".format(r)
            assert w.last_settle_reason == "rename", w.last_settle_reason
            assert w.finalize_at > 0
            return "PS tmp->rename 保存 -> 立即发布（settle_reason=rename, ~0.15s < 0.3s）"
        check("P1-C settle rename 检测：tmp->rename 立即发布", _settle_rename_publish)

        # -- 8f'. 2026-10-17 用户真机反馈修正（2026-10-18 落地为「watcher 仅唤醒 +
        # mtime/size 双采样 settle 自适应窗口」）：PS/SAI 是**原地覆盖写**（非
        # tmp->rename），ctime_ns 不变 -> rename 分支在真机上永不触发。判定改由
        # mtime/size 双采样 settle 负责：WinDirWatcher（ReadDirectoryChangesW）只在
        # 每次写入后即时唤醒主循环（paced_wait），并把写入事件流喂给「自适应稳定窗口」
        # （记录相邻写入最大间隔，要求连续稳定 >= max(SETTLE_STABLE_SEC, gap*SAFETY)）。
        # 关键：watcher **绝不**直接 finalize——单纯「最后写入距今 >= 阈值」在分块保存
        # （块间隔受 fsync 抖动拉到 0.13~0.37s+）会把「块间间隔」误判成「保存完成」，
        # 回传半写文件（真机 e2e returns=[1,1] 证伪）。本单测用桩 watcher 注入最后写入
        # 时刻，验证：原地覆盖写 -> **停笔稳定后**才 finalize（reason=stable）。
        def _settle_watcher_publish():
            class FakeWatcher:
                available = True
                def __init__(self):
                    self.t = 0.0
                def get_last_change(self, path):
                    return self.t
            fw = FakeWatcher()
            w = mod.Watch({"id": "tw", "to": {"app": "photoshop"},
                           "from": {"app": "ue"}, "asset_name": "W",
                           "options": {}}, [str(state_dir / "tw.png")], None, fw)
            base = {"tw.png": (1000, 100, 5000)}      # (mtime_ns, size, ctime_ns)
            changed = {"tw.png": (2000, 200, 5000)}   # ctime 不变（原地覆盖写）
            w.baseline = w.last_snap = dict(base)
            w.snapshot = lambda: dict(base)
            assert w.advance(0.0) is None, "初始未变不应 finalize"
            w.snapshot = lambda: dict(changed)
            fw.t = 0.20                                # 最后写入时刻（watcher 报告）
            # 写入进行中（本周期检测到变化）-> 重置稳定计时，绝不 finalize
            r = w.advance(0.25)
            assert r is None, "写入进行中（changed）不应 finalize（实际 {0}）".format(r)
            assert w.last_settle_reason != "watcher", "watcher 绝不直接驱动 finalize"
            # 停笔后连续稳定达到 SETTLE_STABLE_SEC -> finalize（reason=stable）
            r = w.advance(0.60)   # 停笔首拍：stable_since_mono 起算
            assert r is None, "刚停笔不应立即 finalize（实际 {0}）".format(r)
            r = w.advance(0.95)   # 0.95-0.60=0.35s >= SETTLE_STABLE_SEC
            assert r == "finalize", "停笔稳定后应 finalize（实际 {0}）".format(r)
            assert w.last_settle_reason == "stable", w.last_settle_reason
            assert w.finalize_at > 0
            return ("PS/SAI 原地覆盖写（ctime 不变）：mtime/size 双采样 settle 停笔稳定 "
                    ">= {0}s 后 finalize（settle_reason=stable；watcher 仅唤醒）".format(
                        mod.SETTLE_STABLE_SEC))
        check("P1-C settle Windows 监听：原地覆盖写停笔稳定后 finalize", _settle_watcher_publish)

        # -- 8f''. 2026-10-17 用户真机证伪回归锁：FS_WATCH_FILTER 必须含
        # FILE_NOTIFY_CHANGE_LAST_WRITE(0x10) 与 SIZE(0x08)。上一版写成
        # 0x1|0x2（FILE_NAME|DIR_NAME），漏了 LAST_WRITE -> PS/SAI2 原地覆盖写
        # 触发的 ACTION_MODIFIED(3) 永远收不到 -> 全走 stable。此静态断言在任何
        # 平台都能跑，直接锁死该回归（本机实测 / 沙箱均 0 成本）。
        def _watcher_filter_has_lastwrite():
            f = mod.FS_WATCH_FILTER
            FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
            FILE_NOTIFY_CHANGE_SIZE = 0x00000008
            FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
            assert f & FILE_NOTIFY_CHANGE_FILE_NAME, "filter 缺 FILE_NAME"
            assert f & FILE_NOTIFY_CHANGE_LAST_WRITE, \
                "filter 漏 LAST_WRITE(0x10)：原地覆盖写触发的 ACTION_MODIFIED 收不到"
            assert f & FILE_NOTIFY_CHANGE_SIZE, "filter 缺 SIZE(0x08)"
            return "FS_WATCH_FILTER=0x{0:x} 含 FILE_NAME|SIZE|LAST_WRITE（原地写/rename 全覆盖）".format(f)
        check("P1-C 回归锁：FS_WATCH_FILTER 含 LAST_WRITE", _watcher_filter_has_lastwrite)

        # -- 8f'''. 2026-10-17 补强回归锁：主循环不得因高频写入忙等（每秒数千轮
        # cycle -> 冲刷中转服务 HTTP、拖垮 claim）。paced_wait 必须强制相邻周期
        # >= MIN_CYCLE_SEC，即使 _changed 被持续置位立即唤醒。
        def _paced_wait_throttles_busyloop():
            w = mod.WinDirWatcher()
            w._changed.set()  # 模拟高频写入：变更事件持续置位 -> wait 立即返回
            slept = {}
            seq = {"n": 0}

            def fake_mono():
                seq["n"] += 1
                return 0.0 if seq["n"] <= 1 else 0.005  # c0=0.0，wait 后立即取 = 0.005

            def fake_sleep(sec):
                slept["v"] = sec

            old_mono, old_sleep = mod.time.monotonic, mod.time.sleep
            mod.time.monotonic = fake_mono
            mod.time.sleep = fake_sleep
            try:
                w.paced_wait(5.0)
            finally:
                mod.time.monotonic = old_mono
                mod.time.sleep = old_sleep
            assert "v" in slept, "paced_wait 未执行限频 sleep（busy-loop 未被限频）"
            assert slept["v"] >= mod.MIN_CYCLE_SEC - 0.02, \
                "paced_wait 未强制最小周期：slept={0}".format(slept["v"])
            return "paced_wait 限频 OK（高频写入下仍 >= MIN_CYCLE_SEC={0}s）".format(
                mod.MIN_CYCLE_SEC)
        check("P1-C 回归锁：paced_wait 强制最小周期（防 busy-loop 拖垮 claim）",
              _paced_wait_throttles_busyloop)

        # -- 8g. 2026-10-17 用户质询：REUSE_GRACE_SEC 实际值与真机 25s 矛盾 ----
        # 验收：常数 REUSE_GRACE_SEC=3.0，计时起点 = proc_exited_at（窗口**关闭**
        # 的单调时刻，由 advance 内 proc.poll() 首次非 None 时写入）。
        # 非 delegated watch、returns>0、dirty 为空时：宽限内（<3.0s）返回 None，
        # 超宽限返回 "clear"（只清 watch，不改传输状态）。
        # 真机日志 18:36:20 reopen -> 18:36:45 clear 的 25s 解释：reopen 是「重新
        # 打开窗口」（窗口处于打开态，proc 活着、proc_exited_at=None，宽限不计时），
        # 用户 22s 后关窗 -> proc_exited_at 写入 -> 3s 宽限后 clear。即 3s 是从**关窗**
        # 起算，25s = 窗口打开时长(22s) + 宽限(3s)。clear 日志现已打印实测间隔
        # (now - proc_exited_at) 供真机核对。delegated（单实例助手秒退）watch 的
        # 关窗复用由 WATCH_IDLE_SEC=600 兜底，但本分支对普通关闭同样适用。
        def _reuse_grace_constant():
            w = mod.Watch({"id": "trg", "to": {"app": "photoshop"},
                          "from": {"app": "ue"}, "asset_name": "G",
                          "options": {}}, [str(state_dir / "trg.png")], None)
            w.returns = 1                 # 已成功回传
            w.delegated = False
            w.proc_exited_at = 10.0       # 单调时钟起点
            w.dirty_names = []
            assert abs(mod.REUSE_GRACE_SEC - 3.0) < 1e-9, mod.REUSE_GRACE_SEC
            # 宽限内（2.9s）：保持 None（不翻 done、不清 watch）
            assert w.advance(12.9) is None, "REUSE_GRACE 宽限内不应 clear"
            assert w.returns == 1, "宽限内不应翻写 returns/传输状态"
            # 超宽限（3.1s）：clear（只清 watch，不翻 done->cancelled）
            assert w.advance(13.1) == "clear", "超 REUSE_GRACE 应 clear"
            return "REUSE_GRACE_SEC=3.0，计时起点 proc_exited_at；宽限内 None、超宽限 clear"
        check("REUSE_GRACE_SEC=3.0 实值与宽限行为（桩测断言）", _reuse_grace_constant)

        # -- 9. 回归护栏：周期内不得对被监视文件 O_RDWR 打开 -----------------
        # _file_locked 已删（O_RDWR 打开实测干扰 SAI2）。守护进程对 payload
        # 只应 stat/读拷贝，任何 os.open 都说明锁探测被重新引入。
        def _no_ordrw():
            import os as _os
            O_RDWR = getattr(_os, "O_RDWR", 2)
            hits = []
            real_open = _os.open

            def _spy(path, flags, *a, **k):
                try:
                    p = str(path)
                except Exception:
                    p = repr(path)
                # 只记录「以读写方式」打开被监视文件——这正是已删除的 _file_locked
                # 锁探测的特征（shutil.copy2 读源文件是 O_RDONLY，不应命中）。
                if "T_NoOrdrw" in p and (flags & O_RDWR):
                    hits.append((p, flags))
                return real_open(path, flags, *a, **k)

            _os.open = _spy
            try:
                t, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                         name="T_NoOrdrw")
                ok = run_cycles(d, lambda: transfer(t)["status"] != "queued")
                assert ok, "未被领走"
                # 守护进程监视「稳定打开路径」，保存必须打这条路径才触发回传
                live = [w for w in d.watches if w.tid == t]
                assert live, "守护进程未建立 watch（稳定路径打开失败）"
                Path(live[0].files[0]).write_bytes(b"no-ordrw-edited")
                ok2 = run_cycles(d, lambda: transfer(t)["status"] == "done",
                                 timeout=5)
                assert ok2, "保存后未回传"
            finally:
                _os.open = real_open
            assert not hits, "守护进程以读写方式 os.open 了被监视文件: {0}".format(hits)
            return "claim->保存->回传全程 0 次 O_RDWR 打开（锁探测已移除）"

        check("回归护栏：不对被监视文件做 O_RDWR 锁探测", _no_ordrw)

        # -- 10. 挂机 soak：同文件反复「发送->保存->回传」40 轮无泄漏 --------
        # 状态机类改动最容易引入新泄漏：P0-1 阻塞级修复后，同内容连发只首轮
        # 开 1 个窗口、后续重发一律 superseded（跳过 Popen、复用首窗口）。
        # 轮轮断言同文件 watch 恒 1 条、无 claimed 悬挂。首轮真正保存回传 1 次，
        # 后续 39 轮只 superseded，不产生新回传（不互杀、不泄漏）。
        def _soak():
            # 回退分支键只认主文件名（不含 sha256）：同文件连发恒定同键 ->
            # 轮轮复用同一窗口（首轮真正开窗口，后续一律 superseded）。
            key = ("sai2", "unreal", ("file_name", "t_soak.png"))
            n = 40
            t0 = time.time()
            first_tid = None
            for i in range(n):
                t, _, _ = publish_texture(tmp, "sai2", from_app="unreal",
                                         name="T_Soak")
                ok = run_cycles(d, lambda tt=t: transfer(tt)["status"] != "queued",
                                timeout=5)
                assert ok, "第 {0} 轮未被领走".format(i)
                ws = [w for w in d.watches if w.dedup_key() == key]
                assert len(ws) == 1, "第 {0} 轮后同文件 watch={1} 条（泄漏）".format(
                    i, len(ws))
                stable = ws[0].files[0]
                if i == 0:
                    # 首轮：真实开窗口 + 保存 -> 回传 return_of=首轮 tid
                    first_tid = t
                    Path(stable).write_bytes("soak-0".encode())
                    ok2 = run_cycles(d, lambda tt=t: (
                        transfer(tt)["status"] == "done"
                        and len([x for x in all_transfers()
                                 if (x.get("options") or {}).get(
                                     "return_of") == tt]) == 1), timeout=5)
                    assert ok2, "首轮未产生恰好 1 条回传"
                    assert ws[0].returns == 1, ws[0].returns
                else:
                    # 后续轮：命中存活窗口，跳过 Popen，superseded 到首窗口
                    # 不产生新回传；新传输 done + superseded_by=首 tid
                    ok3 = run_cycles(d, lambda tt=t: transfer(tt)["status"] == "done",
                                    timeout=5)
                    assert ok3, "第 {0} 轮未收尾（superseded）".format(i)
                    r = transfer(t)
                    assert (r.get("result") or {}).get("superseded_by") == first_tid, \
                        "第 {0} 轮未 superseded 到首窗口: {1}".format(i, r.get("result"))
                # 本测试涉及的传输不得挂在 claimed（首轮保存后 done、后续 superseded）
                assert not [x["id"] for x in claimed_transfers()
                            if x["id"] in (t, first_tid)], \
                    "第 {0} 轮出现 claimed 悬挂: {1}".format(
                        i, [x["id"] for x in claimed_transfers()])
            # 收尾：编辑器进程退出 -> 宽限期后 abandon -> watch 清空。
            # 注意 _FakeProc 的退出码在构造时冻结，spy.exit_code 只影响之后
            # 新建的进程——这里直接改现有 watch 的 proc 模拟「编辑器关闭」。
            for w in d.watches:
                if w.dedup_key() == key and w.proc is not None:
                    w.proc._code = 1
            ok4 = run_cycles(d, lambda: not [
                w for w in d.watches if w.dedup_key() == key], timeout=5)
            assert ok4, "进程退出后 watch 未清空"
            return ("{0} 轮同文件重发：仅首轮开 1 窗口（回传 1 条），"
                    "其余 superseded；watch 恒 1 条、无 claimed 悬挂，耗时 {1:.1f}s".format(
                        n, time.time() - t0))

        check("挂机 soak：40 轮同文件往返无泄漏", _soak)

        # -- 11. _pid_alive：zombie（已退出未回收）必须判死 ------------------
        # 2026-10-14：旧实现只凭 OpenProcess 句柄能打开就判活——已退出但
        # 父进程尚未回收的进程句柄照样打得开，导致 --stop 干等 20s 超时、
        # 旧实例变 zombie 后新实例被「已有实例在运行」挡住。
        def _zombie_dead():
            import ctypes as _ctypes
            if not hasattr(_ctypes, "windll"):
                return "非 Windows 平台，跳过"
            p = orig_popen([sys.executable, "-c", "pass"])
            pid = p.pid
            t0 = time.time()
            while time.time() - t0 < 5.0 and p.poll() is None:
                time.sleep(0.05)
            assert p.poll() is not None, "子进程没有退出"
            # 此刻进程对象仍在（Popen 还持着句柄未回收）——旧实现判「活着」
            alive = mod._pid_alive(pid)
            p.wait()
            assert alive is False, "zombie 被误判为活着（旧 bug 复现）"
            p2 = orig_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                assert mod._pid_alive(p2.pid) is True, "活进程被误判为死"
            finally:
                p2.kill()
                p2.wait()
            return "zombie->False；活进程->True"

        check("_pid_alive：退出码判定（zombie 不误判）", _zombie_dead)

        # -- 7. --stop 子命令（未运行时返回 0） ------------------------------
        def _stop_idle():
            rc = mod.do_stop(cfg)
            assert rc == 0, "未运行时应返回 0，实际 {0}".format(rc)
            return "rc=0"

        check("--stop 对未运行的守护返回 0", _stop_idle)

        # -- 12. P0（2026-10-16）：歧义默认按 app 分保守化 ----------------------
        # 用户 P0：旧「歧义默认重开」对 SAI2 会重新引入「是否放弃当前编辑」弹窗
        # （A→B→A 多标签：后台标签画布是隐藏 MDI 子窗口，_collect 跳过 IsWindowVisible
        # =False 的窗口 -> 枚举不到 A 的标题 -> 落歧义 -> 旧默认重开 -> Popen -> 弹窗）。
        # 修复：歧义默认按 app 分——PS 重开（用户实机验证良性聚焦）、SAI2 keep
        # （不重开、不弹窗）。本用例用桩模拟窗口枚举（无 GUI 会话），驱动**真实**的
        # 决策函数（不覆盖 _is_file_open_in_editor），验证 AMBIGUOUS_DEFAULT 逻辑；
        # 真实枚举行为（SAI2 后台标签是否在窗口树里）以 [win-probe] 日志在实机核对。
        def _ambiguity_per_app():
            state3 = tmp / "state3"
            state3.mkdir(parents=True, exist_ok=True)
            d3, d3cfg = make_daemon(mod, state3)
            # 桩：模拟 A→B→A —— A 是后台隐藏标签（枚举不到，只有活动标签 B 可见）。
            # 这正是「旧默认重开」会误判的歧义输入：titles 非空但不含目标文件 a.png。
            def _enum_b_only(exe):
                return (["b.png"], [1])

            d3._enumerate_editor_windows = _enum_b_only
            # SAI2 歧义 -> keep（不重开、绝不 Popen 弹窗）。这是 P0 修复的核心断言。
            assert d3._is_file_open_in_editor("sai2", "fake_sai2.exe", "a.png") is True, \
                "SAI2 歧义应默认 keep（不重开、不弹窗）"
            # PS 歧义 -> reopen（用户实机验证良性聚焦，不弹窗不重复打开）。
            assert d3._is_file_open_in_editor("photoshop", "fake_ps.exe", "a.png") is False, \
                "PS 歧义应默认 reopen（benign 聚焦）"
            # 正向：活动标签 B 命中 -> open（两 app 都视为开着，走聚焦去重分支）。
            assert d3._is_file_open_in_editor("sai2", "fake_sai2.exe", "b.png") is True, \
                "活动标签命中应视为开着（SAI2）"
            assert d3._is_file_open_in_editor("photoshop", "fake_ps.exe", "b.png") is True, \
                "活动标签命中应视为开着（PS）"
            # reuse_ambiguous 可针对单 app 覆盖默认（只作逃生口；默认值已正确）。
            d3cfg["reuse_ambiguous"] = {"sai2": "reopen"}
            assert d3._is_file_open_in_editor("sai2", "fake_sai2.exe", "a.png") is False, \
                "reuse_ambiguous 覆盖 sai2=reopen 应生效"
            d3cfg["reuse_ambiguous"] = {"photoshop": "keep"}
            assert d3._is_file_open_in_editor("photoshop", "fake_ps.exe", "a.png") is True, \
                "reuse_ambiguous 覆盖 ps=keep 应生效"
            return ("SAI2 歧义默认 keep（不弹窗）；PS 歧义默认 reopen（良性）；"
                    "活动标签命中=open；reuse_ambiguous 单 app 覆盖生效")
        check("P0（2026-10-16）：歧义默认按 app 分保守化（SAI2 keep / PS reopen）",
              _ambiguity_per_app)

        # -- 13. P0（2026-10-16 实机）：SAI2 复用分支「聚焦」本身触发弹窗 --------
        # 用户实测两场景：① 发 A→关 A 窗口→再发 A（走 reopen）→ 正常开，不弹窗；
        # ② 发 A→发 B→切到 A/B→再发 A（走复用+仅聚焦）→ SAI2 弹「放弃当前编辑
        # 重新打开」。daemon.log 铁证：复用 + 仅聚焦（无 Popen）也弹窗。根因：SAI2
        # 把外部进程 SetForegroundWindow 聚焦到已打开文件当成重开请求（Popen 不是
        # 唯一触发器）。修复：SAI2 复用分支完全不碰窗口（REUSE_FOCUS_ON_OPEN["sai2"]
        # ="noop"），只更新 last_event 反馈；PS 保持聚焦（用户实测多标签正常）。
        def _p0_sai2_reuse_noop_focus():
            focus_calls = []
            orig_focus = d._focus_editor_window
            d._focus_editor_window = lambda *a, **k: focus_calls.append(a)

            def _run(app):
                focus_calls.clear()
                d._is_file_open_in_editor = lambda *a, **k: True  # 模拟窗口开着
                t0, _, _ = publish_texture(tmp, app, from_app="unreal", name="T_FocusX")
                assert run_cycles(d, lambda tt=t0: transfer(tt)["status"] != "queued"), \
                    "{0} 首次发送未被领走".format(app)
                t1, _, _ = publish_texture(tmp, app, from_app="unreal", name="T_FocusX")
                assert run_cycles(d, lambda tt=t1: transfer(tt)["status"] != "queued"), \
                    "{0} 复用发送未被领走".format(app)
                r = transfer(t1)
                assert r["status"] == "done", "{0} 复用未 done: {1}".format(app, r["status"])
                assert (r.get("result") or {}).get("superseded_by") == t0, \
                    "{0} 复用未 superseded 到首窗口".format(app)
                for w in list(d.watches):
                    if w.tid in (t0, t1) and w.proc is not None:
                        w.proc._code = 1
                run_cycles(d, lambda: not any(w.tid in (t0, t1) for w in d.watches), timeout=5)
                return len(focus_calls)

            n_sai2 = _run("sai2")
            assert n_sai2 == 0, \
                "SAI2 复用分支不应调用聚焦（会触发弹窗），实际聚焦 {0} 次".format(n_sai2)
            n_ps = _run("photoshop")
            assert n_ps >= 1, \
                "PS 复用分支应保持聚焦，实际聚焦 {0} 次".format(n_ps)
            d._focus_editor_window = orig_focus
            return "SAI2 复用聚焦 0 次（不碰窗口）/ PS 复用聚焦>=1 次（保持）"

        check("P0（2026-10-16）：SAI2 复用分支不碰窗口（聚焦 0 次）；PS 保持聚焦",
              _p0_sai2_reuse_noop_focus)

        # -- 14b. 2026-10-17 新增需求：focus_on_open 开关控制 PS/SAI2 前台聚焦 ----
        # 默认关闭：SAI2 复用分支保持 noop（不碰窗口，保护不弹确认框）。
        # 开启时：无视 REUSE_FOCUS_ON_OPEN 的 noop 策略，强制聚焦（含首开后台重试）。
        def _focus_on_open_switch():
            import time as _t
            state_fo = tmp / "state_fo"
            state_fo.mkdir(parents=True, exist_ok=True)
            d_fo, cfg_fo = make_daemon(mod, state_fo)
            cfg_fo["focus_on_open"] = True
            focus_calls = []
            d_fo._focus_editor_window = lambda *a, **k: focus_calls.append(a)
            # SAI2 默认 noop，但开关开启应强制聚焦
            r = d_fo._reuse_focus_if_allowed("sai2", cfg_fo["apps"]["sai2"], "x.obj")
            assert r is True, "focus_on_open 开启时 SAI2 应强制聚焦"
            assert len(focus_calls) >= 1, "focus_on_open 开启时未调用聚焦"
            # 关闭开关 -> SAI2 回到 noop（不聚焦）
            cfg_fo["focus_on_open"] = False
            focus_calls.clear()
            r = d_fo._reuse_focus_if_allowed("sai2", cfg_fo["apps"]["sai2"], "x.obj")
            assert r is False, "focus_on_open 关闭时 SAI2 应 noop"
            assert len(focus_calls) == 0, "focus_on_open 关闭时不应聚焦 SAI2"
            # 首开后台聚焦：开启时应在短时间内调用至少一次
            cfg_fo["focus_on_open"] = True
            focus_calls.clear()
            d_fo._focus_after_open("photoshop", cfg_fo["apps"]["photoshop"], "x.obj")
            _t.sleep(0.8)   # 后台线程重试 ~1.5s，取前段
            assert len(focus_calls) >= 1, "focus_on_open 开启时首开应后台聚焦"
            return "focus_on_open 开关：开启强制聚焦 SAI2 + 首开后台聚焦；关闭恢复 SAI2 noop"
        check("新增（2026-10-17）：focus_on_open 开关控制 PS/SAI2 前台聚焦",
              _focus_on_open_switch)

        # -- 14. status.json 写失败降级（重试 3 次 + 不抛异常）-----------------
        # 用户实机 daemon.log：PermissionError(13, '拒绝访问。') 写 status.json（杀软
        # 锁文件 / 托盘并发读）。修复：_atomic_write_json 加重试（3 次 × 100ms，临时
        # 文件用 mkstemp 唯一名避免并发写冲突），write_status 捕获后降级为日志，绝不
        # 抛异常影响主循环。
        def _status_write_degrade():
            state_st = tmp / "state_st"
            state_st.mkdir(parents=True, exist_ok=True)
            d2, _ = make_daemon(mod, state_st)
            real_replace = os.replace
            calls = {"n": 0}

            def fake_replace(src, dst):
                calls["n"] += 1
                raise PermissionError(13, "拒绝访问。")

            logs = []
            orig_log = d2.log_rl
            d2.log_rl = lambda *a, **k: logs.append(a)
            os.replace = fake_replace
            try:
                d2.write_status()   # 不应抛异常
            finally:
                os.replace = real_replace
                d2.log_rl = orig_log
            assert calls["n"] == 3, \
                "应重试 3 次 os.replace，实际 {0}".format(calls["n"])
            assert any("重试 3 次仍失败" in str(x) for x in logs), \
                "应记录降级日志: {0}".format(logs)
            return "status.json 写失败重试 3 次 + 降级日志，主循环不受影响"

        check("status.json 写失败：重试 3 次 + 降级日志，不抛异常",
              _status_write_degrade)

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
