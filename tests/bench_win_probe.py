#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口探测（_enumerate_editor_windows / _is_file_open_in_editor）性能基准。

回答用户 P1 质询「daemon 每 2s 轮询，每条 watch 都枚举窗口多少 ms」：
  1) 先纠正前提：探测**不在轮询周期里跑**——只在「重发命中复用分支」时调用
     一次（editor_daemon.py handle() 复用分支内），每次重发事件至多 1 次探测。
  2) 本基准实测单次探测耗时（真实 Windows API：进程快照 + EnumWindows
     + EnumChildWindows），并模拟「5 条 watch 各探测一次」的总耗时。
  3) 对照组：空转周期成本（tests/bench_daemon_cycle.py，约 7.7ms）。

用法： python tests/bench_win_probe.py
"""
import os
import sys
import time
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

DAEMON = ROOT / "tools" / "editor_daemon.py"


def load_daemon():
    spec = importlib.util.spec_from_file_location("ab_editor_daemon", str(DAEMON))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if sys.platform != "win32":
        print("仅 Windows 有意义")
        return 1
    mod = load_daemon()
    d = mod.Daemon.__new__(mod.Daemon)   # 不跑 __init__（探测只用 log_rl/cfg）
    d.cfg = {}
    # 用一个真实存在但通常未运行的 exe（找不到进程 -> 快路径），
    # 以及 python.exe（进程存在、窗口少）两个场景各测一轮。
    import shutil
    targets = [
        ("not-running.exe（进程不存在快路径）",
         r"C:\Windows\System32\not-running-bridge-bench.exe"),
        ("python.exe（进程存在、少量窗口）",
         sys.executable),
    ]
    N = 200
    print("单次探测 = 进程快照 + EnumWindows + EnumChildWindows（真实 API）")
    for label, exe in targets:
        ts = []
        for _ in range(N):
            t0 = time.perf_counter()
            d._enumerate_editor_windows(exe)
            ts.append((time.perf_counter() - t0) * 1000.0)
        ts.sort()
        avg = sum(ts) / len(ts)
        p95 = ts[int(len(ts) * 0.95)]
        print("  {0}: avg={1:.2f}ms p95={2:.2f}ms max={3:.2f}ms (n={4})".format(
            label, avg, p95, ts[-1], N))
        print("    -> 5 条 watch 各探测 1 次 ≈ {0:.1f}ms（且这是每次「重发事件」"
              "各 1 次，不是每 2s 周期成本）".format(avg * 5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
