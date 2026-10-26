# -*- coding: utf-8 -*-
"""回归测试：ZBrush inbox 路径错配（payload 单数 vs payloads 复数）导致的「盲人摸象」bug。

2026-10-20 真机铁证：部署副本 ab_config.json 被写成单数 runtime\\payload，而 daemon
服务端写的是复数 runtime\\payloads。插件按单数目录扫一个不存在的目录，scan_inbox 静默
返回 []、import_pending 永远 n=0。本测试锁定两件事：
  1) scan_inbox 在真实（复数 payloads）目录能找到任务；
  2) _resolve_inbox_path 在配置写成单数误配时能自愈到复数目录。
"""
import json
import os
import sys
import tempfile
import shutil

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "..", "plugins", "zbrush", "asset_bridge_zbrush")
sys.path.insert(0, os.path.abspath(PLUGIN_DIR))
import asset_bridge_zbrush as m  # noqa: E402


def _make_inbox(base, exist_payloads=True):
    """在 base/payloads/zbrush_inbox 放一个合法任务（单数 payload 目录不建）。"""
    inbox = os.path.join(base, "payloads", "zbrush_inbox")
    os.makedirs(inbox, exist_ok=True)
    tid = "deadbeef000000000000000000000000"
    with open(os.path.join(inbox, tid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({
            "transfer_id": tid,
            "asset_name": "verify",
            "from_app": "unreal",
            "files": ["x:/nope.obj"],
            "at": "2026-10-20T00:00:00+08:00",
        }, fh)
    return inbox, tid


def test_scan_inbox_finds_task_in_plural_payloads():
    base = tempfile.mkdtemp()
    try:
        inbox, tid = _make_inbox(base)
        tasks, skipped = m.scan_inbox(inbox)
        assert len(tasks) == 1, (tasks, skipped)
        assert tasks[0][1]["transfer_id"] == tid
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_inbox_self_heals_singular_to_plural():
    base = tempfile.mkdtemp()
    try:
        _make_inbox(base)  # 只建复数 payloads/...；单数 payload/... 不存在
        bad = {
            "payload_dir": os.path.join(base, "payload"),
            "inbox_dir": os.path.join(base, "payload", "zbrush_inbox"),
        }
        path, note = m._resolve_inbox_path(bad)
        assert os.path.isdir(path), "自愈未定位到真实 inbox"
        assert path == os.path.join(base, "payloads", "zbrush_inbox")
        assert note, "应当产生自愈 note"
        # 自愈后仍能用 scan_inbox 找到任务
        assert len(m.scan_inbox(path)[0]) == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_inbox_uses_explicit_valid_path_without_heal():
    base = tempfile.mkdtemp()
    try:
        inbox, _ = _make_inbox(base)
        cfg = {
            "payload_dir": os.path.join(base, "payloads"),
            "inbox_dir": inbox,
        }
        path, note = m._resolve_inbox_path(cfg)
        assert path == inbox
        assert note == ""
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    test_scan_inbox_finds_task_in_plural_payloads()
    test_resolve_inbox_self_heals_singular_to_plural()
    test_resolve_inbox_uses_explicit_valid_path_without_heal()
    print("ALL INBOX-RESOLVE TESTS PASSED ✅")
