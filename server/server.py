#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asset-bridge :: bridge_server.server
============================================================================
本地中转服务 —— Blender / UE / Maya / Substance Painter 之间收发资产的中枢。

设计约束（决定了实现方式，别随便改）：
  1. **零第三方依赖**：只用 Python 标准库。因为各 DCC 内嵌的 Python
     （Blender 4.x 自带 3.11、UE5 自带 3.9/3.11、Maya 自带 3.x）都不能保证
     装了 requests/websockets，服务也不能强求宿主环境装包。
  2. **文件 + 元数据分离**：大文件走磁盘（或 PUT 上传），控制面走 JSON。
     这样即便插件崩了，资产文件也不会丢，可以手动补导入。
  3. **拉模式（polling）为主**：Blender/UE/Maya/SP 的主线程都不是自由线程，
     起 socket 长连接在子线程里回调用 API 会崩。统一用「子线程/定时器轮询
     inbox，把结果丢进主线程队列执行」，跨软件行为一致。
  4. **传输记录是事实来源**：每次发送都是一条带 revision 的 transfer 记录，
     同名资产重复发送 = 增量更新，而不是生成 SM_Chair_1。

启动：
    python bridge_server/server.py --root ~/.asset_bridge --port 27631
============================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

# 名称归一化（中文 -> 拼音 ASCII 驼峰）：与本文件同目录的 bridge_names.py。
# 表文件缺失时退化为「拒绝非法名」的旧行为，不影响服务启动。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bridge_names import to_ascii as _name_to_ascii
except Exception:  # noqa: BLE001
    _name_to_ascii = None

SCHEMA_VERSION = 1
API = "/api/v1"
DEFAULT_PORT = 27631
SESSION_TTL_SEC = 120          # 超过这个时间没有心跳，会话标记为 offline
MAX_BODY_BYTES = 8 * 1024 * 1024

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")

# 优雅关闭用的句柄：run_server() 启动时填入。
# 为什么需要它：托盘（或外部脚本）想停服务时，直接杀进程在「跨会话/无
# SeDebugPrivilege」的情况下会被拒绝访问（实测：托盘起的服务由终端另起时，
# Stop-Process 报「拒绝访问」）。走 HTTP 让服务自己退出就没有权限问题。
_HTTPD = None


def request_shutdown(delay: float = 0.3) -> None:
    """让 HTTP 服务优雅退出。

    必须在**另一个线程**里调用 shutdown()，否则会和 serve_forever() 死锁。
    """
    def _go() -> None:
        time.sleep(delay)
        try:
            if _HTTPD is not None:
                _HTTPD.shutdown()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True, name="asset-bridge-shutdown").start()

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def safe_filename(name: str) -> str:
    """防止 ../../ 之类的路径穿越，只允许纯文件名。"""
    name = os.path.basename(unquote(name or "")).strip()
    name = name.replace("\\", "/").split("/")[-1]
    if not name or name in (".", ".."):
        raise ValueError("illegal filename")
    return name


def sanitize_asset_name(name: str) -> str:
    """把资产名规整成 ASCII 安全形式。

    纯 ASCII 名字**原样保留**（不改变已有资产名，避免打断同名更新链路）；
    含中文时转成拼音驼峰（如「木头方块」-> MuTouFangKuai），
    再统一校验。这样各 DCC 发中文命名也能被接收，不会出现乱码/400。
    """
    name = (name or "").strip()
    if SAFE_NAME_RE.match(name):
        return name
    if _name_to_ascii is not None:
        converted = _name_to_ascii(name)
        if SAFE_NAME_RE.match(converted):
            return converted
    raise ValueError(
        f"asset_name 只允许 字母/数字/_/-/. ，收到: {name!r}"
    )


# ---------------------------------------------------------------------------
# 状态存储
# ---------------------------------------------------------------------------


class Store:
    """线程安全的 JSON 状态存储。

    为什么用 JSON 而不是 SQLite：状态量极小（百级别 transfer），
    JSON 可以直接被人肉查看/手改，排查问题时非常省事。
    """

    def __init__(self, root: Path):
        self.root = root
        self.payload_dir = root / "payloads"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "sessions": {},
            "transfers": {},
            "seq": 0,
        }
        self._load()

    # ---- 持久化 ---------------------------------------------------------
    def _load(self) -> None:
        if self.state_path.exists():
            try:
                self.data = json.loads(
                    self.state_path.read_text(encoding="utf-8")
                )
                self.data.setdefault("sessions", {})
                self.data.setdefault("transfers", {})
                self.data.setdefault("seq", 0)
            except Exception as exc:  # 状态文件坏了就重来，别让服务起不来
                print(f"[bridge] state.json 损坏，已备份并重建: {exc}")
                backup = self.state_path.with_suffix(
                    f".corrupt-{int(time.time())}.json"
                )
                try:
                    self.state_path.replace(backup)
                except OSError:
                    pass

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    # ---- 会话 -----------------------------------------------------------
    def register_session(self, payload: dict) -> dict:
        app = str(payload.get("app", "")).lower().strip()
        if not app:
            raise ValueError("app 必填 (blender/unreal/maya/substance_painter)")
        sid = payload.get("session_id") or uuid.uuid4().hex[:16]
        with self.lock:
            sess = self.data["sessions"].get(sid, {})
            sess.update(
                {
                    "session_id": sid,
                    "app": app,
                    "version": payload.get("version", ""),
                    "project": payload.get("project", ""),
                    "host": payload.get("host", ""),
                    "pid": payload.get("pid"),
                    "capabilities": payload.get("capabilities", {}),
                    "status": "online",
                    "last_seen": now_iso(),
                }
            )
            self.data["sessions"][sid] = sess
            self._save()
        return sess

    def heartbeat(self, sid: str) -> Optional[dict]:
        with self.lock:
            sess = self.data["sessions"].get(sid)
            if not sess:
                return None
            sess["status"] = "online"
            sess["last_seen"] = now_iso()
            self._save()
            return sess

    def prune_sessions(self) -> None:
        """把断线的 DCC 标记为 offline（不删除，保留历史便于排查）。"""
        cutoff = time.time() - SESSION_TTL_SEC
        with self.lock:
            changed = False
            for sess in self.data["sessions"].values():
                try:
                    ts = datetime.fromisoformat(
                        sess["last_seen"].replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    continue
                if ts < cutoff and sess.get("status") == "online":
                    sess["status"] = "offline"
                    changed = True
            if changed:
                self._save()

    # ---- transfer -------------------------------------------------------
    def _next_revision(self, asset_name: str, to_app: str, to_project: str) -> int:
        n = 1
        for t in self.data["transfers"].values():
            if (
                t.get("asset_name") == asset_name
                and (t.get("to") or {}).get("app") == to_app
                and (t.get("to") or {}).get("project") == to_project
            ):
                n += 1
        return n

    def create_transfer(self, payload: dict) -> dict:
        asset_name = sanitize_asset_name(payload.get("asset_name", ""))
        to = payload.get("to") or {}
        to_app = str(to.get("app", "")).lower().strip()
        if not to_app:
            raise ValueError("to.app 必填（目标软件，如 unreal），或填 '*' 广播")

        tid = payload.get("id") or uuid.uuid4().hex
        if not SAFE_NAME_RE.match(tid):
            raise ValueError("transfer id 只允许 字母/数字/_/-/.")

        with self.lock:
            if tid in self.data["transfers"]:
                raise ValueError(f"transfer {tid} 已存在")

            self.data["seq"] += 1
            rec = {
                "id": tid,
                "schema": SCHEMA_VERSION,
                "seq": self.data["seq"],
                "asset_name": asset_name,
                "kind": payload.get("kind", "static_mesh"),
                "format": (payload.get("format") or "fbx").lower(),
                "files": [safe_filename(f) for f in (payload.get("files") or [])],
                "from": payload.get("from") or {},
                "to": {
                    "app": to_app,
                    "project": to.get("project", ""),
                    "content_path": to.get("content_path", "/Game/Bridge"),
                },
                "unit": payload.get("unit") or {},
                "options": payload.get("options") or {},
                "objects": payload.get("objects") or [],
                "note": payload.get("note", ""),
                "status": "draft",
                "revision": self._next_revision(
                    asset_name, to_app, to.get("project", "")
                ),
                "created_at": now_iso(),
                "published_at": None,
                "claimed_at": None,
                "finished_at": None,
                "claimed_by": None,
                "result": {},
            }
            self.data["transfers"][tid] = rec
            (self.payload_dir / tid).mkdir(parents=True, exist_ok=True)
            self._save()
        return rec

    def transfer_dir(self, tid: str) -> Path:
        return self.payload_dir / tid

    def set_status(self, tid: str, status: str, **fields) -> dict:
        with self.lock:
            rec = self.data["transfers"].get(tid)
            if not rec:
                raise KeyError(tid)
            rec["status"] = status
            rec.update(fields)
            self._save()
            return rec

    def inbox(self, app: str, project: str = "", limit: int = 50) -> List[dict]:
        app = (app or "").lower()
        with self.lock:
            out = []
            for t in self.data["transfers"].values():
                if t["status"] != "queued":
                    continue
                to = t.get("to") or {}
                if to.get("app") != app and to.get("app") != "*":
                    continue
                if project and to.get("project") and to.get("project") != project:
                    continue
                out.append(t)
            out.sort(key=lambda x: x["created_at"])
            return out[:limit]

    def list_transfers(self, limit: int = 50, status: str = "") -> List[dict]:
        with self.lock:
            out = list(self.data["transfers"].values())
            if status:
                out = [t for t in out if t["status"] == status]
            out.sort(key=lambda x: x.get("seq", 0), reverse=True)
            return out[:limit]

    def get_transfer(self, tid: str) -> Optional[dict]:
        with self.lock:
            return self.data["transfers"].get(tid)

    def gc(self, keep: int = 500) -> int:
        """payload 目录清理：只保留最近 keep 条 transfer 的文件。"""
        with self.lock:
            recs = sorted(
                self.data["transfers"].values(),
                key=lambda x: x.get("seq", 0),
                reverse=True,
            )
            keep_ids = {r["id"] for r in recs[:keep]}
            removed = 0
            for d in self.payload_dir.iterdir():
                if d.is_dir() and d.name not in keep_ids:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            return removed


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "asset-bridge/1.0"
    store: Store  # 由 run_server 注入

    # ---- 基础 -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 静音默认 stderr 日志
        if os.environ.get("BRIDGE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, obj: Any) -> None:
        self._json(200, obj)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ---- 路由 -----------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    # 客户端中途断连（WinError 10053 ConnectionAbortedError / 10054
    # ConnectionResetError / BrokenPipeError，都是 ConnectionError 子类）：
    # 探活客户端超时关连接是常态，不能当 500 抛栈刷 server_console.log
    def _client_gone(self, what: str, exc: Exception) -> None:
        if os.environ.get("BRIDGE_VERBOSE"):
            sys.stderr.write("[bridge] %s client disconnected (%s): %s\n"
                             % (self.address_string(), what, exc))

    def do_GET(self) -> None:
        try:
            self._route_get()
        except ConnectionError as exc:
            self._client_gone("GET", exc)
        except Exception as exc:
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except ConnectionError:
                self._client_gone("GET/500", exc)

    def do_POST(self) -> None:
        try:
            self._route_post()
        except ConnectionError as exc:
            self._client_gone("POST", exc)
        except ValueError as exc:
            try:
                self._err(400, str(exc))
            except ConnectionError:
                self._client_gone("POST/400", exc)
        except KeyError as exc:
            try:
                self._err(404, f"not found: {exc}")
            except ConnectionError:
                self._client_gone("POST/404", exc)
        except Exception as exc:
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except ConnectionError:
                self._client_gone("POST/500", exc)

    def do_PUT(self) -> None:
        try:
            self._route_put()
        except ConnectionError as exc:
            self._client_gone("PUT", exc)
        except ValueError as exc:
            try:
                self._err(400, str(exc))
            except ConnectionError:
                self._client_gone("PUT/400", exc)
        except KeyError as exc:
            try:
                self._err(404, f"not found: {exc}")
            except ConnectionError:
                self._client_gone("PUT/404", exc)
        except Exception as exc:
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except ConnectionError:
                self._client_gone("PUT/500", exc)

    # -- GET
    def _route_get(self) -> None:
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if p in ("", "/", "/api", API):
            self._ok(
                {
                    "service": "asset-bridge",
                    "schema": SCHEMA_VERSION,
                    "root": str(self.store.root),
                    "payload_dir": str(self.store.payload_dir),
                    "endpoints": [
                        "GET  /api/v1/health",
                        "GET  /api/v1/config",
                        "POST /api/v1/sessions",
                        "POST /api/v1/sessions/<id>/heartbeat",
                        "GET  /api/v1/sessions",
                        "POST /api/v1/transfers",
                        "PUT  /api/v1/transfers/<id>/files/<name>",
                        "POST /api/v1/transfers/<id>/publish",
                        "GET  /api/v1/inbox?app=unreal&project=P",
                        "POST /api/v1/transfers/<id>/claim",
                        "POST /api/v1/transfers/<id>/finish",
                        "POST /api/v1/transfers/<id>/retry",
                        "GET  /api/v1/transfers?limit=50",
                        "GET  /api/v1/transfers/<id>",
                        "POST /api/v1/shutdown",
                    ],
                }
            )
            return

        if p == f"{API}/health":
            self.store.prune_sessions()
            self._ok({"ok": True, "time": now_iso()})
            return

        if p == f"{API}/config":
            self._ok(
                {
                    "root": str(self.store.root),
                    "payload_dir": str(self.store.payload_dir),
                    "schema": SCHEMA_VERSION,
                }
            )
            return

        if p == f"{API}/sessions":
            self.store.prune_sessions()
            self._ok({"sessions": list(self.store.data["sessions"].values())})
            return

        if p == f"{API}/inbox":
            app = q.get("app", "")
            if not app:
                self._err(400, "需要 ?app=unreal 之类的目标软件名")
                return
            items = self.store.inbox(app, q.get("project", ""), int(q.get("limit", 50)))
            self._ok({"transfers": items, "count": len(items)})
            return

        if p == f"{API}/transfers":
            self._ok(
                {
                    "transfers": self.store.list_transfers(
                        int(q.get("limit", 50)), q.get("status", "")
                    )
                }
            )
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)$", p)
        if m:
            rec = self.store.get_transfer(m.group(1))
            if not rec:
                self._err(404, "transfer not found")
                return
            rec = dict(rec)
            rec["_files_abs"] = [
                str(self.store.transfer_dir(rec["id"]) / f)
                for f in rec.get("files", [])
            ]
            self._ok(rec)
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/files/([^/]+)$", p)
        if m:  # 方便调试：直接下载 payload
            tid, name = m.group(1), safe_filename(m.group(2))
            fp = self.store.transfer_dir(tid) / name
            if not fp.exists():
                self._err(404, "file not found")
                return
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        self._err(404, f"unknown route {p}")

    # -- POST
    def _route_post(self) -> None:
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        body = self._read_json()

        # 优雅关闭：先回包，再让 serve_forever() 退出（供托盘/外部脚本使用）
        if p == f"{API}/shutdown":
            self._ok({"ok": True, "msg": "服务即将退出"})
            request_shutdown()
            return

        if p == f"{API}/sessions":
            self._ok({"session": self.store.register_session(body)})
            return

        m = re.match(rf"^{API}/sessions/([A-Za-z0-9_\-\.]+)/heartbeat$", p)
        if m:
            sess = self.store.heartbeat(m.group(1))
            if not sess:
                self._err(404, "session not found")
                return
            self._ok({"session": sess})
            return

        if p == f"{API}/transfers":
            rec = self.store.create_transfer(body)
            if body.get("publish"):
                rec = self.store.set_status(rec["id"], "queued", published_at=now_iso())
            self._ok({"transfer": rec, "upload_url": f"{API}/transfers/{rec['id']}/files/"})
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/publish$", p)
        if m:
            tid = m.group(1)
            rec = self.store.get_transfer(tid)
            if not rec:
                self._err(404, "transfer not found")
                return
            if rec["status"] not in ("draft", "queued"):
                self._err(409, f"当前状态 {rec['status']} 不能发布")
                return
            d = self.store.transfer_dir(tid)
            files = sorted(x.name for x in d.iterdir() if x.is_file())
            rec = self.store.set_status(
                tid, "queued", files=files, published_at=now_iso()
            )
            self._ok({"transfer": rec})
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/claim$", p)
        if m:
            tid = m.group(1)
            rec = self.store.get_transfer(tid)
            if not rec:
                self._err(404, "transfer not found")
                return
            if rec["status"] != "queued":
                self._err(409, f"已被处理，当前状态 {rec['status']}")
                return
            rec = self.store.set_status(
                tid,
                "claimed",
                claimed_at=now_iso(),
                claimed_by=(body.get("session_id") or ""),
            )
            rec = dict(rec)
            rec["_files_abs"] = [
                str(self.store.transfer_dir(tid) / f) for f in rec.get("files", [])
            ]
            self._ok({"transfer": rec})
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/finish$", p)
        if m:
            tid = m.group(1)
            rec = self.store.get_transfer(tid)
            if not rec:
                self._err(404, "transfer not found")
                return
            status = body.get("status", "done")
            if status not in ("done", "error", "cancelled"):
                self._err(400, "status 只能是 done/error/cancelled")
                return
            # 只有真正被认领过的记录才能出结果。
            # 否则一份从没被任何 DCC 领走的 draft/queued 也能被记成 "已完成"，
            # 排查丢件时会看到一片虚假的 done。
            if rec["status"] not in ("claimed", "error"):
                self._err(
                    409,
                    f"当前状态 {rec['status']} 不能 finish，需先 claim"
                    f"（error 状态可直接重试 finish）",
                )
                return
            rec = self.store.set_status(
                tid,
                status,
                finished_at=now_iso(),
                result=body.get("result", {}),
            )
            self._ok({"transfer": rec})
            return

        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/retry$", p)
        if m:
            tid = m.group(1)
            rec = self.store.get_transfer(tid)
            if not rec:
                self._err(404, "transfer not found")
                return
            rec = self.store.set_status(tid, "queued", claimed_by=None, result={})
            self._ok({"transfer": rec})
            return

        if p == f"{API}/gc":
            self._ok({"removed": self.store.gc(int(body.get("keep", 500)))})
            return

        self._err(404, f"unknown route {p}")

    # -- PUT（文件上传，供没有共享磁盘权限的客户端如 SP 使用）
    def _route_put(self) -> None:
        u = urlparse(self.path)
        p = u.path
        m = re.match(rf"^{API}/transfers/([A-Za-z0-9_\-\.]+)/files/(.+)$", p)
        if not m:
            self._err(404, f"unknown route {p}")
            return
        tid, name = m.group(1), safe_filename(m.group(2))
        rec = self.store.get_transfer(tid)
        if not rec:
            self._err(404, "transfer not found")
            return

        d = self.store.transfer_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        target = d / name

        total = int(self.headers.get("Content-Length") or 0)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            total = 0  # 分块传输，边读边写
        remaining = total
        with open(target, "wb") as fh:
            while True:
                if remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    remaining -= len(chunk)
                else:
                    chunk = self.rfile.read(65536) if total == 0 else b""
                if not chunk:
                    break
                fh.write(chunk)
                if total and remaining <= 0:
                    break

        self.store.set_status(
            tid, rec["status"], files=sorted(x.name for x in d.iterdir() if x.is_file())
        )
        self._ok({"file": name, "bytes": target.stat().st_size, "path": str(target)})


def make_handler(store: Store):
    class _H(BridgeHandler):
        pass

    _H.store = store
    return _H


def port_in_use(host: str, port: int, timeout: float = 0.6) -> bool:
    """启动前探一下端口。

    Windows 上 SO_REUSEADDR 允许两个进程同时 bind 同一个端口，
    结果是谁接请求不确定 —— 出现过「连的是旧实例、数据落在别的目录」的诡异问题，
    所以宁可直接拒绝启动。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def run_server(root: Path, host: str, port: int) -> int:
    global _HTTPD
    if port_in_use(host, port):
        print(f"[bridge] 端口 {host}:{port} 已被占用，已有实例在跑。")
        print("         请先结束旧进程，或换一个 --port。")
        return 2

    store = Store(root)
    handler = make_handler(store)
    httpd = ThreadingHTTPServer((host, port), handler)
    _HTTPD = httpd
    print("=" * 64)
    print(" asset-bridge 中转服务已启动")
    print(f"   监听   : http://{host}:{port}{API}")
    print(f"   数据根 : {root}")
    print(f"   负载区 : {store.payload_dir}")
    print("   Ctrl+C 退出")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] 已停止")
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="asset-bridge 本地中转服务")
    ap.add_argument(
        "--root",
        default=os.environ.get(
            "ASSET_BRIDGE_ROOT",
            str(Path.home() / ".asset_bridge"),
        ),
        help="数据与负载根目录（默认 ~/.asset_bridge）",
    )
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return run_server(root, args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
