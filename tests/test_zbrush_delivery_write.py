"""P0 投递死锁排查（无头，零 pip）：验证 UE->ZBrush 传输能否被守护真实写出 inbox 任务文件。

直接驱动生产链 cycle() -> handle() -> handle_zbrush() -> zb_write_task()，
仅 mock HTTP 层（safe），文件写入是真实 IO。证明：在「传输进入守护」的前提下，
inbox 任务文件一定落盘到 <payload_dir>/zbrush_inbox/<tid>.json。

若本测试通过、但真机 import_pending 仍 n=0，则根因必在「传输根本没进入守护」
（服务端 /inbox?app=zbrush 没返回该传输）或「路径/配置不一致」——
由 editor_daemon.py 新增的 [zb-inbox-poll]/[handle_zbrush] 诊断日志在真机定位。
"""
import os
import sys
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import tools.editor_daemon as mod  # noqa: E402


def _mk_daemon(state_dir: Path, apps: dict):
    for p in apps.values():
        Path(p).write_bytes(b"MZ-fake")
    cfg = {"server": "http://127.0.0.1:1", "state_dir": str(state_dir),
           "apps": apps}
    return mod.Daemon(cfg, interval=0.2, once=False, verbose=False), cfg


def test_ue_to_zbrush_writes_inbox_file():
    tmp = Path(tempfile.mkdtemp(prefix="ab_zb_write_"))
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = tmp / "payload"
    payload.mkdir(parents=True, exist_ok=True)

    d, _ = _mk_daemon(state_dir, {"zbrush": str(state_dir / "zb.exe")})
    d.payload_dir = str(payload)
    d.ensure_session = lambda: True

    tid = "ue2zb_001"
    src_obj = payload / tid / "char.obj"
    src_obj.parent.mkdir(parents=True, exist_ok=True)
    src_obj.write_bytes(b"v 0 0 0\n")  # 真实文件，files 校验必须通过

    # 模拟一条 UE -> ZBrush 的待收传输
    zb_rec = {"id": tid, "to": {"app": "zbrush"}, "kind": "models",
              "from": {"app": "unreal"}, "asset_name": "char",
              "_files_abs": [str(src_obj)]}

    finished = []
    d._claim_finish = lambda t, s, r: finished.append((t, s))

    def fake_safe(method, path, body=None):
        if method == "GET" and path.startswith("/inbox"):
            # 只有 zbrush 这一路有任务；ps/sai2 返回空
            if "app=zbrush" in path:
                return {"transfers": [zb_rec]}
            return {"transfers": []}
        if method == "POST" and "/claim" in path:
            return {"transfer": {"id": tid, "_files_abs": zb_rec["_files_abs"],
                                 "files": ["char.obj"], "asset_name": "char",
                                 "from": {"app": "unreal"}}}
        return {}

    d.safe = fake_safe

    # 跑一次真实 cycle（含 rehydrate/advance_watches/_zb_advance_pending 等全部环节）
    d.cycle()

    inbox = payload / "zbrush_inbox"
    task_file = inbox / (tid + ".json")
    assert inbox.is_dir(), "zbrush_inbox 目录应被创建"
    assert task_file.is_file(), \
        "UE->ZBrush 传输必须被守护写出 inbox 任务文件（否则真机 n=0 的根因在更上游）"

    data = json.loads(task_file.read_text(encoding="utf-8"))
    assert data["transfer_id"] == tid, "任务文件 transfer_id 应匹配"
    assert data["from_app"] == "unreal", "任务文件应记录来源 app"
    assert data["files"] == [str(src_obj)], "任务文件应记录源文件路径"
    assert any(t == tid for t, _ in finished) is False, \
        "成功投递不应调用 _claim_finish（等待 ZBrush 回执才 finish）"
    # 任务应进入 zb_pending 等待 ZBrush 回执
    assert any(p["tid"] == tid for p in d.zb_pending), "投递后应进入 zb_pending 等待回执"


def test_missing_payload_dir_silent_deadlock_has_diag():
    """回归：payload_dir 为空时，handle_zbrush 必须显式报错（诊断日志可见），
    而不是静默丢任务。"""
    tmp = Path(tempfile.mkdtemp(prefix="ab_zb_nopayload_"))
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    d, _ = _mk_daemon(state_dir, {"zbrush": str(state_dir / "zb.exe")})
    d.payload_dir = ""  # 故意留空
    d.ensure_session = lambda: True
    finished = []
    d._claim_finish = lambda t, s, r: finished.append((t, s))
    d.safe = lambda m, p, b=None: (
        {"transfer": {"id": "x", "_files_abs": []}} if "/claim" in p else {})

    d.handle_zbrush({"id": "x", "to": {"app": "zbrush"}, "kind": "models",
                    "from": {"app": "unreal"}, "_files_abs": []}, "models")

    assert any(t == "x" and s == "error" for t, s in finished), \
        "payload_dir 为空必须 error 收尾（并在日志打印原因，便于真机定位）"


if __name__ == "__main__":
    test_ue_to_zbrush_writes_inbox_file()
    test_missing_payload_dir_silent_deadlock_has_diag()
    print("ALL ZBRUSH DELIVERY WRITE TESTS PASSED")
