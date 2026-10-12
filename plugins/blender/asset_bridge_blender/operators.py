# -*- coding: utf-8 -*-
"""插件的所有 Operator，以及定时器实际执行的收发逻辑。"""
import tempfile
from pathlib import Path
from typing import List

import bpy

from . import bridge_state, io_fbx
from . import bridge_client
from .bridge_client import BridgeError
from .bridge_names import to_ascii as _to_ascii

import queue
import threading
import time

# 心跳节流：不是每次轮询都打心跳
_hb_counter = {"n": 0}

# ---------------------------------------------------------------------------
# 后台网络线程：所有 HTTP IO 都在这里跑，主线程（bpy.app.timers / 面板 draw）
# 只消费队列、执行 bpy 导入、读状态缓存——绝不发起网络请求，避免服务器没开时
# 连接挂起把主线程卡到超时。
# ---------------------------------------------------------------------------
_CFG = {           # 主线程每轮同步进来的偏好，后台线程只读
    "server_url": "http://127.0.0.1:27631",
    "project": "",
    "version": "",
    "auto_receive": True,
    "poll_interval": 2.0,
    "running": False,
}
_STATUS = {"ok": False, "ts": 0.0}        # 连接状态缓存（后台线程更新，draw 只读）
_pending = queue.Queue()                  # 后台 claim 到的完整记录 -> 主线程导入
_result_queue = queue.Queue()             # 主线程导入结果 -> 后台线程上报 finish
_poll_thread = None
_stop_event = None
_poll_hb = {"n": 0}
_fail = {"streak": 0, "next": 0.0, "circuit": False, "lastlog": 0.0}


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _client():
    prefs = _prefs()
    import bpy as _b

    return bridge_state.get_client(
        base_url=prefs.server_url,
        version=".".join(str(x) for x in _b.app.version),
        project=prefs.project,
    )


def _clean_name(name: str) -> str:
    """归一化成 ASCII 安全名。

    纯 ASCII 原样保留（避免打断已有的同名更新链路）；
    中文转拼音驼峰（「木头方块」-> MuTouFangKuai）。

    注意：旧实现用 `ch.isalnum()` 过滤非法字符，但 Python 里中文的
    `isalnum()` 返回 True，中文会被原样留下 -> 服务端 400 / 目标软件乱码。
    现统一走拼音表（bridge_names.py）。
    """
    return _to_ascii(name, default="Asset")


def _target_objects(context) -> List[bpy.types.Object]:
    objs = list(context.selected_objects)
    if not objs and context.view_layer.objects.active:
        objs = [context.view_layer.objects.active]
    return [o for o in objs if o.type in {"MESH", "ARMATURE", "EMPTY"}]


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------


class BRIDGE_OT_test_connection(bpy.types.Operator):
    bl_idname = "bridge.test_connection"
    bl_label = "测试中转服务连接"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            client = _client()
            client.register()
            sess = client._request("GET", "/sessions", timeout=2.0)
            apps = ", ".join(
                f"{s['app']}({s['status']})" for s in sess.get("sessions", [])
            )
            self.report({"INFO"}, f"已连接。在线端：{apps or '无'}")
        except BridgeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


class BRIDGE_OT_send_selection(bpy.types.Operator):
    bl_idname = "bridge.send_selection"
    bl_label = "发送到目标软件"
    bl_description = "导出选中对象（含材质槽 / UV / 骨架）并推送到中转服务"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = _prefs()
        objects = _target_objects(context)
        if not objects:
            self.report({"WARNING"}, "没有选中任何网格 / 骨架")
            return {"CANCELLED"}

        target = prefs.target_app
        profile = bridge_state.route_profile("blender", target)

        # 注意：bridge_asset_name 是注册在 Scene 上的 RNA 属性，不是 ID 自定义属性。
        # Blender 里 scene["x"] / scene.get("x") 只认 ID 自定义属性，取 RNA 属性会拿不到，
        # 必须用 getattr（Blender 5.0 实测：scene.get() 返回 None，导致资产名被静默忽略）。
        scene_name = getattr(context.scene, "bridge_asset_name", "") or ""
        base = scene_name.strip() or _clean_name(objects[0].name)
        if prefs.use_prefix:
            base = bridge_state.naming_rule(io_fbx.detect_kind(objects), base)
        asset_name = _clean_name(base)

        meta = {
            "asset_name": asset_name,
            "kind": io_fbx.detect_kind(objects),
            "format": profile.get("format", "fbx"),
            "to": {
                "app": target,
                "project": prefs.project,
                "content_path": prefs.content_path,
            },
            "unit": {
                "scene_unit": "m",
                "up_axis": "Z",
                "forward_axis": "-Y",
                "unit_scale": profile.get("export", {}).get("global_scale", 1.0),
            },
            "options": {
                "mode": prefs.send_mode,
                "import_materials": prefs.import_materials,
                "import_textures": prefs.import_textures,
                "combine_meshes": prefs.combine_meshes,
                "import_animations": prefs.import_animations,
            },
            "objects": io_fbx.gather_objects(objects),
            # 同上：RNA 属性要用 getattr 取
            "note": getattr(context.scene, "bridge_note", "") or "",
        }

        try:
            client = _client()
            rec = client.create_transfer(meta)
            tid = rec["id"]
            # 服务端可能把中文名归一化成了拼音，以它为准，保证文件名/记录名一致
            asset_name = rec.get("asset_name") or asset_name

            payload_dir = client.payload_dir() if prefs.direct_write else None
            if payload_dir:
                dst = Path(payload_dir) / tid
                dst.mkdir(parents=True, exist_ok=True)
                fbx = io_fbx.export_selection(
                    str(dst / f"{asset_name}.fbx"), profile, objects
                )
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    fbx = io_fbx.export_selection(
                        str(Path(tmp) / f"{asset_name}.fbx"), profile, objects
                    )
                    client.put_file(tid, Path(fbx))

            rec = client.publish(tid)
        except BridgeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"导出失败: {type(exc).__name__}: {exc}")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"已发送 {asset_name} -> {target}（第 {rec['revision']} 版，{Path(fbx).name}）",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 接收
# ---------------------------------------------------------------------------


def poll_and_import(max_items: int = 5) -> int:
    """拉 inbox 并导入。返回值是成功处理的条数。

    只能在主线程（timer）里调用 —— 里面全是 bpy 操作。
    """
    prefs = _prefs()
    client = _client()
    handled = 0
    try:
        records = client.inbox(limit=max_items)
    except BridgeError:
        return 0

    for rec in records:
        try:
            full = client.claim(rec["id"])
        except BridgeError as exc:
            if "409" not in str(exc):
                print(f"[bridge] claim 失败: {exc}")
            continue

        try:
            files = full.get("_files_abs") or []
            fbx_files = [f for f in files if f.lower().endswith((".fbx", ".obj"))]
            if not fbx_files:
                raise RuntimeError(f"transfer {full['id']} 里没有可导入的模型文件")

            src_app = (full.get("from") or {}).get("app", "unreal")
            profile = bridge_state.route_profile(src_app, "blender")
            imported = []
            for f in fbx_files:
                imported += io_fbx.import_fbx(f, profile)

            client.finish(
                full["id"],
                "done",
                {
                    "objects": [o.name for o in imported],
                    "message": io_fbx.last_import_report(imported),
                },
            )
            handled += 1
            print(f"[bridge] 收到 {full['asset_name']}（来自 {src_app}，第 {full['revision']} 版）")
        except Exception as exc:
            client.finish(full["id"], "error", {"message": f"{type(exc).__name__}: {exc}"})
            print(f"[bridge] 导入失败: {exc}")

    if handled:
        try:
            bpy.context.window_manager.popup_menu(
                lambda self, ctx: self.layout.label(text=f"Asset Bridge 已导入 {handled} 项资产"),
                title="Asset Bridge",
                icon="IMPORT",
            )
        except Exception:
            pass
    return handled


class BRIDGE_OT_pull_now(bpy.types.Operator):
    bl_idname = "bridge.pull_now"
    bl_label = "立即接收"
    bl_description = "手动从其他软件拉取待处理的资产"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            n = poll_and_import()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if n == 0:
            self.report({"INFO"}, "收件箱为空")
        else:
            self.report({"INFO"}, f"已导入 {n} 项")
        return {"FINISHED"}


class BRIDGE_OT_open_payload_dir(bpy.types.Operator):
    bl_idname = "bridge.open_payload_dir"
    bl_label = "打开负载目录"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            pd = _client().payload_dir()
        except BridgeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not pd:
            self.report({"WARNING"}, "拿不到服务端负载目录")
            return {"CANCELLED"}
        import subprocess

        subprocess.Popen(f'explorer "{pd}"')
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 定时器
# ---------------------------------------------------------------------------


def tick():
    """bpy.app.timers 回调（主线程）：只读缓存 + 消费导入队列，绝不联网。

    真正的网络 IO 全在 _poll_loop 后台线程。这里只在主线程执行 bpy 导入，
    所以服务器没开时主线程完全不卡。
    """
    try:
        _sync_config()
    except Exception as exc:
        print(f"[bridge] 同步配置失败: {type(exc).__name__}: {exc}")
    handled = 0
    try:
        while not _pending.empty():
            full = _pending.get_nowait()
            try:
                _import_one(full)
                handled += 1
            except Exception as exc:
                print(f"[bridge] 导入异常: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"[bridge] tick 异常: {type(exc).__name__}: {exc}")
    if handled:
        try:
            bpy.context.window_manager.popup_menu(
                lambda self, ctx: self.layout.label(
                    text=f"Asset Bridge 已导入 {handled} 项资产"),
                title="Asset Bridge", icon="IMPORT")
        except Exception:
            pass
    try:
        return max(0.2, float(_prefs().poll_interval))
    except Exception:
        return 2.0


# ---------------------------------------------------------------------------
# 后台网络线程（与 Maya / SP / UE 同策略：主线程零网络 IO）
# ---------------------------------------------------------------------------

def _sync_config():
    """主线程调用：把偏好同步进 _CFG（后台线程只读，绝不碰 bpy.context 之外的风险）。"""
    prefs = _prefs()
    try:
        version = ".".join(str(x) for x in bpy.app.version)
    except Exception:
        version = ""
    _CFG["server_url"] = prefs.server_url
    _CFG["project"] = prefs.project
    _CFG["auto_receive"] = prefs.auto_receive
    _CFG["poll_interval"] = prefs.poll_interval
    _CFG["version"] = version
    _CFG["running"] = bridge_state.is_running()


def _make_client():
    """后台线程调用：依据 _CFG 构造纯 urllib 客户端（不碰 bpy）。"""
    return bridge_client.BridgeClient(
        base_url=_CFG.get("server_url", "http://127.0.0.1:27631"),
        app="blender",
        version=_CFG.get("version", ""),
        project=_CFG.get("project", ""),
        timeout=1.0,
    )


def _recalc_status():
    """后台线程调用：短超时探测连通性，更新 _STATUS 缓存（draw 只读它）。"""
    try:
        ok = bridge_client.is_server_reachable(
            _CFG.get("server_url", "http://127.0.0.1:27631"), timeout=0.3)
    except Exception:
        ok = False
    _STATUS["ok"] = ok
    _STATUS["ts"] = time.time()


def _drain_results():
    """后台线程：把主线程回传的导入结果通过 HTTP 上报 finish（网络 IO）。"""
    while True:
        try:
            tid, status, result = _result_queue.get_nowait()
        except queue.Empty:
            break
        try:
            _make_client().finish(tid, status, result)
        except Exception as exc:
            print(f"[bridge] finish 上报失败 {tid}: {exc}")
        _result_queue.task_done()


def _on_net_failure(msg):
    """后台线程：指数退避 + 日志限频 + 达到阈值熔断。"""
    _fail["streak"] += 1
    backoff = min(1000 * (2 ** (_fail["streak"] - 1)), 30000) / 1000.0
    _fail["next"] = time.time() + backoff
    now = time.time()
    if now - _fail["lastlog"] >= 30.0:
        _fail["lastlog"] = now
        print(f"[bridge] 中转服务通信失败({_fail['streak']}): {msg}")
    if _fail["streak"] >= 5 and not _fail["circuit"]:
        _fail["circuit"] = True
        print("[bridge] 中转服务不可达，已暂停轮询。请先启动中转服务。")


def _import_one(full):
    """主线程调用：执行 bpy 导入，把结果放回 _result_queue 交由后台上报。"""
    try:
        files = full.get("_files_abs") or []
        fbx_files = [f for f in files if f.lower().endswith((".fbx", ".obj"))]
        if not fbx_files:
            raise RuntimeError(f"transfer {full['id']} 里没有可导入的模型文件")
        src_app = (full.get("from") or {}).get("app", "unreal")
        profile = bridge_state.route_profile(src_app, "blender")
        imported = []
        for f in fbx_files:
            imported += io_fbx.import_fbx(f, profile)
        _result_queue.put((full["id"], "done", {
            "objects": [o.name for o in imported],
            "message": io_fbx.last_import_report(imported),
        }))
        print(f"[bridge] 收到 {full.get('asset_name')}（来自 {src_app}）")
    except Exception as exc:
        _result_queue.put((full["id"], "error", {"message": f"{type(exc).__name__}: {exc}"}))
        print(f"[bridge] 导入失败: {exc}")


def _poll_loop():
    """后台 daemon 线程主体：所有网络 IO 都在这里。"""
    ev = _stop_event
    while ev is not None and not ev.is_set():
        now = time.time()
        if now < _fail["next"]:
            ev.wait(max(0.0, _fail["next"] - now))
            continue
        # 状态探测（始终做，短超时，不影响 fail_streak）
        try:
            _recalc_status()
        except Exception:
            pass
        # 收件箱轮询（仅运行态）
        if _CFG.get("running"):
            try:
                client = _make_client()
                _poll_hb["n"] += 1
                if _poll_hb["n"] % 10 == 1:
                    client.heartbeat()
                if _CFG.get("auto_receive"):
                    for rec in client.inbox(limit=3):
                        full = client.claim(rec["id"])
                        _pending.put(full)
                _fail["streak"] = 0
                _fail["circuit"] = False
            except Exception as exc:
                _on_net_failure(str(exc))
        # 上报导入结果（后台网络）
        _drain_results()
        # 节流：至少间隔 poll_interval
        if ev is not None and not ev.is_set():
            ev.wait(max(0.2, float(_CFG.get("poll_interval", 2.0))))


def start_poll_thread():
    """主线程调用：启动后台网络线程（幂等）。"""
    global _poll_thread, _stop_event
    if _poll_thread is not None and _poll_thread.is_alive():
        return
    _stop_event = threading.Event()
    _poll_thread = threading.Thread(
        target=_poll_loop, name="asset-bridge-poll", daemon=True)
    _poll_thread.start()
    print("[bridge] 后台网络线程已启动")


def stop_poll_thread():
    """主线程调用：停止后台网络线程。"""
    global _poll_thread, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _poll_thread is not None and _poll_thread.is_alive():
        _poll_thread.join(timeout=2.0)
    _poll_thread = None
    _stop_event = None
    print("[bridge] 后台网络线程已停止")


class BRIDGE_OT_start(bpy.types.Operator):
    bl_idname = "bridge.start"
    bl_label = "启动桥接"
    bl_description = "开始轮询中转服务并自动接收资产"
    bl_options = {"REGISTER"}

    def execute(self, context):
        bridge_state.set_running(True)
        self.report({"INFO"}, "桥接已启动")
        return {"FINISHED"}


class BRIDGE_OT_pause(bpy.types.Operator):
    bl_idname = "bridge.pause"
    bl_label = "暂停桥接"
    bl_description = "暂停轮询（不再自动接收资产，随时可再启动）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        bridge_state.set_running(False)
        self.report({"INFO"}, "桥接已暂停")
        return {"FINISHED"}


CLASSES = [
    BRIDGE_OT_test_connection,
    BRIDGE_OT_send_selection,
    BRIDGE_OT_pull_now,
    BRIDGE_OT_open_payload_dir,
    BRIDGE_OT_start,
    BRIDGE_OT_pause,
]
