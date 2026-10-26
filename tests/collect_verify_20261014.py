#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总 2026-10-14 P0/P1 修复证据：聚焦 diff + 各测试原始输出。"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "runtime" / "verify" / "2026-10-14_daemon_p0_verify.txt"
PY = r"C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe"

DAEMON_MARKERS = [
    "watch 去重", "dedup", "dup_key", "_file_locked", "STILL_ACTIVE",
    "GetExitCodeProcess", "detect_at", "done_at", "wall clock",
    "WATCH_IDLE_SEC = 600", "finalize_at", "_should_expire", "_watch_keep",
]
TEST_MARKERS = [
    "claimed_transfers", "去重", "soak", "O_RDWR", "zombie", "_pid_alive",
    "ordrw", "NoOrdrw",
]


def run(cmd):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def focused_diff(path, markers):
    raw = run(["git", "diff", "-U2", path])
    if not raw.strip():
        return "(无 diff)\n"
    out, hunk, keep = [], [], False
    for line in raw.splitlines():
        if line.startswith("@@"):
            if hunk and keep:
                out.extend(hunk)
            hunk, keep = [line], False
            continue
        if hunk or line.startswith("diff ") or line.startswith("index ") \
                or line.startswith("--- ") or line.startswith("+++ "):
            hunk.append(line)
            if any(m in line for m in markers):
                keep = True
    if hunk and keep:
        out.extend(hunk)
    if not out:
        return "(本轮无匹配 hunk)\n"
    # 只保留文件头 + 匹配 hunk
    head = [l for l in raw.splitlines()
            if l.startswith(("diff ", "index ", "--- ", "+++ "))]
    return "\n".join(head + out) + "\n"


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    parts.append("=" * 72)
    parts.append("AssetBridge 2026-10-14 P0 watch 泄漏 / P1 _file_locked / P1 timing 修复证据")
    parts.append("仓库: E:\\CirnoWorkSpace\\AssetBridge   生成时间: 见文件 mtime")
    parts.append("=" * 72)

    parts.append("\n\n########## 1. tools/editor_daemon.py 聚焦 diff（本轮改动） ##########\n")
    parts.append(focused_diff("tools/editor_daemon.py", DAEMON_MARKERS))

    parts.append("\n\n########## 2. tests/test_editor_daemon.py 聚焦 diff（本轮新增测试） ##########\n")
    parts.append(focused_diff("tests/test_editor_daemon.py", TEST_MARKERS))

    parts.append("\n\n########## 3. 单测：test_editor_daemon.py（13 项） ##########\n")
    parts.append(run([PY, "tests/test_editor_daemon.py"]))

    parts.append("\n\n########## 4. e2e：真实子进程 daemon（7 项） ##########\n")
    parts.append(run([PY, "tests/e2e_editor_daemon.py"]))

    parts.append("\n\n##ib########## 5. 回归：test_obj_flow.py（11 项） ##########\n".replace("##ib", "###"))
    parts.append(run([PY, "tests/test_obj_flow.py"]))

    parts.append("\n\n########## 6. 轮询周期耗时实测（P2 评估依据） ##########\n")
    parts.append(run([PY, "tests/bench_daemon_cycle.py"]))

    OUT.write_text("\n".join(parts), encoding="utf-8")
    print("written: {0} ({1} bytes)".format(OUT, OUT.stat().st_size))


if __name__ == "__main__":
    main()
