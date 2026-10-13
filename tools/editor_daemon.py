# -*- coding: utf-8 -*-
"""Asset Bridge —— Photoshop / SAI2 外部编辑器守护进程。

为什么需要守护进程：
    Photoshop / SAI2 没有可编程的桥接 API（SP 有 JS API、Toolbag 有 mset 插件），
    「发送到 PS → 编辑 → 保存后发送端自动重新加载」只能由外部进程代劳：

      1. 轮询中转服务里 to.app = photoshop / sai2 的贴图传输（kind=textures）
      2. claim 后用对应 exe 打开文件（PS / SAI2 都支持命令行传文件路径）
      3. 监听文件 mtime/大小变化 + 同目录新图片文件（覆盖「另存为」）
      4. 检测到保存且写入稳定后：新建一条 kind=textures 传输回传给原发送端
         （options.return_of / source_file 告诉发送端这是「重载」而非新导入），
         随后把原传输标记 done
      5. 用户没保存就关了编辑器 → 宽限期后把原传输标记 cancelled

单实例：pid 文件 + 进程存活检查（ctypes OpenProcess），
        防止两个守护进程互相抢 claim / 互相 cancel。
停止：  --stop 写 stop.flag，主循环每轮检查，干净退出（不杀用户的 PS/SAI2）。
状态：  status.json 供托盘读取（watching / handled / last_event）。

仅标准库；所有 HTTP 走无代理 opener（本机系统代理会劫持 127.0.0.1）。

用法：
    pythonw.exe editor_daemon.py                 # 常驻运行（无控制台）
    python editor_daemon.py --check              # 打印配置与运行状态（JSON）
    python editor_daemon.py --stop [--force]     # 请求停止（--force 才兜底强杀）
    python editor_daemon.py --once               # 只跑一个轮询周期（调试用）
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "editor_daemon.json"
DEFAULT_STATE_DIR = REPO_ROOT / "runtime" / "editor_daemon"

DEFAULT_SERVER = "http://127.0.0.1:27631"
DEFAULT_APPS = {
    "photoshop": r"D:\DCC\Adobe Photoshop 2023\Photoshop.exe",
    "sai2": r"D:\DCC\[Saier]SAI Ver.2\sai2.exe",
    "zbrush": r"D:\DCC\Zbrush2026.2.1\ZBrush.exe",
}
APP_LABELS = {"photoshop": "Photoshop", "sai2": "SAI2", "zbrush": "ZBrush"}

# ZBrush 专用目录（payload 下）
ZB_INBOX = "zbrush_inbox"        # 任务文件目录：守护进程写 <tid>.json，ZBrush 适配器消费
ZB_OUTBOX = "zbrush_outbox"      # 热文件夹：用户手动导出，守护进程自动回传
ZB_HEARTBEAT = "zb_bridge.heartbeat"   # ZBrush 侧常驻桥接循环写的心跳（判断 worker 存活）
ZB_NOTIFY = "notify.json"        # D3 托盘提示：ZBrush 运行中且 worker 未接管的落盘通知
ZB_IMPORT_TIMEOUT_SEC = 300.0    # 任务投递后等回执的上限
ZB_POPEN_TIMEOUT_SEC = 90.0      # 一次性 -script 拉起后等回执的上限
ZB_BRIDGE_MAX_AGE = 12.0         # 心跳 freshness 阈值（秒）

# ZBrush 适配器源码位置（一次性 -script 兜底用）
ZB_ONE_SHOT = REPO_ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "one_shot_import.py"

# 认识的模型扩展名（outbox 只收这些）
MODEL_EXTS = {".obj", ".ztl", ".stl", ".ply", ".fbx"}

# 认识的图片扩展名（另存为新文件时只收这些；.sai2 等原生格式不收）
IMAGE_EXTS = {
    ".png", ".psd", ".tga", ".jpg", ".jpeg", ".bmp", ".exr", ".tif", ".tiff",
}

POLL_INTERVAL_SEC = 2.0        # 主循环周期
SETTLE_SEC = 1.5               # 文件写入稳定判定（mtime/大小不变这么久才算保存完成）
EXIT_GRACE_SEC = 60.0          # 编辑器进程退出后、无保存行为的宽限期
LAUNCH_DELEGATE_SEC = 3.0      # 启动后秒退 = 委托给已运行实例，不能当成用户关闭
COPY_RETRY = 3                 # 拷贝保存文件的重试次数（PS 可能短暂占用文件）
COPY_RETRY_WAIT = 0.6
HEARTBEAT_EVERY = 15           # 每多少个循环发一次心跳
LOG_RL_SEC = 30.0              # 同类日志限频间隔

HTTP_TIMEOUT = 4.0
API = "/api/v1"

# 本机回环必须绕过代理：这台机器的系统 HTTP 代理会把 127.0.0.1 也劫走，
# 表现是 502 upstream，极难查（curl 侧同样要 --noproxy '*'）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_print(text: str) -> None:
    """GBK 控制台下打印中文不炸（pythonw 运行时 stdout 为 None）。"""
    try:
        if sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """Windows 下不依赖 tasklist 的进程存活检查。"""
    if not pid or pid <= 0:
        return False
    SYNCHRONIZE = 0x00100000
    try:
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED(5) = 进程存在但没权限开句柄
        return ctypes.windll.kernel32.GetLastError() == 5
    except Exception:
        return False


def _request(method: str, path: str, body=None, server: str = DEFAULT_SERVER,
             timeout: float = HTTP_TIMEOUT):
    url = server + API + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _put_file(server: str, path: str, file_path: str,
              timeout: float = 300.0) -> dict:
    """PUT /transfers/{id}/files/{name}，原始字节上传。"""
    url = server + API + path
    with open(file_path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/octet-stream"})
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# ZBrush 握手（任务文件 + 回执 + 心跳 + 托盘通知）
# ---------------------------------------------------------------------------

def zb_dirs(payload_dir: str):
    inbox = os.path.join(payload_dir, ZB_INBOX) if payload_dir else ""
    outbox = os.path.join(payload_dir, ZB_OUTBOX) if payload_dir else ""
    hb = os.path.join(payload_dir, ZB_HEARTBEAT) if payload_dir else ""
    return inbox, outbox, hb


def zb_bridge_alive(payload_dir: str, max_age: float = ZB_BRIDGE_MAX_AGE) -> bool:
    """ZBrush 侧常驻桥接循环是否活着（心跳 freshness）。"""
    _, _, hb = zb_dirs(payload_dir)
    if not hb or not os.path.isfile(hb):
        return False
    try:
        return (time.time() - os.stat(hb).st_mtime) <= max_age
    except OSError:
        return False


def zb_write_task(payload_dir: str, record: dict, files) -> str:
    """写 <tid>.json 任务文件（ZBrush 适配器扫描消费）。"""
    inbox, _, _ = zb_dirs(payload_dir)
    if not inbox:
        return ""
    os.makedirs(inbox, exist_ok=True)
    task = {
        "transfer_id": record.get("id", ""),
        "asset_name": record.get("asset_name", ""),
        "from_app": str((record.get("from") or {}).get("app", "")),
        "files": [f for f in (files or []) if f],
        "at": now_iso(),
    }
    path = os.path.join(inbox, task["transfer_id"] + ".json")
    _atomic_write_json(Path(path), task)
    return path


def zb_receipt_path(payload_dir: str, tid: str) -> str:
    inbox, _, _ = zb_dirs(payload_dir)
    return os.path.join(inbox, tid + ".done.json") if inbox else ""


def zb_take_receipt(payload_dir: str, tid: str):
    """读回执并消费（删除）。没有则返回 None。"""
    p = zb_receipt_path(payload_dir, tid)
    if not p or not os.path.isfile(p):
        return None
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        data = None
    try:
        os.remove(p)
    except OSError:
        pass
    return data


def zb_write_notify(state_dir: Path, record: dict, files) -> str:
    """D3：ZBrush 运行中且 worker 未接管 -> 落盘通知托盘（路径+一键复制+自动消失）。"""
    try:
        os.makedirs(str(state_dir), exist_ok=True)
        obj = {
            "at": now_iso(),
            "transfer_id": record.get("id", ""),
            "asset_name": record.get("asset_name", ""),
            "files": [str(f) for f in (files or [])],
            "path": str(files[0]) if files else "",
        }
        p = state_dir / ZB_NOTIFY
        _atomic_write_json(p, obj)
        return str(p)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config(args) -> dict:
    """优先级：命令行 > editor_daemon.json > 内置默认。"""
    cfg = {
        "server": DEFAULT_SERVER,
        "state_dir": Path(DEFAULT_STATE_DIR),
        "apps": dict(DEFAULT_APPS),
        "zbrush_outbox_to": "blender",
    }
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if str(data.get("server_url", "")).strip():
                cfg["server"] = str(data["server_url"]).strip()
            for name, exe in (data.get("apps") or {}).items():
                if exe:
                    cfg["apps"][str(name)] = str(exe)
            if str(data.get("zbrush_outbox_to", "")).strip():
                cfg["zbrush_outbox_to"] = str(data["zbrush_outbox_to"]).strip()
        except Exception as exc:  # 配置文件坏了不影响默认值
            _safe_print("[editor_daemon] 配置文件读取失败，用默认值: {0}".format(exc))
    if args.server:
        cfg["server"] = args.server
    if args.state_dir:
        cfg["state_dir"] = Path(args.state_dir)
    if args.photoshop:
        cfg["apps"]["photoshop"] = args.photoshop
    if args.sai2:
        cfg["apps"]["sai2"] = args.sai2
    if getattr(args, "zbrush", ""):
        cfg["apps"]["zbrush"] = args.zbrush
    return cfg


# ---------------------------------------------------------------------------
# 监视器：一个「编辑器里打开着待保存文件」的会话
# ---------------------------------------------------------------------------

class Watch(object):
    def __init__(self, record: dict, files, proc):
        self.tid = record["id"]
        self.app = str((record.get("to") or {}).get("app", "")).lower()
        self.asset_name = record.get("asset_name", "")
        frm = record.get("from") or {}
        self.from_app = str(frm.get("app", "")).lower()
        self.from_project = str(frm.get("project", "") or "")
        self.files = [f for f in (files or []) if f]
        self.dir = os.path.dirname(self.files[0]) if self.files else ""
        self.orig_options = record.get("options") or {}   # 回传时原样合并回去
        self.proc = proc
        self.opened_at = now_iso()
        self.opened_mono = time.monotonic()
        self.proc_exited_at = None      # 单调时钟
        self.delegated = False          # 启动即退 = 委托给已在运行的实例
        self.dirty_ts = 0.0             # 单调时钟
        self.dirty_names = []
        self.baseline = self.snapshot()

    # -- 快照：原文件 + 同目录新图片文件 ------------------------------------
    def snapshot(self) -> dict:
        snap = {}
        for f in self.files:
            name = os.path.basename(f)
            try:
                st = os.stat(f)
                snap[name] = (st.st_mtime, st.st_size)
            except OSError:
                snap[name] = None
        if self.dir and os.path.isdir(self.dir):
            try:
                for name in os.listdir(self.dir):
                    if name in snap:
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in IMAGE_EXTS:
                        continue
                    p = os.path.join(self.dir, name)
                    try:
                        st = os.stat(p)
                        snap[name] = (st.st_mtime, st.st_size)
                    except OSError:
                        pass
            except OSError:
                pass
        return snap

    def changed_files(self):
        cur = self.snapshot()
        out = []
        for name, meta in cur.items():
            if self.baseline.get(name) != meta:
                out.append(name)
        return out

    def brief(self) -> dict:
        return {
            "tid": self.tid,
            "app": self.app,
            "asset_name": self.asset_name,
            "return_to": self.from_app,
            "files": [os.path.basename(f) for f in self.files],
            "opened_at": self.opened_at,
            "pending_save": bool(self.dirty_names),
        }

    # -- 每周期推进一次，返回要做的动作 --------------------------------------
    def advance(self, now_mono: float):
        if self.proc is not None and self.proc_exited_at is None:
            try:
                if self.proc.poll() is not None:
                    self.proc_exited_at = now_mono
                    # PS 已运行时再启动一个 Photoshop.exe 会秒退（文件委托给已有
                    # 实例），此时用户还在编辑，绝不能按「已关闭」处理。
                    if now_mono - self.opened_mono <= LAUNCH_DELEGATE_SEC:
                        self.delegated = True
            except Exception:
                pass

        changed = self.changed_files()
        if changed:
            if changed != self.dirty_names:
                self.dirty_ts = now_mono
                self.dirty_names = changed
            if now_mono - self.dirty_ts >= SETTLE_SEC:
                return "finalize"

        if (self.proc_exited_at is not None and not self.delegated
                and not self.dirty_names):
            if now_mono - self.proc_exited_at >= EXIT_GRACE_SEC:
                return "abandon"
        return None


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------

class Daemon(object):
    def __init__(self, cfg: dict, interval: float = POLL_INTERVAL_SEC,
                 once: bool = False, verbose: bool = False):
        self.cfg = cfg
        self.interval = max(0.5, float(interval))
        self.once = once
        self.verbose = verbose
        self.server = cfg["server"]
        self.own_pidfile = not once   # --once 调试模式不占用单实例位
        self.state_dir = Path(cfg["state_dir"])
        self.pid_path = self.state_dir / "daemon.pid"
        self.stop_path = self.state_dir / "stop.flag"
        self.log_path = self.state_dir / "daemon.log"
        self.status_path = self.state_dir / "status.json"
        self.session_id = "ed_" + uuid.uuid4().hex[:12]
        self.started_at = now_iso()
        self.payload_dir = ""           # 由 GET /config 缓存
        self.watches = []
        self.zb_pending = []            # ZBrush 任务投递等待回执的列表
        self.zb_returned = 0            # 热文件夹回传计数
        self.handled = 0
        self.returned = 0
        self.last_event = "守护进程启动中"
        self.stopping = False
        self._rl = {}          # 日志限频表
        self._cycle_count = 0

    # -- 日志 ---------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = "{0} [editor_daemon] {1}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        if self.verbose:
            _safe_print(line)

    def log_rl(self, key: str, msg: str) -> None:
        now = time.monotonic()
        if now - self._rl.get(key, 0.0) >= LOG_RL_SEC:
            self._rl[key] = now
            self.log(msg)

    def event(self, msg: str) -> None:
        self.last_event = msg
        self.log(msg)

    # -- HTTP（容错） -------------------------------------------------------
    def req(self, method: str, path: str, body=None):
        try:
            return _request(method, path, body, server=self.server)
        except Exception:
            return None

    def safe(self, method: str, path: str, body=None):
        return self.req(method, path, body)

    # -- 会话 ---------------------------------------------------------------
    def ensure_session(self) -> bool:
        if self.session_id:
            resp = self.safe("POST", "/sessions/{0}/heartbeat".format(self.session_id), {})
            if resp and resp.get("session"):
                self._refresh_payload_dir()
                return True
        resp = self.safe("POST", "/sessions", {
            "app": "editor_daemon",
            "version": "1.0",
            "pid": os.getpid(),
            "capabilities": {"apps": sorted(self.cfg["apps"].keys())},
        })
        if resp and resp.get("session"):
            self.session_id = resp["session"].get("session_id") or self.session_id
            self._refresh_payload_dir()
            return True
        return False

    def _refresh_payload_dir(self) -> None:
        cfg = self.safe("GET", "/config")
        payload_dir = str((cfg or {}).get("payload_dir", ""))
        if payload_dir:
            self.payload_dir = payload_dir

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> int:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _safe_print("[editor_daemon] 无法创建状态目录 {0}: {1}".format(
                self.state_dir, exc))
            return 2

        # 单实例保护（--once 调试模式不占位）
        if self.own_pidfile:
            if not self._acquire_pidfile():
                return 3

        # 清掉可能残留的停止flag，否则新进程一启动就退
        try:
            if self.stop_path.exists():
                self.stop_path.unlink()
        except Exception:
            pass

        self.log("守护进程启动 pid={0} server={1} state={2}".format(
            os.getpid(), self.server, self.state_dir))
        self.event("守护进程已启动（{0}）".format(
            "、".join(APP_LABELS.get(a, a) for a in self.cfg["apps"])))

        code = 0
        try:
            while True:
                if self.stop_path.exists():
                    self.event("收到停止指令，退出")
                    break
                try:
                    self.cycle()
                except Exception as exc:
                    self.log_rl("cycle", "周期异常: {0!r}".format(exc))
                self.write_status()
                if self.once:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.event("Ctrl+C，退出")
        finally:
            self.cleanup()
        return code

    def _acquire_pidfile(self) -> bool:
        if self.pid_path.exists():
            try:
                old = int(self.pid_path.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                old = 0
            if old and old != os.getpid() and _pid_alive(old):
                _safe_print("[editor_daemon] 已有实例在运行（pid={0}），退出".format(old))
                self.log("拒绝启动：已有实例 pid={0}".format(old))
                return False
            self.log("清理残留 pid 文件（pid={0} 已不存在）".format(old))
        _atomic_write_json(self.pid_path, os.getpid())
        return True

    def cleanup(self) -> None:
        # 没保存完的监视会话：标记 cancelled，不动用户的编辑器
        for w in list(self.watches):
            try:
                self.safe("POST", "/transfers/{0}/finish".format(w.tid), {
                    "status": "cancelled",
                    "result": {"message": "守护进程停止，未完成的编辑作废（文件仍在负载目录）"},
                })
            except Exception:
                pass
        self.watches = []
        # ZBrush 待回执任务：manual 模式保留任务文件（用户稍后加载），
        # 其余清理任务文件并取消传输
        for p in list(self.zb_pending):
            if p["mode"] != "manual" and p.get("task"):
                try:
                    os.remove(p["task"])
                except OSError:
                    pass
            try:
                self.safe("POST", "/transfers/{0}/finish".format(p["tid"]), {
                    "status": "cancelled",
                    "result": {"message": "守护进程停止，ZBrush 任务作废（文件仍在负载目录）"},
                })
            except Exception:
                pass
        self.zb_pending = []
        self.stopping = True
        if self.own_pidfile:
            try:
                if self.pid_path.exists():
                    self.pid_path.unlink()
            except Exception:
                pass
            self.write_status(final=True)
        self.log("守护进程退出")

    # -- 一个轮询周期 -------------------------------------------------------
    def cycle(self) -> None:
        self._cycle_count += 1
        if not self.ensure_session():
            self.log_rl("server", "中转服务不可达（{0}），退避中".format(self.server))
            return

        if self._cycle_count % HEARTBEAT_EVERY == 0:
            self.safe("POST", "/sessions/{0}/heartbeat".format(self.session_id), {})

        for app in self.cfg["apps"]:
            resp = self.safe("GET", "/inbox?app={0}&limit=20".format(app))
            if not resp:
                self.log_rl("inbox", "收件箱请求失败（{0}）".format(app))
                continue
            for rec in resp.get("transfers") or []:
                self.handle(rec)

        self.advance_watches()
        self._zb_advance_pending()
        self._zb_process_outbox()

    # -- 处理一条新传输 -----------------------------------------------------
    def handle(self, record: dict) -> None:
        tid = record.get("id", "")
        app = str((record.get("to") or {}).get("app", "")).lower()
        kind = str(record.get("kind", "")).lower()

        if app not in self.cfg["apps"]:
            return
        if app == "zbrush":
            self.handle_zbrush(record, kind)
            return
        if kind != "textures":
            self._claim_finish(tid, "error", {
                "message": "{0} 只接收贴图（kind=textures），本次是 {1}".format(
                    APP_LABELS.get(app, app), kind or "未标注")})
            return

        files = list(record.get("_files_abs") or [])
        if not files:
            files = [os.path.join(self.payload_dir, tid, f)
                     for f in (record.get("files") or [])]
        files = [f for f in files if f and os.path.isfile(f)]
        if not files:
            self._claim_finish(tid, "error", {"message": "没有随传输发来的文件"})
            return

        frm = record.get("from") or {}
        from_app = str(frm.get("app", "")).lower()
        if not from_app:
            self._claim_finish(tid, "error", {
                "message": "发送端未标注 from.app，无法回传"})
            return
        if from_app == app:
            self._claim_finish(tid, "error", {
                "message": "回环：编辑器不接受来源为自己的传输"})
            return

        exe = self.cfg["apps"].get(app, "")
        if not exe or not os.path.isfile(exe):
            self._claim_finish(tid, "error", {
                "message": "未配置 {0} 的可执行文件（当前: {1}）".format(
                    APP_LABELS.get(app, app), exe or "空")})
            return

        resp = self.safe("POST", "/transfers/{0}/claim".format(tid),
                         {"session_id": self.session_id})
        rec = (resp or {}).get("transfer")
        if not rec:
            # 409=被别人领了 / 网络失败，都不是错误，跳过即可
            self.log_rl("claim", "claim {0} 失败（可能已被处理）".format(tid))
            return
        claim_files = [f for f in (rec.get("_files_abs") or []) if os.path.isfile(f)]
        if claim_files:
            files = claim_files

        try:
            proc = subprocess.Popen([exe] + files, close_fds=True)
        except Exception as exc:
            self._claim_finish(tid, "error", {
                "message": "启动 {0} 失败: {1}".format(APP_LABELS.get(app, app), exc)})
            return

        self.watches.append(Watch(rec, files, proc))
        self.handled += 1
        self.event("已在 {0} 打开 {1}（等待保存回传 {2}）".format(
            APP_LABELS.get(app, app),
            "、".join(os.path.basename(f) for f in files),
            APP_LABELS.get(from_app, from_app)))

    # -- ZBrush：任务文件握手 / 一次性 -script 兜底 / 热文件夹回传 -----------
    def handle_zbrush(self, record: dict, kind: str) -> None:
        tid = record.get("id", "")
        if kind != "models":
            self._claim_finish(tid, "error", {
                "message": "ZBrush 只接收模型（kind=models），本次是 {0}".format(
                    kind or "未标注")})
            return

        files = list(record.get("_files_abs") or [])
        if not files:
            files = [os.path.join(self.payload_dir, tid, f)
                     for f in (record.get("files") or [])]
        files = [f for f in files if f and os.path.isfile(f)]
        if not files:
            self._claim_finish(tid, "error", {"message": "没有随传输发来的文件"})
            return
        frm = record.get("from") or {}
        from_app = str(frm.get("app", "")).lower()
        if not from_app:
            self._claim_finish(tid, "error", {
                "message": "发送端未标注 from.app，无法回传"})
            return

        resp = self.safe("POST", "/transfers/{0}/claim".format(tid),
                         {"session_id": self.session_id})
        rec = (resp or {}).get("transfer")
        if not rec:
            self.log_rl("claim", "claim {0} 失败（可能已被处理）".format(tid))
            return
        claim_files = [f for f in (rec.get("_files_abs") or []) if os.path.isfile(f)]
        if claim_files:
            files = claim_files

        if not self.payload_dir:
            self._claim_finish(tid, "error", {
                "message": "拿不到负载目录，无法投递任务"})
            return

        # ① ZBrush 侧常驻桥接活着 -> 任务文件握手（不打断用户当前会话）
        if zb_bridge_alive(self.payload_dir):
            task = zb_write_task(self.payload_dir, rec, files)
            if task:
                self.zb_pending.append({
                    "tid": tid, "files": files, "mode": "handshake",
                    "deadline": time.monotonic() + ZB_IMPORT_TIMEOUT_SEC,
                    "task": task, "from_app": from_app,
                    "asset_name": rec.get("asset_name", ""),
                })
                self.handled += 1
                self.event("已投递模型任务给 ZBrush 桥接：{0}（等待导入回执）".format(
                    "、".join(os.path.basename(f) for f in files)))
                return

        # ② 桥接不在 -> 试一次性 -script 拉起（ZBrush 未运行时才拉得起来）
        exe = self.cfg["apps"].get("zbrush", "")
        if exe and os.path.isfile(exe) and ZB_ONE_SHOT.is_file():
            inbox, _, _ = zb_dirs(self.payload_dir)
            os.makedirs(inbox, exist_ok=True)
            try:
                subprocess.Popen(
                    [exe, "-script", str(ZB_ONE_SHOT)] + files + [inbox, tid],
                    close_fds=True)
                self.zb_pending.append({
                    "tid": tid, "files": files, "mode": "oneshot",
                    "deadline": time.monotonic() + ZB_POPEN_TIMEOUT_SEC,
                    "task": "", "from_app": from_app,
                    "asset_name": rec.get("asset_name", ""),
                })
                self.handled += 1
                self.event("已用一次性脚本拉起 ZBrush 导入：{0}".format(
                    "、".join(os.path.basename(f) for f in files)))
                return
            except Exception as exc:
                self.log("一次性拉起 ZBrush 失败: {0!r}".format(exc))

        # ③ 都走不通 -> 落盘 + D3 托盘通知（用户手动 Tool:Load）
        task = zb_write_task(self.payload_dir, rec, files)
        zb_write_notify(self.state_dir, rec, files)
        self.zb_pending.append({
            "tid": tid, "files": files, "mode": "manual",
            "deadline": time.monotonic() + ZB_IMPORT_TIMEOUT_SEC,
            "task": task, "from_app": from_app,
            "asset_name": rec.get("asset_name", ""),
        })
        self.handled += 1
        self.event("ZBrush 桥接未就绪，模型已落盘：{0}（托盘已提示，"
                   "可在 ZBrush 里 Tool:Load 手动加载）".format(
                       "、".join(os.path.basename(f) for f in files)))

    def _zb_advance_pending(self) -> None:
        now = time.monotonic()
        for p in list(self.zb_pending):
            tid = p["tid"]
            receipt = zb_take_receipt(self.payload_dir, tid)
            if receipt is not None:
                status = str(receipt.get("status", "error"))
                imported = receipt.get("imported") or []
                if status == "ok":
                    self._claim_finish(tid, "done", {
                        "message": "ZBrush 已导入 {0}".format(
                            "、".join(imported) or "、".join(
                                os.path.basename(f) for f in p["files"])),
                    })
                    self.event("ZBrush 导入完成 {0}".format(tid))
                else:
                    self._claim_finish(tid, "error", {
                        "message": "ZBrush 导入失败: {0}".format(
                            receipt.get("error") or "未知错误")})
                self.zb_pending.remove(p)
                continue
            if now >= p["deadline"]:
                # 超时：manual 模式保留任务文件等用户稍后加载；其余清理并取消
                if p["mode"] != "manual" and p.get("task"):
                    try:
                        os.remove(p["task"])
                    except OSError:
                        pass
                if p["mode"] == "oneshot":
                    # 一次性拉起失败（多半是已有实例挡路）-> 转 manual
                    task = zb_write_task(self.payload_dir, {
                        "id": tid, "asset_name": p.get("asset_name", ""),
                        "from": {"app": p.get("from_app", "")}}, p["files"])
                    zb_write_notify(self.state_dir, {
                        "id": tid, "asset_name": p.get("asset_name", "")},
                        p["files"])
                    p["mode"] = "manual"
                    p["task"] = task
                    p["deadline"] = now + ZB_IMPORT_TIMEOUT_SEC
                    self.event("ZBrush 一次性导入超时（可能已有实例在运行），"
                               "已转为落盘 + 托盘提示")
                    continue
                self._claim_finish(tid, "cancelled", {
                    "message": "等待 ZBrush 导入回执超时（文件仍在负载目录，"
                               "可用 ZBrush 的 Tool:Load 手动加载）"})
                self.event("ZBrush 任务 {0} 等待超时".format(tid))
                self.zb_pending.remove(p)

    def _zb_process_outbox(self) -> None:
        """热文件夹：用户从 ZBrush 手动导出的模型 -> 建传输回传默认目标。"""
        if not self.payload_dir:
            return
        _, outbox, _ = zb_dirs(self.payload_dir)
        if not outbox or not os.path.isdir(outbox):
            return
        to_app = str(self.cfg.get("zbrush_outbox_to", "blender"))
        now = time.time()
        for name in sorted(os.listdir(outbox)):
            ext = os.path.splitext(name)[1].lower()
            if ext not in MODEL_EXTS:
                continue
            p = os.path.join(outbox, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if now - st.st_mtime < 2.0:
                continue
            body = {
                "asset_name": os.path.splitext(name)[0],
                "kind": "models",
                "format": ext.lstrip("."),
                "to": {"app": to_app, "project": "", "content_path": ""},
                "from": {"app": "zbrush", "project": ""},
                "options": {"editor": "zbrush", "source_file": name,
                            "hot_folder": True},
                "note": "ZBrush 热文件夹回传",
            }
            try:
                created = self.safe("POST", "/transfers", body)
                rtid = (created or {}).get("transfer", {}).get("id", "")
                if not rtid:
                    self.log_rl("zbout", "热文件夹建传输失败（{0}）".format(name))
                    continue
                _put_file(self.server,
                          "/transfers/{0}/files/{1}".format(rtid, name), p)
                self.safe("POST", "/transfers/{0}/publish".format(rtid), {})
                os.remove(p)
                self.zb_returned += 1
                self.event("热文件夹回传 {0} -> {1}（{2}）".format(name, to_app, rtid))
            except Exception as exc:
                self.log_rl("zbout", "热文件夹回传失败 {0}: {1!r}".format(name, exc))

    def _claim_finish(self, tid: str, status: str, result: dict) -> None:
        self.safe("POST", "/transfers/{0}/claim".format(tid),
                  {"session_id": self.session_id})
        self.safe("POST", "/transfers/{0}/finish".format(tid),
                  {"status": status, "result": result})
        self.log("transfer {0} -> {1}: {2}".format(tid, status, result.get("message", "")))

    # -- 推进监视会话 -------------------------------------------------------
    def advance_watches(self) -> None:
        now = time.monotonic()
        for w in list(self.watches):
            try:
                action = w.advance(now)
            except Exception as exc:
                self.log_rl("watch", "监视 {0} 异常: {1!r}".format(w.tid, exc))
                continue
            if action == "finalize":
                self.finalize(w)
            elif action == "abandon":
                self._claim_finish(w.tid, "cancelled", {
                    "message": "编辑器已关闭且未检测到保存"})
                self.event("{0} 关闭了 {1}，未保存，传输作废".format(
                    APP_LABELS.get(w.app, w.app), w.asset_name))
                self.watches.remove(w)

    # -- 保存后回传 ---------------------------------------------------------
    def finalize(self, w: Watch) -> None:
        names = w.dirty_names or w.changed_files()
        if not names:
            self.watches.remove(w)
            return
        srcs = [os.path.join(w.dir, n) for n in names]
        fmt = os.path.splitext(names[0])[1].lstrip(".").lower() or "png"

        # 回传 options = 原传输 options（如 UE 的 asset_paths/source_files）
        # + 回传标记；发送端据此做「重载」而不是「新导入」
        opts = dict(w.orig_options)
        opts.update({
            "return_of": w.tid,
            "source_file": os.path.basename(w.files[0]) if w.files else "",
            "source_files": [os.path.basename(f) for f in w.files],
            "editor": w.app,
        })
        created = self.safe("POST", "/transfers", {
            "asset_name": w.asset_name,
            "kind": "textures",
            "format": fmt,
            "to": {"app": w.from_app, "project": w.from_project,
                   "content_path": ""},
            "from": {"app": w.app, "project": ""},
            "options": opts,
            "note": "{0} 保存回传".format(APP_LABELS.get(w.app, w.app)),
        })
        rt = (created or {}).get("transfer") or {}
        rtid = rt.get("id", "")
        if not rtid:
            self._claim_finish(w.tid, "done", {
                "message": "检测到保存（{0}），但回传传输创建失败，文件仍在: {1}".format(
                    "、".join(names), w.dir),
                "saved_files": names,
            })
            self.watches.remove(w)
            return

        dest = os.path.join(self.payload_dir, rtid) if self.payload_dir else ""
        if not dest:
            self._claim_finish(w.tid, "done", {
                "message": "检测到保存（{0}），但拿不到负载目录，回传取消，文件仍在: {1}".format(
                    "、".join(names), w.dir),
                "saved_files": names,
            })
            self.watches.remove(w)
            return
        failed = []
        try:
            if not os.path.isdir(dest):
                os.makedirs(dest)
            for src in srcs:
                if not self._copy_retry(src, os.path.join(dest, os.path.basename(src))):
                    failed.append(os.path.basename(src))
        except Exception as exc:
            failed.append(str(exc))

        if failed:
            # 回传传输作废（draft 不能直接 finish，先 claim 再 finish error）
            self.safe("POST", "/transfers/{0}/claim".format(rtid),
                      {"session_id": self.session_id})
            self.safe("POST", "/transfers/{0}/finish".format(rtid), {
                "status": "error",
                "result": {"message": "拷贝保存文件失败: {0}".format("、".join(failed))},
            })
            self._claim_finish(w.tid, "error", {
                "message": "检测到保存，但回传失败（文件被编辑器占用?）: {0}".format(
                    "、".join(failed)),
                "saved_files": names,
            })
            self.event("回传失败：{0}".format("、".join(failed)))
            self.watches.remove(w)
            return

        self.safe("POST", "/transfers/{0}/publish".format(rtid), {})
        self._claim_finish(w.tid, "done", {
            "message": "{0} 已保存，回传 {1} 个文件 -> {2}".format(
                APP_LABELS.get(w.app, w.app), len(names),
                APP_LABELS.get(w.from_app, w.from_app)),
            "returned": True,
            "return_transfer": rtid,
            "saved_files": names,
        })
        self.returned += 1
        self.event("已回传 {0} -> {1}（保存后自动重载）".format(
            "、".join(names), APP_LABELS.get(w.from_app, w.from_app)))
        self.watches.remove(w)

    def _copy_retry(self, src: str, dst: str) -> bool:
        for i in range(COPY_RETRY):
            try:
                shutil.copy2(src, dst)
                return True
            except Exception as exc:
                if i == COPY_RETRY - 1:
                    self.log("拷贝失败 {0} -> {1}: {2}".format(src, dst, exc))
                time.sleep(COPY_RETRY_WAIT)
        return False

    # -- 状态文件 -----------------------------------------------------------
    def write_status(self, final: bool = False) -> None:
        st = {
            "pid": os.getpid(),
            "running": not final,
            "started_at": self.started_at,
            "server": self.server,
            "session_id": self.session_id,
            "state_dir": str(self.state_dir),
            "apps": {name: {"exe": exe, "ok": os.path.isfile(exe)}
                     for name, exe in self.cfg["apps"].items()},
            "watching": [w.brief() for w in self.watches],
            "zbrush_pending": len(self.zb_pending),
            "zbrush_bridge_alive": zb_bridge_alive(self.payload_dir)
            if self.payload_dir else False,
            "handled": self.handled,
            "returned": self.returned,
            "last_event": self.last_event,
            "updated_at": now_iso(),
        }
        try:
            _atomic_write_json(self.status_path, st)
        except Exception as exc:
            self.log_rl("status", "写 status.json 失败: {0!r}".format(exc))


# ---------------------------------------------------------------------------
# 子命令：--check / --stop
# ---------------------------------------------------------------------------

def _read_pidfile(state_dir: Path):
    p = state_dir / "daemon.pid"
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return None


def do_check(cfg: dict) -> int:
    state_dir = Path(cfg["state_dir"])
    pid = _read_pidfile(state_dir)
    alive = bool(pid and _pid_alive(pid))
    info = {
        "running": alive,
        "pid": pid if alive else None,
        "stale_pid": bool(pid and not alive),
        "server": cfg["server"],
        "state_dir": str(state_dir),
        "log": str(state_dir / "daemon.log"),
        "apps": {name: {"exe": exe, "ok": os.path.isfile(exe)}
                 for name, exe in cfg["apps"].items()},
        "watching": [],
        "last_event": "",
    }
    status_file = state_dir / "status.json"
    if status_file.is_file():
        try:
            st = json.loads(status_file.read_text(encoding="utf-8"))
            info["watching"] = st.get("watching") or []
            info["last_event"] = st.get("last_event", "")
            info["handled"] = st.get("handled", 0)
            info["returned"] = st.get("returned", 0)
        except Exception:
            pass
    _safe_print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def do_stop(cfg: dict, force: bool = False) -> int:
    state_dir = Path(cfg["state_dir"])
    pid = _read_pidfile(state_dir)
    if not pid or not _pid_alive(pid):
        _safe_print("[editor_daemon] 守护进程未在运行")
        # 清掉可能残留的 flag / pid 文件
        for name in ("stop.flag", "daemon.pid"):
            try:
                p = state_dir / name
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
        return 0
    flag = state_dir / "stop.flag"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(now_iso(), encoding="utf-8")
    except Exception as exc:
        _safe_print("[editor_daemon] 写停止指令失败: {0}".format(exc))
        return 2
    _safe_print("[editor_daemon] 已发送停止指令（pid={0}），等待退出…".format(pid))
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            _safe_print("[editor_daemon] 已退出")
            try:
                if flag.is_file():
                    flag.unlink()
            except Exception:
                pass
            return 0
        time.sleep(0.5)
    if force:
        _safe_print("[editor_daemon] 宽限期已过，强杀 pid={0}".format(pid))
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        except Exception as exc:
            _safe_print("[editor_daemon] taskkill 失败: {0}".format(exc))
            return 2
        try:
            if flag.is_file():
                flag.unlink()
        except Exception:
            pass
        return 0
    _safe_print("[editor_daemon] 20 秒未退出（可能仍有编辑器在保存回传），"
                "可加 --force 强杀")
    return 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Asset Bridge PS/SAI2 编辑器守护进程")
    ap.add_argument("--server", default="", help="中转服务地址")
    ap.add_argument("--state-dir", default="", help="状态目录（pid/log/status）")
    ap.add_argument("--photoshop", default="", help="Photoshop.exe 路径")
    ap.add_argument("--sai2", default="", help="sai2.exe 路径")
    ap.add_argument("--zbrush", default="", help="ZBrush.exe 路径（2026.1+）")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL_SEC,
                    help="轮询周期（秒）")
    ap.add_argument("--check", action="store_true", help="只打印状态")
    ap.add_argument("--stop", action="store_true", help="请求停止")
    ap.add_argument("--force", action="store_true", help="--stop 宽限后强杀")
    ap.add_argument("--once", action="store_true", help="只跑一个周期（调试）")
    ap.add_argument("--verbose", action="store_true", help="日志同时打印到控制台")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config(args)
    if args.check:
        return do_check(cfg)
    if args.stop:
        return do_stop(cfg, force=args.force)
    d = Daemon(cfg, interval=args.interval, once=args.once, verbose=args.verbose)
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
