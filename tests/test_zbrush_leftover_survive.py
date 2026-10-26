#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：import_max_per_click 上限留下的未消费任务，daemon 超时后不得删除。

场景（要求 二）：UE 连发 5 个，ZBrush 单次「接收」受 import_max_per_click=3 限制只处理 3 个，
剩 2 个 json 留在 inbox。daemon 在 _zb_advance_pending 里对「无回执到达」的任务执行
os.remove(p["task"])（ZB_IMPORT_MAX_RENEWALS × ZB_IMPORT_TIMEOUT_SEC 后）。本测试验证：
只要任务文件仍在 inbox（未消费），daemon 必须续期、不删、不 cancel，使「剩 2 个留 inbox
下次再收」成为事实，而非 15 分钟后被静默删除。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAEMON = ROOT / "tools" / "editor_daemon.py"
_spec = importlib.util.spec_from_file_location("editor_daemon_ls", str(DAEMON))
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


def test_leftover_survive():
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "state")
    payload = os.path.join(tmp, "payload")
    os.makedirs(state)
    os.makedirs(payload)
    cfg = {"server": "http://127.0.0.1:1", "state_dir": state}
    d = mod.Daemon(cfg, interval=1.0, once=True)
    d.payload_dir = payload
    mod.zbrush_process_running = lambda: True

    captured = {}

    def fake_safe(method, path, body=None, **kw):
        captured["path"] = path
        return {"transfer": captured.get("record")}

    d.safe = fake_safe

    inbox = os.path.join(payload, "zbrush_inbox")
    os.makedirs(inbox, exist_ok=True)
    tids = []
    for i in range(5):
        tid = "L{0}".format(i)
        f = os.path.join(payload, "m{0}.obj".format(i))
        with open(f, "wb") as fh:
            fh.write(b"x")
        rec = {"id": tid, "kind": "models", "from": {"app": "ue"},
               "asset_name": "x", "_files_abs": [f]}
        captured["record"] = rec
        d.handle_zbrush(rec, "models")
        tids.append(tid)

    check("daemon 投递 5 个任务（zb_pending=5）", len(d.zb_pending) == 5,
          "len={0}".format(len(d.zb_pending)))

    # 插件只消费前 3 个（写回执），后 2 个留在 inbox（不写回执，模拟 import_max_per_click 上限）
    ab_done = os.path.join(inbox, "done")
    os.makedirs(ab_done, exist_ok=True)
    for tid in tids[:3]:
        with open(os.path.join(ab_done, tid + ".done.json"), "w", encoding="utf-8") as fh:
            json.dump({"transfer_id": tid, "status": "ok", "imported": ["m.obj"]}, fh)

    # 反复触发超时（把 deadline 设到过去），远超 ZB_IMPORT_MAX_RENEWALS
    for _ in range(10):
        for p in d.zb_pending:
            p["deadline"] = -1.0
        d._zb_advance_pending()

    consumed = [t for t in tids[:3] if not any(p["tid"] == t for p in d.zb_pending)]
    remaining = [t for t in tids[3:] if any(p["tid"] == t for p in d.zb_pending)]
    check("消费的 3 个任务已离开 zb_pending", len(consumed) == 3, "consumed={0}".format(consumed))
    check("剩 2 个任务仍在 zb_pending（未被 cancel）", len(remaining) == 2,
          "remaining={0}".format(remaining))
    for tid in tids[3:]:
        check("未消费任务文件 {0}.json 仍在 inbox（未被 daemon 删）".format(tid),
              os.path.isfile(os.path.join(inbox, tid + ".json")),
              "missing " + tid)


def main() -> int:
    print("== ZBrush 剩余任务不被 daemon 超时删除 回归测试 ==")
    test_leftover_survive()
    print("\n结果: 通过 {0} / 失败 {1}".format(len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项: " + "、".join(FAILED))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
