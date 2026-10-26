#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""editor_daemon 真实子进程端到端验证（2026-10-14 P0 watch 去重；2026-10-15 P0-1/P1-3 返工）。

与 test_editor_daemon.py（进程内 monkeypatch 驱动周期）互补：
这里把 tools/editor_daemon.py 当**真实子进程**跑起来——main loop、
pid 文件、status.json、--stop 全部走真实路径；中转服务是真实 HTTP
服务；「编辑器」是 pythonw 跑的一个小脚本（同时就是被监视的 payload
文件本体）：持打开着文件 -> 1s 后改写一次（保存）-> 2s 后再改写一次。

验证点（对应用户实机发现的 watch 泄漏 P0：连发 5 次堆了 5 条 watch）：
  1. 同一文件连发 5 次 -> status.json watching 里只剩 1 条（首 tid，复用窗口）
  2. 旧 4 条传输全部 done，server 无 claimed 悬挂
  3. 首传输两次保存 -> 恰好 2 条回传（return_of=首 tid），returns=2
  4. daemon.log 有 [watch-dedup] 证据行（>=4）
  5. [timing] save->detect 为合理正数（monotonic/wall clock 混用不复现）
  6. --stop 干净退出（rc=0，日志有「守护进程退出」）
  7. 不同目录同名文件（不同 asset_paths）-> 两条 watch 并存、都回传，
     内容完整（P0-1 主分支 + P1-3 settle 双采样）
  8. 回退分支（无 asset_paths，Blender/Maya 形态）：同名（不论内容）-> 复用
     1 窗口（首 tid 存活），重发一律 superseded（窗口开着 -> 只聚焦，绝不
     覆盖编辑内容、不动检测状态），首窗口回传不丢（P0-1 用户复核返工：旧
     「叠 sha256」在「改一版发一版」下不复用、又堆窗口，已修复）；「关窗
     再发必须重开（不静默丢发送）」分支由无头单测 _p0_reopen_when_closed
     （monkeypatch 窗口探测）覆盖；回退分支串台 = Blender/Maya 发送上线
     的前置条件（见 asset_identity TODO，当前无真实流量）
  9. 回退分支同内容连发 -> 仍去重成 1 条（首 tid），重发 superseded_by 首 tid
 10. 关窗重开分支（第 2 个 daemon 子进程，AB_EDITOR_OPEN_STATE=closed）：
     关窗后重发 -> 重发 done(message 含「重新打开」) + sim.log " run" 恰 2 条
     （Popen 真被再次执行的铁证）+ [watch-reopen] 日志 + watch 恒 1 + 2 条回传

注：daemon 子进程带 AB_EDITOR_OPEN_STATE=open 启动（见 launch 处注释）——
无头模拟无真实窗口，强制走 keep-open 分支使断言确定。

用法： python tests/e2e_editor_daemon.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import server as bridge_server  # noqa: E402

DAEMON = ROOT / "tools" / "editor_daemon.py"
PY = sys.executable
PYW = Path(sys.executable).with_name("pythonw.exe")

PASSED, FAILED = [], []

# 「编辑器」小脚本：既是进程本体，也是被监视的 payload 文件。
# 持打开着（真实编辑器都持有句柄）-> 到点改写两次（两次保存）。
SIM_BODY = b'''# e2e fake editor: process body == watched payload file
import os, time

def _log(msg):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.log")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{0:.3f} {1}\\n".format(time.time(), msg))

def main():
    me = os.path.abspath(__file__)
    marker = os.path.join(os.path.dirname(me), ".watch_opened")
    # Re-focus (same-name window focus) does not re-edit: when the daemon
    # re-focuses the same window, a real editor only focuses the existing
    # document, not re-save. marker simulates: first open truly saves; later
    # focus just exits (avoids "one more return per focus" making returns
    # uncontrollable in e2e).
    if os.path.exists(marker):
        _log("refocus")
        return
    with open(marker, "w") as fh:
        fh.write("1")
    _log("open")
    try:
        fh = open(me, "r+b")
        try:
            time.sleep(1.0)
            fh.seek(0); fh.truncate()
            fh.write(b"# sim save 1\\n"); fh.flush(); os.fsync(fh.fileno())
            _log("save1")
            time.sleep(2.0)
            fh.seek(0); fh.truncate()
            fh.write(b"# sim save 2 (a bit longer than the first)\\n")
            fh.flush(); os.fsync(fh.fileno())
            _log("save2")
            time.sleep(0.5)
        finally:
            fh.close()
            _log("close")
    except Exception as exc:
        _log("error {0!r}".format(exc))
        raise

main()
'''

# 大文件（>16MB）慢速分块写「编辑器」（P1-C 尺寸分档兜底验收）：持打开着 -> 1s 后
# 用 ~1.5s 分块写入自身（每 0.09s 一块 1MB + flush + fsync，size 持续增长，
# 总 ~17MB）-> 停笔。17MB 落到 16-64MB 档 -> 兜底上限 6s，远大于实际写入时长，
# 故必须等停笔稳定后才回传（半写回传为回归红线，P1-C 验收点）。
SLOW_CHUNKS = 17
SLOW_CHUNK_BYTES = 1024 * 1024
SLOW_EXPECTED = b"Y" * (SLOW_CHUNK_BYTES * SLOW_CHUNKS)
SIM_SLOW_BODY = b'''# e2e fake editor: large-file slow chunked save (P1-C no half-write)
import os, time

def _log(msg):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.log")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{0:.3f} {1}\\n".format(time.time(), msg))

def main():
    me = os.path.abspath(__file__)
    marker = os.path.join(os.path.dirname(me), ".watch_opened")
    if os.path.exists(marker):
        _log("refocus")
        return
    with open(marker, "w") as fh:
        fh.write("1")
    _log("open")
    try:
        fh = open(me, "r+b")
        try:
            time.sleep(1.0)
            fh.seek(0); fh.truncate()
            for i in range(17):
                fh.write(b"Y" * (1024 * 1024))
                fh.flush(); os.fsync(fh.fileno())
                time.sleep(0.09)
            _log("slow-save")
            time.sleep(0.5)
        finally:
            fh.close()
            _log("close")
    except Exception as exc:
        _log("error {0!r}".format(exc))
        raise

main()
'''


# 快速单保存「编辑器」（不同内容连发高频场景用）：持打开 -> 1s 后改写一次
# （一次保存 => 恰好 1 条回传）-> 再稳 2.5s 让 daemon 完成 settle+回传 -> 关闭。
# 比 SIM_BODY（两次保存）更快，控制 e2e 总时长 < 3 分钟。pass 标记不同 =>
# 内容字节不同 => payload sha256 不同 => 回退去重键不同（不同编辑会话不互杀）。
# 用 ###PASS### 占位符 + bytes.replace 生成（避免 .format 与编辑器内 {0:.3f}
# 字段冲突）。
SIM_ONCE_BODY = b'''# e2e fake editor: single fast save
import os, time

def _log(msg):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.log")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{0:.3f} {1}\\n".format(time.time(), msg))

def main():
    me = os.path.abspath(__file__)
    marker = os.path.join(os.path.dirname(me), ".watch_opened")
    # Re-focus (same-name window focus) does not re-edit: when the daemon
    # re-focuses the same window, a real editor only focuses the existing
    # document, not re-save. marker simulates: first open truly saves; later
    # focus just exits (avoids "one more return per focus" making returns
    # uncontrollable in e2e).
    if os.path.exists(marker):
        _log("refocus")
        return
    with open(marker, "w") as fh:
        fh.write("1")
    _log("open")
    try:
        fh = open(me, "r+b")
        try:
            time.sleep(1.0)
            fh.seek(0); fh.truncate()
            fh.write(b"# sim edit pass ###PASS###\\n"); fh.flush(); os.fsync(fh.fileno())
            _log("save1")
            time.sleep(2.5)
        finally:
            fh.close()
            _log("close")
    except Exception as exc:
        _log("error {0!r}".format(exc))
        raise

main()
'''

def _sim_once_body(marker: str) -> bytes:
    """生成第 marker 次编辑的「快速单保存」编辑器脚本（内容各异）。"""
    return SIM_ONCE_BODY.replace(b"###PASS###", str(marker).encode("utf-8"))


# 「编辑器」（关窗重开分支专用）：**没有 .watch_opened 守卫**——每次被 Popen
# 执行都追加一行 " run" 到 sim.log 并做一次保存。用于计数「Popen 真实执行
# 次数」：重开分支应恰好执行 2 次（首开 + 关窗重发后的重开）。
SIM_RUNMARK_BODY = b'''# e2e fake editor: run marker (counts real Popen executions)
import os, time

def _log(msg):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.log")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{0:.3f} {1}\\n".format(time.time(), msg))

def main():
    me = os.path.abspath(__file__)
    _log("run")
    try:
        fh = open(me, "r+b")
        try:
            time.sleep(1.0)
            fh.seek(0); fh.truncate()
            fh.write(b"# runmark edit\\n"); fh.flush(); os.fsync(fh.fileno())
            _log("save")
            time.sleep(2.0)
        finally:
            fh.close()
            _log("close")
    except Exception as exc:
        _log("error {0!r}".format(exc))
        raise

main()
'''


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


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=10, base=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(base + "/api/v1" + path, data=data,
                               headers=headers, method=method)
    with _OPENER.open(r, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def main() -> int:
    print("\n=== editor_daemon 真实子进程 e2e（P0 watch 去重）===\n")
    if not PYW.is_file():
        print("找不到 pythonw.exe: {0}".format(PYW))
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="ab_e2e_daemon_"))
    port = free_port()
    base = "http://127.0.0.1:{0}".format(port)
    t = threading.Thread(target=bridge_server.run_server,
                         args=(tmp, "127.0.0.1", port), daemon=True)
    t.start()
    if not wait_port(port):
        print("服务没能起来")
        return 1
    cfg = req("GET", "/config", base=base)
    print("服务: {0}  数据根: {1}".format(base, cfg.get("data_root")))

    state = tmp / "state"
    stdout_path = tmp / "daemon_stdout.txt"
    # AB_EDITOR_OPEN_STATE=open：本 e2e 的「编辑器」是无 GUI 的 python 小脚本，
    # 窗口探测找不到真实窗口会走「已关 -> 重开」分支；这里强制「开着」以
    # 确定性走 keep-open 去重分支（superseded 语义），与下方断言一致。
    # 「已关 -> 重开」分支由无头单测 _p0_reopen_when_closed（monkeypatch）覆盖。
    dproc = subprocess.Popen(
        [str(PYW), "-u", str(DAEMON),
         "--server", base,
         "--state-dir", str(state),
         "--sai2", str(PYW),       # 「编辑器」= pythonw（payload 是 .py 小脚本）
         "--interval", "0.5"],
        cwd=str(ROOT), close_fds=True,
        env=dict(os.environ, AB_EDITOR_OPEN_STATE="open"),
        stdout=open(stdout_path, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT)
    print("守护进程已拉起 pid={0}（pythonw, --interval 0.5）".format(dproc.pid))

    def read_status():
        try:
            return json.loads((state / "status.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def wait_status(pred, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = read_status()
            if st and pred(st):
                return st
            if dproc.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    def publish(name="T_E2E.py", asset_paths=None, slow=False,
                body=None, from_app="unreal"):
        # asset_paths：UE 发送端的稳定资产标识（P0-1 去重键主标识）。
        # 显式传 None = 发送端不带资产标识——Blender/Maya 发贴图到
        # PS/SAI2 的真实形态（options 只有 {"editor": target}），走回退
        # 分支「主文件名」（不含 sha256，P0-1 用户复核返工：内容不进键，
        # 同名即同一窗口，避免「改一版发一版」下堆窗口）。
        # slow=True 用慢速分块写「编辑器」（P1-3 settle 返工）。
        # body：自定义「编辑器」脚本内容（回退分支要同名不同内容）。
        options = {"source_files": [name]}
        if asset_paths:
            options["asset_paths"] = list(asset_paths)
        meta = {
            "asset_name": "T_E2E",
            "kind": "textures",
            "format": "py",
            "to": {"app": "sai2", "project": "", "content_path": ""},
            "from": {"app": from_app, "project": "E2EProj"},
            "options": options,
            "note": "e2e",
        }
        rec = req("POST", "/transfers", meta, base=base)
        tid = rec["transfer"]["id"]
        dest = Path(cfg["payload_dir"]) / tid
        dest.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = SIM_SLOW_BODY if slow else SIM_BODY
        (dest / name).write_bytes(body)
        req("POST", "/transfers/{0}/publish".format(tid), {}, base=base)
        return tid

    def transfer(tid):
        return req("GET", "/transfers/{0}".format(tid), base=base)

    def claimed_ids():
        return [x["id"] for x in
                req("GET", "/transfers?limit=200&status=claimed",
                    base=base)["transfers"]]

    def _stable_log(name, from_app="blender"):
        """回退分支（无 asset_paths）的稳定打开路径 slot。

        与 editor_daemon.py 的 Daemon._stage_for_open 用同一套 dedup_key 算法
        复算：slot = md5(repr(("sai2", from_app, ("file_name", name.lower())))).
        编辑器脚本被拷到 state/open/sai2/<slot>/<name> 并由此 Popen，故 sim.log
        （与 .watch_opened 标记）落在稳定目录而非 per-tid payload 目录。
        """
        dup_key = ("sai2", from_app, ("file_name", name.lower()))
        slot = hashlib.md5(repr(dup_key).encode("utf-8")).hexdigest()[:16]
        return state / "open" / "sai2" / slot / "sim.log"

    def _wait_editor_done(stable_log, timeout=30.0):
        """等「编辑器」进程结束（stable 目录 sim.log 出现 close 或 refocus）。

        复用场景下重发只重聚焦窗口（refocus，不重新保存），编辑器日志
        只打 refocus；首次打开则正常 open/save/close。两者都表示进程已退出。
        注意：日志在稳定打开路径目录，不在 per-tid payload 目录。
        """
        log = stable_log
        t0 = time.time()
        while time.time() - t0 < timeout:
            if log.is_file():
                txt = log.read_text(encoding="utf-8", errors="replace")
                if "close" in txt or "refocus" in txt:
                    return True
            time.sleep(0.3)
        return False

    try:
        # -- 0. 守护进程就绪 ------------------------------------------------
        st = wait_status(lambda s: s.get("running") is True, timeout=20.0)
        assert st is not None, "守护进程 20s 内未就绪（stdout: {0}）".format(
            stdout_path.read_text(encoding="utf-8", errors="replace")[-500:]
            if stdout_path.exists() else "无")
        print("守护进程就绪: session={0} apps={1}".format(
            st.get("session_id"), sorted((st.get("apps") or {}).keys())))

        # -- 1. 同一文件连发 5 次（0.7s 间隔，模拟用户反复点发送） ----------
        # 主分支（UE 形态：options 带 asset_paths）。回退分支的同内容
        # 连发去重见 case 2c（Blender 形态：无 asset_paths）。
        tids = []
        for i in range(5):
            tids.append(publish(asset_paths=["/Game/Bridge/Textures/T_E2E"]))
            print("  第 {0} 次发送 tid={1}".format(i + 1, tids[-1][:12]))
            time.sleep(0.7)
        t0 = time.time()
        while time.time() - t0 < 15.0:
            if all(transfer(x)["status"] in ("done", "error") for x in tids):
                break
            time.sleep(0.2)
        assert all(transfer(x)["status"] in ("done", "error") for x in tids), \
            "有传输没被领走: {0}".format(
                [(x[:8], transfer(x)["status"]) for x in tids])
        time.sleep(1.0)   # 让去重收尾（旧传输 finish）完成

        def _watching_one():
            st = read_status()
            assert st is not None, "status.json 读不到"
            w = st.get("watching") or []
            assert len(w) == 1, "watching={0} 条（预期 1）: {1}".format(
                len(w), [(x.get("app"), x.get("tid", "")[:8]) for x in w])
            assert w[0]["app"] == "sai2", w[0]
            assert w[0]["tid"] == tids[0], \
                "存活窗口应为首传输（复用，不更新为最新） {0}，实际 {1}".format(
                    tids[0][:8], w[0]["tid"][:8])
            assert w[0]["returns"] == 2, "returns={0}（预期 2）".format(w[0]["returns"])
            return "watching=1 tid={0}…（首传输，复用）returns=2".format(tids[0][:8])

        def _old_done_no_claimed():
            for i, x in enumerate(tids[:-1]):
                s = transfer(x)["status"]
                assert s == "done", "旧传输 #{0} 状态 {1}（应 done）".format(i + 1, s)
            cl = claimed_ids()
            assert not cl, "claimed 悬挂: {0}".format(cl)
            return "旧 4 条全 done；server 无 claimed 悬挂"

        def _two_returns():
            backs = [x for x in
                     req("GET", "/transfers?limit=200", base=base)["transfers"]
                     if (x.get("options") or {}).get("return_of") == tids[0]]
            assert len(backs) == 2, "回传 {0} 条（预期 2）".format(len(backs))
            assert all(b["status"] == "queued" for b in backs), \
                [b["status"] for b in backs]
            assert all(b["to"]["app"] == "unreal" for b in backs), "回传目标不对"
            return "恰好 2 条回传（return_of=首 tid，均 queued）"

        def _sims_finished():
            # 编辑器实际从「稳定打开路径」运行，sim.log 落在
            # state/open/sai2/<slot>/sim.log（不在 per-tid payload 目录）。
            # 5 连发同名同 asset_paths 共享同一 slot，故只有 1 个 sim.log，
            # 由首传输的编辑器写出 open/save/close（重发只 refocus）。
            logs = list(state.glob("open/sai2/*/sim.log"))
            assert logs, "稳定打开路径下无 sim.log（编辑器未运行）"
            txt = "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
            assert "close" in txt or "refocus" in txt, \
                "编辑器未正常结束: {0}".format(txt[-200:])
            return "编辑器进程正常退出（稳定路径 sim.log 含 close/refocus）"

        def _dedup_log():
            log = (state / "daemon.log").read_text(encoding="utf-8")
            n = log.count("[watch-dedup]")
            assert n >= 4, "[watch-dedup] 只有 {0} 条（预期 >=4）".format(n)
            return "[watch-dedup] x{0}".format(n)

        def _timing_sane():
            log = (state / "daemon.log").read_text(encoding="utf-8")
            vals = [float(m) for m in re.findall(
                r"save->detect=(-?\d+(?:\.\d+)?)s", log)]
            assert len(vals) >= 2, "[timing] 只有 {0} 条".format(len(vals))
            bad = [v for v in vals if not (-1.0 < v < 30.0)]
            assert not bad, "save->detect 异常值: {0}".format(bad)
            # 2026-10-16 P1-C：settle 自适应后 save->detect 应从旧 ~3s 降到 <1.5s
            assert min(vals) < 1.5, \
                "save->detect 未降到 <1.5s（P1-C 前 ~3s）: {0}".format(
                    ["{0:.2f}".format(v) for v in vals])
            return "save->detect min={0:.2f}s（P1-C 前 ~3s，已自适应下降）".format(min(vals))

        # -- 2. 等两次保存都回传完再断言 ------------------------------------
        t0 = time.time()
        while time.time() - t0 < 30.0:
            st = read_status()
            if st and (st.get("watching") or []):
                w0 = st["watching"][0]
                if w0.get("returns", 0) >= 2:
                    break
            time.sleep(0.3)
        assert read_status(), "status.json 消失"
        check("5 连发后 watching 只剩 1 条（最新 tid, returns=2）", _watching_one)
        check("旧传输全 done、无 claimed 悬挂", _old_done_no_claimed)
        check("恰好 2 条回传", _two_returns)
        check("5 个编辑器进程正常生命周期", _sims_finished)
        check("daemon.log [watch-dedup] 证据", _dedup_log)
        check("[timing] save->detect 正数", _timing_sane)

        # -- 2b. 不同目录同名文件两次发送 -> watching=2，两条都回传 ---------
        # 2026-10-15 P0-1 返工：去重键 = (to_app, from_app, 资产标识)，
        # 资产标识取 options.asset_paths（UE 稳定资产路径）。同名不同
        # asset_paths = 不同资产，不得误杀；大文件（17MB >16MB 档，兜底上限 6s）
        # 慢速分块写同时验证 P1-C settle 双采样不回传半写文件。
        # 注意：5 连发 case 残留 1 条旧 watch（returns=2），断言一律按
        # tid 过滤，不假设 watching 总数。
        def _same_name_diff_asset():
            log_path = state / "daemon.log"
            n_dedup_before = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            ta = publish(name="T_E2E.py", slow=True,
                         asset_paths=["/Game/Bridge/Textures/T_E2E_A"])
            time.sleep(0.7)
            tb = publish(name="T_E2E.py", slow=True,
                         asset_paths=["/Game/Bridge/Textures/Props/T_E2E_B"])
            print("  同名不同资产发送 tid_a={0} tid_b={1}".format(
                ta[:12], tb[:12]))
            t0 = time.time()
            while time.time() - t0 < 15.0:
                # 等「被 daemon 领走（claimed，编辑器已拉起、正在写）」即可——本断言要
                # 在**慢写进行中**检查 returns==0（无半写回传）。若等 done/error 会一路
                # 等到回传完成，断言永远落在 returns>=1 之后（误报）。其它测试等完成用
                # in ("done","error")，本处刻意用 != "queued"（=claimed/进行中）。
                if all(transfer(x)["status"] != "queued" for x in (ta, tb)):
                    break
                time.sleep(0.2)
            assert all(transfer(x)["status"] != "queued" for x in (ta, tb)), \
                "有传输没被领走: {0}".format(
                    [(x[:8], transfer(x)["status"]) for x in (ta, tb)])
            time.sleep(1.0)   # 让 claim + watch 建立完成
            st = read_status()
            w = [x for x in (st.get("watching") or [])
                 if x.get("tid") in (ta, tb)]
            assert len(w) == 2, "同名不同资产应各留 1 条 watch: {0}".format(
                [(x.get("tid", "")[:8], x.get("returns")) for x in w])
            assert all(x["returns"] == 0 for x in w), \
                "不应有任何回传: {0}".format([x.get("returns") for x in w])
            # 等两个慢写编辑器都回传完（1s + 3.6s 分块 + 1.5s settle + 轮询）
            t0 = time.time()
            while time.time() - t0 < 40.0:
                st = read_status()
                ws = [x for x in (st.get("watching") or [])
                      if x.get("tid") in (ta, tb)]
                if ws and all(x.get("returns", 0) >= 1 for x in ws):
                    break
                time.sleep(0.3)
            all_recs = req("GET", "/transfers?limit=200",
                           base=base)["transfers"]
            for t in (ta, tb):
                backs = [x for x in all_recs
                         if (x.get("options") or {}).get("return_of") == t]
                assert len(backs) == 1, "tid={0} 回传 {1} 条（预期 1）".format(
                    t[:8], len(backs))
                assert backs[0]["status"] == "queued", backs[0]["status"]
                assert transfer(t)["status"] == "done", transfer(t)["status"]
                # 回传内容必须是完整分块内容（不是半写）
                bdir = Path(cfg["payload_dir"]) / backs[0]["id"]
                data = (bdir / "T_E2E.py").read_bytes()
                assert data == SLOW_EXPECTED, \
                    "tid={0} 回传了半写文件: {1!r}…".format(t[:8], data[:40])
            # 这两次发送之间不得产生新的 [watch-dedup] 证据行
            n_dedup_after = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            assert n_dedup_after == n_dedup_before, \
                "同名不同资产触发了去重: {0} -> {1}".format(
                    n_dedup_before, n_dedup_after)
            return ("watching=2（按 tid 过滤）；两条各自回传 1 条且内容完整；"
                    "无新增 [watch-dedup]")

        check("不同目录同名文件：两条 watch 并存、都回传（P0-1/P1-3）",
              _same_name_diff_asset)

        # -- 2b. 回退分支（无 asset_paths）：同名（不论内容）复用 1 窗口（P0-1 用户复核返工）
        # 用户评审必答：Blender/Maya 发贴图到 PS/SAI2 是真实流量，options 只有
        # {"editor": target}，没有 asset_paths -> 走回退分支。回退键只认主文件名
        # （内容不进键，P0-1 用户复核返工已修「叠 sha256 在改一版发一版下不复用」），
        # 故「我改一版发一版」= 同名不同内容也复用同一窗口：重发被 superseded、
        # 旧窗口（首会话）回传不丢。这里用两个内容不同的 sim 编辑器实证：
        # 同名不同内容 -> 仅 1 窗口（首 tid）、重发 superseded、首窗口两次保存回传不丢。
        def _fallback_diff_content():
            log_path = state / "daemon.log"
            n_dedup_before = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            ta = publish(name="T_FB.py", asset_paths=None, body=SIM_BODY,
                         from_app="blender")
            assert _wait_editor_done(_stable_log("T_FB.py", "blender")), \
                "ta 编辑器未正常结束"
            tb = publish(name="T_FB.py", asset_paths=None, body=SIM_SLOW_BODY,
                         from_app="blender")
            print("  回退分支同名不同内容 tid_a={0} tid_b={1}".format(
                ta[:12], tb[:12]))
            t0 = time.time()
            while time.time() - t0 < 15.0:
                if all(transfer(x)["status"] in ("done", "error") for x in (ta, tb)):
                    break
                time.sleep(0.2)
            assert all(transfer(x)["status"] in ("done", "error") for x in (ta, tb)), \
                "有传输没被领走"
            time.sleep(1.0)
            st = read_status()
            w = [x for x in (st.get("watching") or [])
                 if x.get("tid") in (ta, tb)]
            assert len(w) == 1, "回退分支同名（不论内容）应复用 1 窗口: {0}".format(
                [(x.get("tid", "")[:8], x.get("returns")) for x in w])
            assert w[0]["tid"] == ta, "存活窗口应为首传输（复用，不更新为最新）"
            r_b = transfer(tb)
            assert r_b["status"] == "done", "重发应 done: {0}".format(r_b["status"])
            assert (r_b.get("result") or {}).get("superseded_by") == ta, \
                "重发未 superseded 到首窗口: {0}".format(r_b.get("result"))
            n_dedup_after = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            assert n_dedup_after == n_dedup_before + 1, \
                "复用应产生 1 条 [watch-dedup]: {0} -> {1}".format(
                    n_dedup_before, n_dedup_after)
            # 等首窗口（ta）两次保存都回传完（SIM_BODY 存两次）
            t0 = time.time()
            while time.time() - t0 < 40.0:
                st = read_status()
                ws = [x for x in (st.get("watching") or [])
                      if x.get("tid") == ta]
                if ws and ws[0].get("returns", 0) >= 2:
                    break
                time.sleep(0.3)
            all_recs = req("GET", "/transfers?limit=200",
                           base=base)["transfers"]
            backs_a = [x for x in all_recs
                       if (x.get("options") or {}).get("return_of") == ta]
            backs_b = [x for x in all_recs
                       if (x.get("options") or {}).get("return_of") == tb]
            assert len(backs_a) == 2, \
                "首窗口（ta）回传 {0} 条（预期 2）".format(len(backs_a))
            assert not backs_b, \
                "重发（tb）被 superseded，不应产生回传，实际 {0} 条".format(len(backs_b))
            # 首窗口两次保存都回传了，且内容是 SIM_BODY 的两次保存
            # （复用窗口不会自动重写内容；tb 虽内容不同但被 superseded，未产生回传）
            # 注意：API 返回顺序不保证为时间序，按 created_at 排序后再比对。
            backs_a_sorted = sorted(
                backs_a, key=lambda r: r.get("created_at") or "")
            want_seq = [b"# sim save 1\n", b"# sim save 2 (a bit longer than the first)\n"]
            for bk, want in zip(backs_a_sorted, want_seq):
                bdir = Path(cfg["payload_dir"]) / bk["id"]
                data = (bdir / "T_FB.py").read_bytes()
                assert data == want, \
                    "回传内容不对（半写?） len={0} want_len={1} data={2!r}".format(
                        len(data), len(want), data)
            return ("回退分支同名不同内容 -> 复用 1 窗口（首 tid）、重发 superseded、"
                    "首窗口 2 次保存回传不丢")

        check("回退分支：编辑→发→编辑→再发（不同内容，高频）两条 watch 各回传不互杀（P0-1 二轮返工）",
              _fallback_diff_content)

        # -- 2b-2. 回退分支「编辑→发→编辑→再发」高频场景：同文件 3 次不同
        # 内容发送。P0-1 用户复核返工后的最终语义：回退去重键只认主文件名
        # （不含 sha256），3 个不同内容、同名文件 = **同一逻辑窗口**——
        # 只有首传输（t1）建立 watch，后续 2 次重发一律 done + superseded_by=
        # 首 tid；首窗口回传不丢（首次打开的编辑器存 1 次，returns=1），重发
        # 不各开窗口（绝不堆窗口）。这与「不同目录同名资产串台」是同源取舍：
        # 无 asset_paths 时只能靠文件名，串台是残余风险；此处要证明的是
        # 「高频改稿连发不堆窗口、首窗口回传不丢」。
        def _fallback_diff_content_x3():
            log_path = state / "daemon.log"
            n_dedup_before = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            t1 = publish(name="T_FB3.py", asset_paths=None,
                         body=_sim_once_body("0"), from_app="blender")
            assert _wait_editor_done(_stable_log("T_FB3.py", "blender")), \
                "首次编辑器未正常结束"
            t2 = publish(name="T_FB3.py", asset_paths=None,
                         body=_sim_once_body("1"), from_app="blender")
            t3 = publish(name="T_FB3.py", asset_paths=None,
                         body=_sim_once_body("2"), from_app="blender")
            print("  回退分支 3 连发（同名不同内容） tid_1={0} tid_2={1} tid_3={2}".format(
                t1[:12], t2[:12], t3[:12]))
            t0 = time.time()
            while time.time() - t0 < 15.0:
                if all(transfer(x)["status"] in ("done", "error") for x in (t1, t2, t3)):
                    break
                time.sleep(0.2)
            assert all(transfer(x)["status"] in ("done", "error") for x in (t1, t2, t3)), \
                "有传输没被领走"
            time.sleep(1.0)   # 让去重收尾（重发 finish）完成
            st = read_status()
            w = [x for x in (st.get("watching") or [])
                 if x.get("tid") in (t1, t2, t3)]
            assert len(w) == 1, \
                "同名不同内容应复用 1 窗口: {0}".format(
                    [(x.get("tid", "")[:8], x.get("returns")) for x in w])
            assert w[0]["tid"] == t1, \
                "存活窗口应为首传输（复用，不更新为最新） {0}，实际 {1}".format(
                    t1[:8], w[0]["tid"][:8])
            # 重发的 2 次都 superseded 到首窗口
            for t_re in (t2, t3):
                r = transfer(t_re)
                assert r["status"] == "done", "重发应 done: {0}".format(r["status"])
                assert (r.get("result") or {}).get("superseded_by") == t1, \
                    "重发未 superseded 到首窗口: {0}".format(r.get("result"))
            n_dedup_after = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            assert n_dedup_after == n_dedup_before + 2, \
                "2 次复用应产生 2 条 [watch-dedup]: {0} -> {1}".format(
                    n_dedup_before, n_dedup_after)
            # 首窗口的 1 次保存回传不丢（重发被 superseded，不产生回传）
            t0 = time.time()
            while time.time() - t0 < 40.0:
                st = read_status()
                ws = [x for x in (st.get("watching") or [])
                      if x.get("tid") == t1]
                if ws and ws[0].get("returns", 0) >= 1:
                    break
                time.sleep(0.3)
            all_recs = req("GET", "/transfers?limit=200",
                           base=base)["transfers"]
            backs_1 = [x for x in all_recs
                       if (x.get("options") or {}).get("return_of") == t1]
            backs_2 = [x for x in all_recs
                       if (x.get("options") or {}).get("return_of") == t2]
            backs_3 = [x for x in all_recs
                       if (x.get("options") or {}).get("return_of") == t3]
            assert len(backs_1) == 1, \
                "首窗口（t1）回传 {0} 条（预期 1）".format(len(backs_1))
            assert not backs_2, \
                "重发（t2）被 superseded，不应回传，实际 {0} 条".format(len(backs_2))
            assert not backs_3, \
                "重发（t3）被 superseded，不应回传，实际 {0} 条".format(len(backs_3))
            return ("回退分支同名不同内容 3 连发 -> 复用 1 窗口（首 tid）、"
                    "2 次重发 superseded、首窗口回传不丢")

        check("回退分支：编辑→发→编辑→再发（不同内容，高频）3 连发复用 1 窗口（P0-1 用户复核）",
              _fallback_diff_content_x3)

        # -- 2c. 回退分支同内容连发：仍去重（首 tid 存活，重发 superseded）---
        # 最终语义（P0-1 用户复核返工）：同名即同一窗口，无论内容是否相同。
        # 首传输（t1）建立 watch 并存活，重发（t2）done + superseded_by=首 tid；
        # 幸存 watch 是 t1（不是最新），旧测试「保留最新 tid」已被推翻。
        def _fallback_same_content():
            log_path = state / "daemon.log"
            n_dedup_before = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            t1 = publish(name="T_FB2.py", asset_paths=None, body=SIM_BODY,
                         from_app="blender")
            time.sleep(0.7)
            t2 = publish(name="T_FB2.py", asset_paths=None, body=SIM_BODY,
                         from_app="blender")
            print("  回退分支同内容连发 tid_1={0} tid_2={1}".format(
                t1[:12], t2[:12]))
            t0 = time.time()
            while time.time() - t0 < 15.0:
                if all(transfer(x)["status"] in ("done", "error") for x in (t1, t2)):
                    break
                time.sleep(0.2)
            assert all(transfer(x)["status"] in ("done", "error") for x in (t1, t2)), \
                "有传输没被领走"
            time.sleep(1.0)
            st = read_status()
            w = [x for x in (st.get("watching") or [])
                 if x.get("tid") in (t1, t2)]
            assert len(w) == 1, "同内容连发应去重成 1 条: {0}".format(
                [(x.get("tid", "")[:8], x.get("returns")) for x in w])
            assert w[0]["tid"] == t1, \
                "存活窗口应为首传输（复用，不更新为最新） {0}，实际 {1}".format(
                    t1[:8], w[0]["tid"][:8])
            r2 = transfer(t2)
            assert r2["status"] == "done", r2["status"]
            assert (r2.get("result") or {}).get("superseded_by") == t1, \
                "superseded_by 未写回首 tid: {0}".format(r2.get("result"))
            n_dedup_after = log_path.read_text(
                encoding="utf-8").count("[watch-dedup]")
            assert n_dedup_after == n_dedup_before + 1, \
                "[watch-dedup] 应 +1: {0} -> {1}".format(
                    n_dedup_before, n_dedup_after)
            # 幸存 watch（t1）至少回传 1 条（SIM_BODY 会存两次）
            t0 = time.time()
            while time.time() - t0 < 20.0:
                st = read_status()
                ws = [x for x in (st.get("watching") or [])
                      if x.get("tid") == t1]
                if ws and ws[0].get("returns", 0) >= 1:
                    break
                time.sleep(0.3)
            ws = [x for x in (read_status().get("watching") or [])
                  if x.get("tid") == t1]
            assert ws and ws[0].get("returns", 0) >= 1, \
                "幸存 watch（t1）未回传: {0}".format(ws)
            return "回退分支同内容连发 -> 去重成 1 条（首 tid 存活）、重发 done+superseded_by、幸存者回传"

        check("回退分支：同内容连发仍去重（首 tid 存活）",
              _fallback_same_content)

        # -- 2d. 关窗重开分支 e2e（用户 P1 质询：该分支此前只在单测跑过）------
        # 停 daemon1，换 AB_EDITOR_OPEN_STATE=closed 重启 daemon2（同一中转
        # 服务、同一 state 目录），真实验证「探测为关 -> 重新 Popen」全链路。
        rc = subprocess.call([str(PY), str(DAEMON), "--stop",
                              "--state-dir", str(state)])
        assert rc == 0, "停 daemon1 rc={0}".format(rc)
        t0 = time.time()
        while time.time() - t0 < 10.0 and dproc.poll() is None:
            time.sleep(0.2)
        assert dproc.poll() is not None, "daemon1 未退出"
        dproc = subprocess.Popen(          # 闭包读的是变量本身，重绑即生效
            [str(PYW), "-u", str(DAEMON),
             "--server", base,
             "--state-dir", str(state),
             "--sai2", str(PYW),
             "--interval", "0.5"],
            cwd=str(ROOT), close_fds=True,
            env=dict(os.environ, AB_EDITOR_OPEN_STATE="closed"),
            stdout=open(state / "daemon2_stdout.txt", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
        print("守护进程2已拉起 pid={0}（AB_EDITOR_OPEN_STATE=closed）".format(
            dproc.pid))
        st = wait_status(lambda s: s.get("running") is True, timeout=20.0)
        assert st is not None, "daemon2 20s 内未就绪"

        def _closed_reopen():
            t1 = publish(name="T_RE.py", body=SIM_RUNMARK_BODY,
                         from_app="blender")
            logp = _stable_log("T_RE.py", "blender")
            t0 = time.time()
            while time.time() - t0 < 20.0 and not logp.is_file():
                time.sleep(0.2)
            assert _wait_editor_done(logp), "run1 编辑器未结束"
            # 关窗后重发 -> 探测强制 closed -> 必须重新 Popen（不静默丢）
            t2 = publish(name="T_RE.py", body=SIM_RUNMARK_BODY,
                         from_app="blender")
            t0 = time.time()
            while time.time() - t0 < 20.0:
                if transfer(t2)["status"] in ("done", "error"):
                    break
                time.sleep(0.2)
            r2 = transfer(t2)
            assert r2["status"] == "done", r2["status"]
            msg = (r2.get("result") or {}).get("message", "")
            assert "重新打开" in msg, "重发应走重开分支: {0}".format(msg)
            # Popen 真被再次调用的铁证：sim.log 恰好 >= 2 条 " run"
            t0 = time.time()
            runs = 0
            while time.time() - t0 < 25.0:
                runs = logp.read_text(
                    encoding="utf-8", errors="replace").count(" run\n")
                if runs >= 2:
                    break
                time.sleep(0.3)
            assert runs >= 2, \
                "Popen 未被再次真实执行（run 标记 {0} 条）".format(runs)
            assert _wait_editor_done(logp), "run2 编辑器未结束"
            # daemon.log 有 [watch-reopen] 证据行
            dlog = (state / "daemon.log").read_text(
                encoding="utf-8", errors="replace")
            assert "[watch-reopen]" in dlog, "缺 [watch-reopen] 日志"
            # 不追加新 watch：t1/t2 只剩 1 条且 tid 为 t1
            t0 = time.time()
            while time.time() - t0 < 25.0:
                st2 = read_status()
                ws = [x for x in (st2.get("watching") or [])
                      if x.get("tid") in (t1, t2)]
                if len(ws) == 1 and ws[0].get("tid") == t1 \
                        and ws[0].get("returns", 0) >= 2:
                    break
                time.sleep(0.3)
            st2 = read_status()
            ws = [x for x in (st2.get("watching") or [])
                  if x.get("tid") in (t1, t2)]
            assert len(ws) == 1 and ws[0].get("tid") == t1, \
                "重开不应追加 watch: {0}".format(
                    [(x.get("tid", "")[:8], x.get("returns")) for x in ws])
            assert ws[0].get("returns", 0) >= 2, \
                "两次打开各保存 1 次应产生 2 条回传: {0}".format(ws[0])
            return ("关窗重发 -> 重发 done(重新打开) + sim run 标记 2 条"
                    "（Popen 真被调 2 次）+ watch 恒 1 + 2 条回传")

        check("关窗重开分支（daemon2 closed）：重发必重开，Popen 真被调",
              _closed_reopen)

        # -- 3. --stop 干净退出 --------------------------------------------
        def _stop_clean():
            rc = subprocess.call([str(PY), str(DAEMON), "--stop",
                                  "--state-dir", str(state)])
            assert rc == 0, "--stop 返回 {0}".format(rc)
            t1 = time.time()
            while time.time() - t1 < 20.0:
                if dproc.poll() is not None:
                    break
                time.sleep(0.2)
            assert dproc.poll() is not None, "守护进程 20s 未退出"
            assert dproc.returncode == 0, "退出码 {0}".format(dproc.returncode)
            log = (state / "daemon.log").read_text(encoding="utf-8")
            assert "守护进程退出" in log, "日志无退出记录"
            assert not (state / "daemon.pid").exists(), "pid 文件未清理"
            return "--stop rc=0，进程退出码 0，pid 文件已清理"

        check("--stop 干净退出", _stop_clean)

    finally:
        if dproc.poll() is None:
            try:
                dproc.kill()
            except Exception:
                pass
        try:
            req("POST", "/shutdown", {}, base=base)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 结果: {0} 通过 / {1} 失败 ===\n".format(len(PASSED), len(FAILED)))
    for name, exc in FAILED:
        print("  x {0}: {1}".format(name, exc))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
