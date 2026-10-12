# -*- coding: utf-8 -*-
"""
Asset Bridge :: Unreal Engine 适配器
============================================================================
线程模型（重要，别乱改）：
    UE 的 Python 里，任何资产操作（导入/导出/创建）都必须在 **游戏线程** 执行，
    而网络请求绝对不能卡在游戏线程上。所以：
        - 后台 daemon 线程：轮询 inbox（纯网络 IO）——**心跳也在这里**，绝不阻塞游戏线程
        - Slate tick 回调（游戏线程）：从队列取出记录，真正执行导入
    这个模式和 Blender 的 timers、Maya 的 scriptJob 是同一套思路。

生命周期（Bug4 修复）：
    start() 幂等：已有存活 worker 则先彻底停掉旧实例（用**每次启动独立的
    stop Event**）再启动，杜绝旧实现里 `_stop.clear()` 复活旧线程、每次
    start() 累积一个线程 + 一个 tick 回调的泄漏。
============================================================================
"""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import unreal

from . import exporter, importer
from .client import BridgeClient, BridgeError

# 名称归一化（中文 -> 拼音 ASCII 驼峰）：与本文件同目录的 bridge_names.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bridge_names import to_ascii as _to_ascii
except Exception:  # noqa: BLE001
    _to_ascii = None

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

_client: Optional[BridgeClient] = None
_pending: "queue.Queue" = queue.Queue()
_worker: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None   # 每次 start() 新建，杜绝 clear() 复活旧线程
_tick_handle = None
_hb_counter = 0

CONFIG = {
    "server_url": "http://127.0.0.1:27631",
    "project": "",
    "poll_interval": 0.5,
    "auto_receive": True,
    "spawn_on_import": True,
    "content_path": "/Game/Bridge",
}

# 退避 / 熔断 / 日志限频（与 SP / Maya 同策略）
_fail_streak = 0
_next_poll_ts = 0.0
_last_fail_log_ts = 0.0
_circuit_open = False
_last_probe_ts = 0.0

# 连接状态缓存：由后台 worker 维护，菜单/状态灯只读它（绝不主线程同步打网络）
_SERVER_REACHABLE = False


def _profiles() -> Dict:
    try:
        p = Path(__file__).resolve().parent / "profiles.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        unreal.log_warning(f"[bridge] profiles.json 读取失败: {exc}")
        return {"routes": {}}


def route_profile(src: str) -> Dict:
    return _profiles().get("routes", {}).get(f"{src}>unreal", {})


# 引擎版本必须在**游戏线程**读取：unreal.SystemLibrary 是 UE API，在后台 worker
# 线程里调用会抛 "Attempted to access Unreal API from outside the main game thread"。
# 而 client() 首次是在后台轮询线程（或 asset-bridge-send 线程）里被调用的 ——
# 所以这里预先缓存版本号（模块导入 & start() 都发生在游戏线程），client() 只读缓存。
_engine_version_cache: Optional[str] = None


def _read_engine_version() -> str:
    """读引擎版本（UE API，仅可在游戏线程调用）。非游戏线程时返回占位符而不是抛错。"""
    try:
        return unreal.SystemLibrary.get_engine_version()
    except Exception:
        return "unknown"


def _engine_version() -> str:
    global _engine_version_cache
    if _engine_version_cache is None:
        _engine_version_cache = _read_engine_version()
    return _engine_version_cache


def client() -> BridgeClient:
    global _client
    if _client is None:
        _client = BridgeClient(
            base_url=CONFIG["server_url"],
            app="unreal",
            version=_engine_version(),
            project=CONFIG["project"],
        )
    return _client


# ---------------------------------------------------------------------------
# 退避 / 熔断 / 日志限频（与 SP / Maya 同策略）
# ---------------------------------------------------------------------------


def _on_net_success() -> None:
    """一次网络成功：复位失败计数与熔断，恢复正常轮询间隔。"""
    global _fail_streak, _circuit_open, _next_poll_ts, _SERVER_REACHABLE
    _fail_streak = 0
    _circuit_open = False
    _SERVER_REACHABLE = True
    _next_poll_ts = time.time() + max(0.2, CONFIG["poll_interval"])


def _on_net_failure(what: str) -> None:
    """一次网络失败：指数退避 + 日志限频 + 达到阈值熔断。

    在后台 worker 线程调用；unreal.log_warning 本就可在 worker 线程用。
    """
    global _fail_streak, _next_poll_ts, _last_fail_log_ts, _circuit_open, _last_probe_ts
    global _SERVER_REACHABLE
    _SERVER_REACHABLE = False
    _fail_streak += 1
    # 指数退避：第 n 次失败的间隔 = min(1000 * 2^(n-1), 30000) ms（转秒）
    backoff_s = min(1000 * (2 ** (_fail_streak - 1)), 30000) / 1000.0
    _next_poll_ts = time.time() + backoff_s
    now = time.time()
    if now - _last_fail_log_ts >= 30.0:
        _last_fail_log_ts = now
        unreal.log_warning(f"[bridge] 中转服务通信失败({_fail_streak}): {what}")
    if _fail_streak >= 5 and not _circuit_open:
        _circuit_open = True
        _last_probe_ts = time.time()
        unreal.log_error("[bridge] 中转服务不可达，已暂停轮询。请先启动 start_bridge_server.bat")


# ---------------------------------------------------------------------------
# 后台轮询线程
# ---------------------------------------------------------------------------


def _worker_loop(stop_event: threading.Event) -> None:
    """后台 daemon 线程：注册 + 轮询 inbox + 心跳（纯网络 IO），绝不阻塞游戏线程。

    stop_event 由 start() 每次新建并传给本线程，旧实例 stop() 时置位，
    本线程随即退出；新实例用新的 event 启动，绝不会被旧实例的 clear() 复活。
    """
    c = client()
    global _hb_counter, _SERVER_REACHABLE
    # 注册（纯网络）也放在后台线程，避免点「启动桥接」时卡游戏线程
    try:
        sess = c.register()
        unreal.log(f"[bridge] 已注册 UE 会话 {sess['session_id']}")
    except BridgeError as exc:
        unreal.log_warning(f"[bridge] 连接中转服务失败：{exc}")
    except Exception as exc:
        unreal.log_warning(f"[bridge] 注册异常：{exc}")
    while not stop_event.is_set():
        now = time.time()
        wait = _next_poll_ts - now
        if wait > 0:
            stop_event.wait(wait)       # 退避 / 时间闸门；stop() 时立即返回
            continue

        # 心跳放后台线程（每 30 个 tick 一次），绝不阻塞游戏线程
        _hb_counter += 1
        if _hb_counter % 30 == 1:
            try:
                c.heartbeat()
            except Exception:
                pass

        try:
            for rec in c.inbox(limit=5):
                try:
                    full = c.claim(rec["id"])
                except BridgeError as exc:
                    if "409" not in str(exc):
                        unreal.log_warning(f"[bridge] claim 失败: {exc}")
                    continue
                _pending.put(full)
            _on_net_success()
        except BridgeError:
            _on_net_failure("服务不可达")
        except Exception as exc:
            _on_net_failure(f"轮询异常: {exc}")


# ---------------------------------------------------------------------------
# 游戏线程：真正干活
# ---------------------------------------------------------------------------


def _handle(record: dict) -> None:
    c = client()
    try:
        files = record.get("_files_abs") or []
        kind = record.get("kind", "static_mesh")
        src_app = (record.get("from") or {}).get("app", "blender")

        if kind == "groom":
            abc = [f for f in files if f.lower().endswith(".abc")]
            if not abc:
                raise RuntimeError("没有可导入的 Alembic 文件")
            imported = importer.import_groom(record, abc[0], route_profile(src_app))
            result = {
                "assets": imported.get("all") or [],
                "groom": imported.get("groom") or "",
                "material": imported.get("material") or "",
                "message": imported.get("message", ""),
            }
        elif kind == "textures":
            paths = importer.import_textures(record, files)
            result = {"assets": paths, "message": f"导入 {len(paths)} 张贴图"}
        else:
            fbx = [f for f in files if f.lower().endswith((".fbx", ".obj"))]
            if not fbx:
                raise RuntimeError("没有可导入的模型文件")
            imported = importer.import_model(record, fbx[0], route_profile(src_app))
            # import_model 返回的是结构化结果，网格和材质分开，别再按「取第一个」当网格用
            mesh_path = imported.get("mesh") or ""
            placed = 0
            if CONFIG["spawn_on_import"] and mesh_path:
                placed = importer.spawn_into_level([mesh_path])
            n_mat = len(imported.get("materials") or [])
            result = {
                "assets": imported.get("all") or [],
                "mesh": mesh_path,
                "materials": imported.get("materials") or [],
                "message": (
                    f"导入网格 {mesh_path or '(无)'}，{n_mat} 个材质槽"
                    + (f"，摆放 {placed} 个" if placed else "")
                ),
            }
            if not mesh_path:
                raise RuntimeError(
                    f"导入结果里没有网格资产（拿到的是：{imported.get('all')}）"
                )

        c.finish(record["id"], "done", result)
        unreal.log(f"[bridge] ✓ {record['asset_name']} (rev {record['revision']}) -> {result['message']}")
    except Exception as exc:
        c.finish(record["id"], "error", {"message": f"{type(exc).__name__}: {exc}"})
        unreal.log_error(f"[bridge] ✗ {record['asset_name']}: {exc}")


def _on_tick(delta_time: float) -> None:
    """Slate tick 回调（游戏线程）：只消费队列、调用 UE API，不做任何网络请求。"""
    while not _pending.empty():
        try:
            _handle(_pending.get_nowait())
        except Exception as exc:
            unreal.log_error(f"[bridge] 处理异常: {exc}")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def start(**overrides) -> None:
    global _worker, _tick_handle, _stop_event
    global _fail_streak, _next_poll_ts, _last_fail_log_ts, _circuit_open, _last_probe_ts
    global _engine_version_cache
    CONFIG.update({k: v for k, v in overrides.items() if k in CONFIG})

    # 游戏线程：刷新引擎版本缓存 —— 后台 worker / 发送线程里的 client() 只读缓存，
    # 绝不能在那里调 unreal.SystemLibrary（会抛 outside the main game thread）。
    _engine_version_cache = _read_engine_version()

    # 幂等守卫：已有存活 worker 则先彻底停掉旧实例，避免线程 / tick 回调泄漏
    if _worker is not None and _worker.is_alive():
        unreal.log("[bridge] 适配器已在运行，先停止旧实例再重启")
        stop()

    # 重置退避 / 熔断状态
    _fail_streak = 0
    _next_poll_ts = 0.0
    _last_fail_log_ts = 0.0
    _circuit_open = False
    _last_probe_ts = 0.0
    _SERVER_REACHABLE = False

    # 本次运行独立的 stop 事件，传给 worker 线程（杜绝 clear() 复活旧线程）
    _stop_event = threading.Event()
    _worker = threading.Thread(target=_worker_loop, name="asset-bridge-poll", daemon=True,
                               args=(_stop_event,))
    _worker.start()
    _tick_handle = unreal.register_slate_post_tick_callback(_on_tick)
    unreal.log("[bridge] Unreal 适配器已启动")


def stop() -> None:
    global _worker, _tick_handle, _stop_event, _SERVER_REACHABLE
    _SERVER_REACHABLE = False
    if _stop_event is not None:
        _stop_event.set()
    if _tick_handle is not None:
        try:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
        except Exception:
            pass
        _tick_handle = None
    if _worker is not None and _worker.is_alive():
        _worker.join(timeout=2.0)
    _worker = None
    _stop_event = None


# ---------------------------------------------------------------------------
# 主动发送
# ---------------------------------------------------------------------------


def send_selected_assets(target_app: str = "blender", note: str = "") -> int:
    """把内容浏览器里选中的资产发到目标软件。

    游戏线程只做「选中查询 + 本地导出」（UE 资产导出 API 必须在游戏线程），
    网络部分（建 transfer / 落盘中转目录或上传 / publish）全部丢到后台线程，
    避免服务器没开时菜单点击被同步 HTTP 超时卡住 2~3 秒。
    """
    paths = exporter.selected_asset_paths()
    if not paths:
        unreal.log_warning("[bridge] 内容浏览器里没有选中资产")
        return 0

    # 中文名 -> 拼音驼峰（UE 资产名必须是 ASCII，否则导入会失败/乱码）
    raw_name = Path(paths[0]).name
    asset_name = _to_ascii(raw_name) if _to_ascii else raw_name

    # 游戏线程：仅本地导出（不产生网络 IO，不卡 UI）
    import tempfile
    import shutil
    work_dir = tempfile.mkdtemp(prefix="ab_send_")
    try:
        files = exporter.export_assets(paths, work_dir, {})
    except Exception as exc:
        unreal.log_error(f"[bridge] 导出失败: {exc}")
        shutil.rmtree(work_dir, ignore_errors=True)
        return 0
    if not files:
        unreal.log_warning("[bridge] 导出失败：未生成任何文件")
        shutil.rmtree(work_dir, ignore_errors=True)
        return 0

    meta = {
        "asset_name": asset_name,
        "kind": "static_mesh",
        "format": "fbx",
        "to": {"app": target_app, "project": CONFIG["project"], "content_path": ""},
        "unit": {"scene_unit": "cm", "up_axis": "Z", "forward_axis": "X", "unit_scale": 1.0},
        "options": {"mode": "update"},
        "objects": [{"name": Path(p).name, "type": "ASSET"} for p in paths],
        "note": note,
    }
    # 后台线程完成网络（建 transfer / 落盘或上传 / publish）
    threading.Thread(
        target=_send_bg, name="asset-bridge-send",
        args=(target_app, meta, files, work_dir), daemon=True
    ).start()
    unreal.log(f"[bridge] 正在后台推送 {len(files)} 个资产 -> {target_app} …")
    return len(files)


def _send_bg(target_app: str, meta: dict, files: list, work_dir: str) -> None:
    """后台线程：建 transfer + 落盘中转目录或上传 + publish。不碰任何 UE 资产 API。"""
    import shutil
    c = client()
    try:
        rec = c.create_transfer(meta)
        tid = rec["id"]
        pd = c.payload_dir()
        if pd:
            dest = Path(pd) / tid
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                try:
                    shutil.move(f, str(dest / Path(f).name))
                except Exception:
                    pass
        else:
            for f in files:
                c.put_file(tid, f)
        rec = c.publish(tid)
        unreal.log(f"[bridge] ✓ 已发送 {len(files)} 个资产 -> {target_app} (rev {rec['revision']})")
    except Exception as exc:
        unreal.log_error(f"[bridge] ✗ 发送失败: {type(exc).__name__}: {exc}")
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def import_local_groom(abc_path: str, asset_name: str = "", content_path: str = "/Game/Bridge/Groom") -> Dict:
    """手动把本地 .abc 毛发导入为 Groom（不走桥接，给高级用户 / 排错用）。

    例：import asset_bridge_unreal; asset_bridge_unreal.import_local_groom("D:/hair.abc")
    """
    from pathlib import Path
    record = {
        "asset_name": asset_name or Path(abc_path).stem,
        "kind": "groom",
        "format": "abc",
        "to": {"app": "unreal", "content_path": content_path},
        "options": {"mode": "update", "bind_to_character": False},
    }
    res = importer.import_groom(record, abc_path, {})
    unreal.log(f"[bridge] 手动导入 Groom: {res.get('message')}")
    return res
