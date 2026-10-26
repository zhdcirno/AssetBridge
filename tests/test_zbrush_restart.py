#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZBrush「Bridge 重启但 ZBrush 保持运行」场景回归测试（2026-10-18 补充）。

用户真机：完整重启 Bridge（中转守护）后 ZBrush 依然无法接收。根因是中转服务
（server.py）持久化 transfer 状态，旧实例 claim 过但还没收尾的传输会永久卡在
claimed，新实例 /inbox 不再返回它；而新实例内存里的 zb_pending / _zb_delivered
随进程销毁。若仅靠内存状态，就会出现「claimed 永不终结」死锁。

本测试覆盖 _zb_rehydrate（守护启动/接管时从磁盘恢复上一实例遗留的 ZBrush 投递状态）：
  · 场景 A（已导入）：上一实例已写任务文件 + ZBrush 已导入并产生回执 -> 重启后
    新实例必须把孤儿回执重放收尾（server 侧 claimed 传输正常 done），且不把已消费
    任务文件重新登记（不重复投递、不重复导入）。
  · 场景 B（未点击）：上一实例已写任务文件但 ZBrush 还没点 -> 重启后新实例必须把
    孤儿任务文件重新登记进 zb_pending，继续等回执；ZBrush 随后点击导入 -> 正常收尾。
  · 场景 C（claim 后崩溃，无磁盘产物）：上一实例 claim 成功（server 已持久化
    claimed）但在写 inbox 任务文件前崩溃——磁盘上既无任务文件也无回执。这是真机
    「完整重启后依然收不到」的最隐蔽根因：步骤 1/2 都抓不到，/inbox 永不返回它。
    重启后新实例必须把孤儿 claimed 复活 -> queued，后续 cycle 才能重新投递。

用法： python tests/test_zbrush_restart.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.parse as _urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# —— 加载守护（可 monkeypatch）——
DAEMON = ROOT / "tools" / "editor_daemon.py"
_spec = importlib.util.spec_from_file_location("editor_daemon_zr", str(DAEMON))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  ✅ {0}".format(name))
    else:
        FAILED.append(name)
        print("  ❌ {0}  {1}".format(name, extra))


# ---------------------------------------------------------------------------
# 假中转服务：模拟 server.py 持久化 transfer 状态（含 claimed 查询 / retry 复活）
# ---------------------------------------------------------------------------
class FakeServer:
    def __init__(self):
        # tid -> {"id","status","to":{...}}；模拟 server.py 落盘持久化
        self.transfers = {}
        self.finish_calls = []  # [(tid, status)]
        self.retry_calls = []   # [tid]

    def safe(self, method, path, body=None, **kw):
        # GET /transfers?status=claimed  -> 返回 claimed 列表（inbox 不返回的关键）
        if method == "GET" and path.startswith("/transfers?"):
            q = _urlparse.urlparse(path).query
            params = _urlparse.parse_qs(q)
            st = params.get("status", [""])[0]
            if st:
                return {"transfers": [t for t in self.transfers.values() if t["status"] == st]}
            return {"transfers": list(self.transfers.values())}
        # POST /claim
        if method == "POST" and path.endswith("/claim"):
            tid = path.rsplit("/transfers/", 1)[-1].rsplit("/claim", 1)[0]
            t = self.transfers.setdefault(tid, {"id": tid, "status": "queued", "to": {}})
            t["status"] = "claimed"
            return {"transfer": dict(t)}
        # POST /finish
        if method == "POST" and path.endswith("/finish"):
            tid = path.rsplit("/transfers/", 1)[-1].rsplit("/finish", 1)[0]
            st = (body or {}).get("status", "done")
            if tid in self.transfers:
                self.transfers[tid]["status"] = st
            self.finish_calls.append((tid, st))
            return {"transfer": self.transfers.get(tid, {"id": tid, "status": st})}
        # POST /retry  -> 复活孤儿 claimed 回 queued
        if method == "POST" and path.endswith("/retry"):
            tid = path.rsplit("/transfers/", 1)[-1].rsplit("/retry", 1)[0]
            if tid in self.transfers:
                self.transfers[tid]["status"] = "queued"
                self.transfers[tid].pop("claimed_by", None)
                self.transfers[tid]["result"] = {}
            self.retry_calls.append(tid)
            return {"transfer": self.transfers.get(tid, {"id": tid, "status": "queued"})}
        return {"transfer": {}}


def _new_daemon(payload):
    cfg = {
        "server": "http://127.0.0.1:1",
        "state_dir": os.path.join(payload, "..", "state"),
        # _zb_rehydrate 步骤 3 需要 apps 来判断 claimed 传输归谁负责
        "apps": ["zbrush", "blender", "maya", "ue", "photoshop", "sai2"],
    }
    d = mod.Daemon(cfg, interval=1.0, once=True)
    d.payload_dir = payload
    mod.zbrush_process_running = lambda: True   # 走 inbox 文件握手
    return d


def _deliver(d, tid, f, from_app="ue"):
    rec = {"id": tid, "kind": "models", "from": {"app": from_app},
           "asset_name": tid, "_files_abs": [f]}
    d.handle_zbrush(rec, "models")


def _zbrush_import(inbox, tid):
    """模拟 ZBrush 点击「导入待收模型」：写回执 + 把任务文件消费进 done/。"""
    done = os.path.join(inbox, "done")
    os.makedirs(done, exist_ok=True)
    with open(os.path.join(done, tid + ".done.json"), "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": tid, "status": "ok", "imported": [tid + ".obj"]}, fh)
    tp = os.path.join(inbox, tid + ".json")
    if os.path.isfile(tp):
        os.replace(tp, os.path.join(done, tid + ".json"))


# ---------------------------------------------------------------------------
# 场景 A：上一实例已投递 + ZBrush 已导入 -> 重启后孤儿回执重放收尾，不重复投递
# ---------------------------------------------------------------------------
def test_restart_after_imported():
    tmp = tempfile.mkdtemp()
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload)
    inbox = os.path.join(payload, "zbrush_inbox")
    os.makedirs(inbox)

    # 旧实例 A 投递 3 个任务
    dA = _new_daemon(payload)
    srvA = FakeServer()
    dA.safe = srvA.safe
    files = []
    for i in range(3):
        f = os.path.join(payload, "m{0}.obj".format(i))
        with open(f, "wb") as fh:
            fh.write(b"x")
        files.append(f)
        _deliver(dA, "RA{0}".format(i), f)

    check("A-投递后 inbox 顶层 3 个任务文件",
          sorted(x for x in os.listdir(inbox) if x.endswith(".json")) ==
          ["RA0.json", "RA1.json", "RA2.json"],
          sorted(x for x in os.listdir(inbox) if x.endswith(".json")))

    # ZBrush 没重启，继续运行：点击导入全部 3 个（写回执 + 消费任务文件）
    for i in range(3):
        _zbrush_import(inbox, "RA{0}".format(i))

    check("A-ZBrush 导入后 inbox 顶层干净（仅 done/）",
          all(not x.endswith(".json") for x in os.listdir(inbox) if x != "done"),
          list(os.listdir(inbox)))
    check("A-done/ 含 3 张回执", all(
        os.path.isfile(os.path.join(inbox, "done", "RA{0}.done.json".format(i))) for i in range(3)))

    # ===== 模拟 Bridge（守护）重启，ZBrush 保持运行 =====
    dB = _new_daemon(payload)         # 全新内存状态：zb_pending=[] / _zb_delivered=set()
    srvB = FakeServer()
    dB.safe = srvB.safe
    check("B-重启后内存 zb_pending 初始为空", dB.zb_pending == [], "len={0}".format(len(dB.zb_pending)))

    dB._zb_rehydrate()                # 关键：从磁盘恢复上一实例遗留状态

    # 孤儿回执应被重放收尾：3 个传输都被 finish -> done
    check("B-孤儿回执重放：3 个传输全部 finish(done)",
          sorted(t for t, s in srvB.finish_calls) == ["RA0", "RA1", "RA2"] and
          all(s == "done" for _, s in srvB.finish_calls),
          srvB.finish_calls)
    check("B-重放后 zb_pending 仍为空（已消费任务不重复登记）",
          dB.zb_pending == [], "len={0}".format(len(dB.zb_pending)))
    check("B-重放后 done/ 回执已清理", not any(
        x.endswith(".done.json") for x in os.listdir(os.path.join(inbox, "done"))))
    check("B-去重集合不含已收尾 tid（不会重复投递）",
          not any("RA{0}".format(i) in dB._zb_delivered for i in range(3)))


# ---------------------------------------------------------------------------
# 场景 B：上一实例已投递但 ZBrush 还没点 -> 重启后孤儿任务文件重新登记，等回执
# ---------------------------------------------------------------------------
def test_restart_before_click():
    tmp = tempfile.mkdtemp()
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload)
    inbox = os.path.join(payload, "zbrush_inbox")
    os.makedirs(inbox)

    dA = _new_daemon(payload)
    srvA = FakeServer()
    dA.safe = srvA.safe
    for i in range(3):
        f = os.path.join(payload, "n{0}.obj".format(i))
        with open(f, "wb") as fh:
            fh.write(b"x")
        _deliver(dA, "RB{0}".format(i), f)

    check("B-ZBrush 还没点：inbox 顶层有 3 个待收任务",
          sorted(x for x in os.listdir(inbox) if x.endswith(".json")) ==
          ["RB0.json", "RB1.json", "RB2.json"])

    # ===== 模拟 Bridge 重启，ZBrush 仍然运行（任务文件还在 inbox，未被消费）=====
    dB = _new_daemon(payload)
    srvB = FakeServer()
    dB.safe = srvB.safe
    dB._zb_rehydrate()

    check("B-重启后孤儿任务文件被重新登记进 zb_pending（3 条）",
          len(dB.zb_pending) == 3, "len={0}".format(len(dB.zb_pending)))
    check("B-重新登记的 tid 正确",
          sorted(p["tid"] for p in dB.zb_pending) == ["RB0", "RB1", "RB2"])
    check("B-重新登记带任务文件路径（handshake 模式）",
          all(os.path.isfile(p["task"]) for p in dB.zb_pending))

    # ZBrush 此时才点击导入（进程一直没关）
    for i in range(3):
        _zbrush_import(inbox, "RB{0}".format(i))
    dB._zb_advance_pending()

    check("B-ZBrush 点击后 zb_pending 清空", dB.zb_pending == [], "len={0}".format(len(dB.zb_pending)))
    check("B-3 个传输全部 finish(done)",
          sorted(t for t, s in srvB.finish_calls) == ["RB0", "RB1", "RB2"] and
          all(s == "done" for _, s in srvB.finish_calls), srvB.finish_calls)


# ---------------------------------------------------------------------------
# 场景 C：claim 成功但守护在「写任务文件」前崩溃 -> 无磁盘产物 -> 孤儿 claimed 复活
# 这正是用户「完整重启 Bridge 和 中转服务后依然收不到」的最隐蔽根因。
# ---------------------------------------------------------------------------
def test_restart_orphan_claimed():
    tmp = tempfile.mkdtemp()
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload)
    inbox = os.path.join(payload, "zbrush_inbox")
    os.makedirs(inbox)

    srv = FakeServer()
    # 模拟 server.py 持久化的传输记录：RC0 已被上一实例 claim（status=claimed），
    # to.app=zbrush（由 sender 发布时确定）。这正是崩溃后的服务端落盘状态。
    srv.transfers["RC0"] = {"id": "RC0", "status": "claimed", "to": {"app": "zbrush"}}

    # ===== 模拟 Bridge（守护）重启，ZBrush 保持运行 =====
    dB = _new_daemon(payload)
    dB.safe = srv.safe
    check("C-重启前 inbox 顶层无任务文件（崩溃发生在写文件前）",
          not any(x.endswith(".json") for x in os.listdir(inbox) if x != "done"),
          list(os.listdir(inbox)))
    check("C-服务端 RC0 仍为 claimed（/inbox 不会返回它，死锁起点）",
          srv.transfers["RC0"]["status"] == "claimed")

    dB._zb_rehydrate()   # 关键：复活孤儿 claimed

    check("C-孤儿 claimed 被复活 -> queued（死锁解除，/inbox 可重新返回）",
          srv.transfers["RC0"]["status"] == "queued", srv.transfers["RC0"])
    check("C-rehydrate 显式调用了 retry 复活端点", "RC0" in srv.retry_calls, srv.retry_calls)
    check("C-复活后 zb_pending 仍为空（等 cycle 重新投递，不本地伪造）",
          dB.zb_pending == [], "len={0}".format(len(dB.zb_pending)))
    check("C-复活后去重集合不含 RC0（允许后续 cycle 重新 claim）",
          "RC0" not in dB._zb_delivered)

    # 模拟后续正常 cycle：/inbox 现在能返回 RC0，守护重新投递并写任务文件，
    # ZBrush 点击导入后正常收尾。
    rec = {"id": "RC0", "kind": "models", "from": {"app": "ue"},
           "asset_name": "RC0", "_files_abs": [os.path.join(payload, "m.obj")]}
    with open(os.path.join(payload, "m.obj"), "wb") as fh:
        fh.write(b"x")
    dB.handle_zbrush(rec, "models")
    check("C-复活后重新投递：inbox 顶层出现 RC0.json",
          os.path.isfile(os.path.join(inbox, "RC0.json")))
    _zbrush_import(inbox, "RC0")
    dB._zb_advance_pending()
    check("C-重新交付后 RC0 正常 finish(done)",
          ("RC0", "done") in srv.finish_calls, srv.finish_calls)


def main() -> int:
    print("== ZBrush「Bridge 重启 / ZBrush 保活」场景回归测试 ==")
    test_restart_after_imported()
    test_restart_before_click()
    test_restart_orphan_claimed()
    print("\n结果: 通过 {0} / 失败 {1}".format(len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项: " + "、".join(FAILED))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
