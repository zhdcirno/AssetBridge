#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 评估辅助：实测 daemon 单个轮询周期耗时（决定 POLL_INTERVAL 能否降）。

回答用户问题「POLL_INTERVAL_SEC 从 2.0s 降到 1.0s 是否可行」：
周期耗时 x 意味着 1/interval 的 CPU 占用上界。分别测：
  A. 空转（无 watch 无传输）：ensure_session + GET /config + 3 个 app 的 inbox
  B. 带 5 条 watch（快照/比对成本，模拟多文件会话）
"""
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import server as bridge_server  # noqa: E402
import urllib.request  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ab_bench_"))
    port = free_port()
    base = "http://127.0.0.1:{0}".format(port)
    threading.Thread(target=bridge_server.run_server,
                     args=(tmp, "127.0.0.1", port), daemon=True).start()
    assert wait_port(port), "服务没起来"

    spec = importlib.util.spec_from_file_location(
        "ab_editor_daemon", str(ROOT / "tools" / "editor_daemon.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    state = tmp / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = {"server": base, "state_dir": state,
           "apps": {"photoshop": str(state / "a.exe"),
                    "sai2": str(state / "b.exe"),
                    "zbrush": str(state / "c.exe")}}
    for p in cfg["apps"].values():
        Path(p).write_bytes(b"MZ")
    d = mod.Daemon(cfg, interval=2.0)

    # 预热（建 session 等一次性成本）
    d.cycle()
    N = 100
    t0 = time.perf_counter()
    for _ in range(N):
        d.cycle()
    idle_ms = (time.perf_counter() - t0) / N * 1000.0

    # B: 造 5 条 watch（假 proc + 假文件，快照成本真实产生）
    class _P:
        def poll(self):
            return None

    payload = tmp / "payloads" / "bench"
    payload.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        f = payload / ("T_Bench{0}.png".format(i))
        f.write_bytes(b"x" * 4096)
        rec = {"id": "bench{0}".format(i), "asset_name": "T_Bench{0}".format(i),
               "to": {"app": "sai2"}, "from": {"app": "unreal"},
               "options": {}}
        d.watches.append(mod.Watch(rec, [str(f)], _P()))

    t0 = time.perf_counter()
    for _ in range(N):
        d.advance_watches()
    watch_ms = (time.perf_counter() - t0) / N * 1000.0

    print(json.dumps({
        "idle_cycle_ms": round(idle_ms, 2),
        "watch_cycle_ms_5watches": round(watch_ms, 2),
        "cpu_pct_at_1s_interval": round((idle_ms + watch_ms) / 1000.0 * 100, 2),
        "cpu_pct_at_2s_interval": round((idle_ms + watch_ms) / 2000.0 * 100, 2),
        "req_per_cycle": 5,
        "req_per_sec_at_1s": 5,
        "req_per_sec_at_2s": 2.5,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
