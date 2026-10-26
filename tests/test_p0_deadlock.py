"""P0 死锁修复回归测试（无头，零 pip，直接 `python tests/test_p0_deadlock.py`）。

覆盖 2026-10-18 的死锁修复：
  1. cycle() 单记录处理异常必须被隔离——不能拖垮整轮轮询，坏记录要被 error 收尾，
     好记录仍正常处理（否则后续任务进不来，ZBrush 接收扫描 n=0）。
  2. handle_zbrush 投递阶段（zb_write_task 等）抛异常必须兜底清理：已 claim 的传输
     被 error 收尾、不残留 zb_pending 条目（否则永久 claimed 卡死后续任务）。
"""
import os
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import tools.editor_daemon as mod  # noqa: E402


def _mk_daemon(state_dir: Path, apps: dict):
    for p in apps.values():
        Path(p).write_bytes(b"MZ-fake")
    cfg = {"server": "http://127.0.0.1:1", "state_dir": str(state_dir), "apps": apps}
    return mod.Daemon(cfg, interval=0.2, once=False, verbose=False), cfg


def test_cycle_isolates_bad_record():
    tmp = Path(tempfile.mkdtemp(prefix="ab_p0_cycle_"))
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    d, _ = _mk_daemon(state_dir, {"photoshop": str(state_dir / "ps.exe")})

    bad_tid, good_tid = "tid_bad", "tid_good"
    Path(state_dir / "a.png").write_bytes(b"x")
    Path(state_dir / "b.png").write_bytes(b"x")
    bad_rec = {"id": bad_tid, "to": {"app": "photoshop"}, "kind": "textures",
               "from": {"app": "blender"}, "_files_abs": [str(state_dir / "a.png")]}
    good_rec = {"id": good_tid, "to": {"app": "photoshop"}, "kind": "textures",
                "from": {"app": "blender"}, "_files_abs": [str(state_dir / "b.png")]}

    handled = []

    def fake_handle(rec):
        if rec["id"] == bad_tid:
            raise RuntimeError("boom")
        handled.append(rec["id"])

    d.handle = fake_handle
    d.ensure_session = lambda: True

    finished = []
    d._claim_finish = lambda t, s, r: finished.append((t, s))

    def fake_safe(method, path, body=None):
        if method == "GET" and path.startswith("/inbox"):
            return {"transfers": [bad_rec, good_rec]}
        return {}

    d.safe = fake_safe

    # 关键：cycle 不得向外抛异常（单记录异常被隔离）
    try:
        d.cycle()
    except Exception as exc:  # pragma: no cover
        raise AssertionError("cycle() 因单记录异常而崩溃: {0!r}".format(exc))

    assert good_tid in handled, "好记录应被正常处理"
    assert any(t == bad_tid and s == "error" for t, s in finished), \
        "坏记录应被 error 收尾（隔离 + 收尾）"


def test_handle_zbrush_cleanup_on_write_failure():
    tmp = Path(tempfile.mkdtemp(prefix="ab_p0_zb_"))
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = tmp / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    d, _ = _mk_daemon(state_dir, {"zbrush": str(state_dir / "zb.exe")})
    d.payload_dir = str(payload)
    d.ensure_session = lambda: True

    tid = "tid_zb_bad"
    rec = {"id": tid, "to": {"app": "zbrush"}, "kind": "models",
           "from": {"app": "unreal"}, "asset_name": "char",
           "_files_abs": [str(payload / tid / "char.obj")]}

    def fake_safe(method, path, body=None):
        if method == "POST" and "/claim" in path:
            return {"transfer": {"id": tid, "_files_abs": rec["_files_abs"]}}
        return {}

    d.safe = fake_safe

    finished = []
    d._claim_finish = lambda t, s, r: finished.append((t, s))

    orig = mod.zb_write_task

    def boom(*a, **k):
        raise OSError("disk full")

    mod.zb_write_task = boom
    try:
        d.handle(rec)
    finally:
        mod.zb_write_task = orig

    assert any(t == tid and s == "error" for t, s in finished), \
        "投递异常应被兜底为 error"
    assert not any(p.get("tid") == tid for p in d.zb_pending), \
        "异常不应残留 zb_pending 条目（彻底清理上下文）"


def main() -> int:
    tests = [test_cycle_isolates_bad_record, test_handle_zbrush_cleanup_on_write_failure]
    failed = 0
    for t in tests:
        try:
            t()
            print("[OK]   {0}".format(t.__name__))
        except AssertionError as e:
            failed += 1
            print("[FAIL] {0}: {1}".format(t.__name__, e))
        except Exception as e:  # pragma: no cover
            failed += 1
            print("[ERR]  {0}: {1!r}".format(t.__name__, e))
    print("\nP0 死锁回归: {0} 通过 / {1} 失败".format(len(tests) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
