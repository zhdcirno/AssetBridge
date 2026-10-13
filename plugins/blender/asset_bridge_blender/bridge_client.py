# -*- coding: utf-8 -*-
"""
Blender 侧的中转服务客户端。

只用标准库 urllib —— Blender 自带 Python 里不一定有 requests。
所有网络调用都必须设短超时，否则轮询会卡住界面。
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

API = "/api/v1"

# 关键：本机回环必须绕过代理。很多公司网络设了 HTTP_PROXY，
# urllib 默认会把它用在本机地址上，结果是 502 upstream connect failed。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:27631",
        app: str = "blender",
        version: str = "",
        project: str = "",
        timeout: float = 3.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.app = app
        self.version = version
        self.project = project
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._payload_dir: Optional[Path] = None

    # ---------------------------------------------------------------- 底层
    def _url(self, path: str) -> str:
        return f"{self.base_url}{API}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        data: Optional[bytes] = None,
        content_type: str = "application/json",
        timeout: Optional[float] = None,
    ) -> Any:
        url = self._url(path)
        req = urllib.request.Request(url=url, method=method)
        if data is not None:
            req.add_header("Content-Type", content_type)
            req.data = data
        elif body is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        try:
            with _OPENER.open(
                req, timeout=timeout if timeout is not None else self.timeout
            ) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise BridgeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise BridgeError(f"无法连接中转服务 {self.base_url}：{exc.reason}") from exc

    # ---------------------------------------------------------------- 会话
    def register(self) -> dict:
        sess = self._request(
            "POST",
            "/sessions",
            {
                "app": self.app,
                "version": self.version,
                "project": self.project,
                "host": os.environ.get("COMPUTERNAME", ""),
                "pid": os.getpid(),
                "capabilities": {"formats": ["fbx", "usd", "gltf"], "send": True, "receive": True},
            },
        )["session"]
        self.session_id = sess["session_id"]
        return sess

    def heartbeat(self) -> None:
        if not self.session_id:
            self.register()
            return
        try:
            self._request("POST", f"/sessions/{self.session_id}/heartbeat", {}, timeout=1.5)
        except BridgeError:
            self.session_id = None

    # ---------------------------------------------------------------- 目录
    def payload_dir(self, refresh: bool = False) -> Optional[Path]:
        """拿到服务端负载目录。同机时直接落盘，省掉一次上传往返。"""
        if self._payload_dir and not refresh and self._payload_dir.is_dir():
            return self._payload_dir
        try:
            cfg = self._request("GET", "/config", timeout=2.0)
            p = Path(cfg["payload_dir"])
            if p.is_dir():
                self._payload_dir = p
                return p
        except Exception:
            pass
        return None

    # ---------------------------------------------------------------- 发送
    def create_transfer(self, meta: dict) -> dict:
        meta = dict(meta)
        meta.setdefault("id", uuid.uuid4().hex)
        meta.setdefault("from", {})
        meta["from"].update({"app": self.app, "version": self.version})
        # from.project 供「编辑器保存后回传」定位发送端工程（PS/SAI2 守护用）
        meta["from"].setdefault("project", self.project)
        return self._request("POST", "/transfers", meta)["transfer"]

    def put_file(self, transfer_id: str, file_path: Path) -> dict:
        return self._request(
            "PUT",
            f"/transfers/{transfer_id}/files/{file_path.name}",
            data=file_path.read_bytes(),
            content_type="application/octet-stream",
            timeout=30.0,
        )

    def publish(self, transfer_id: str) -> dict:
        return self._request("POST", f"/transfers/{transfer_id}/publish", {})["transfer"]

    def send(
        self,
        meta: dict,
        file_paths: List[Path],
        use_direct_write: bool = True,
    ) -> dict:
        """完整发送流程：建记录 -> 落文件 -> 发布。

        use_direct_write: 同机时直接写进服务端 payload 目录（快）；
        否则用 PUT 上传（慢但可跨机）。
        """
        rec = self.create_transfer(meta)
        tid = rec["id"]

        pd = self.payload_dir() if use_direct_write else None
        if pd:
            dst_dir = pd / tid
            dst_dir.mkdir(parents=True, exist_ok=True)
            for fp in file_paths:
                shutil.copy2(fp, dst_dir / fp.name)
        else:
            for fp in file_paths:
                self.put_file(tid, Path(fp))

        return self.publish(tid)

    # ---------------------------------------------------------------- 接收
    def inbox(self, limit: int = 20) -> List[dict]:
        q = f"/inbox?app={self.app}&limit={limit}"
        if self.project:
            q += f"&project={urllib.parse.quote(self.project)}"
        return self._request("GET", q, timeout=2.0).get("transfers", [])

    def claim(self, transfer_id: str) -> dict:
        return self._request(
            "POST",
            f"/transfers/{transfer_id}/claim",
            {"session_id": self.session_id},
        )["transfer"]

    def finish(self, transfer_id: str, status: str = "done", result: Optional[dict] = None) -> dict:
        return self._request(
            "POST",
            f"/transfers/{transfer_id}/finish",
            {"status": status, "result": result or {}},
        )["transfer"]

    def history(self, limit: int = 30) -> List[dict]:
        return self._request("GET", f"/transfers?limit={limit}", timeout=2.0).get("transfers", [])


def is_server_reachable(base_url: str = "http://127.0.0.1:27631", timeout: float = 1.0) -> bool:
    """轻量连通性探测：GET /api/v1/health，供面板上的连接状态灯使用。

    只返回 True/False，绝不抛异常。
    """
    try:
        req = urllib.request.Request(url=f"{base_url.rstrip('/')}{API}/health", method="GET")
        with _OPENER.open(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
