# -*- coding: utf-8 -*-
"""AssetBridge —— ZBrush 2026.1+ 适配器（官方 Python SDK）。

架构（重要，2026-10-13 实测 + 社区权威实现 dcc-mcp-zbrush 一致结论）：

    * ZBrush 内嵌 Python VM 把 GIL 留在**主线程**——zbrush.commands 只能在
      主线程调用，后台线程调 zbc 会饿死。因此本适配器没有常驻 worker 线程，
      而是「**主线程 serve 循环**」：迭代之间 zbc.update(1, True) pump UI，
      停止按钮随时能打断（UI 事件在 update 里被处理）。
    * ZBrush 会话内 Python 解释器**常驻单会话**：按钮创建后不会被后续脚本销毁，
      所以「Load Script 一次」即可（自动加载见安装器注入的 ZData\\Python\\init.py）。

功能：
    1. 接收（中转 -> ZBrush）：serve 循环扫描 inbox 目录里的 *.json 任务文件
       （由 tools/editor_daemon.py 守护进程投递），主线程导入 OBJ
       （set_next_filename + Tool:Import，免对话框），写 *.done.json 回执，
       任务文件消费后即删。
    2. 发送（ZBrush -> 中转）：[发送当前模型] 按钮 set_next_filename +
       Tool:Export 免对话框导出到暂存目录，再走 HTTP 建传输 + 上传 + publish。
    3. 热文件夹兜底：outbox 目录里稳定的 OBJ/.ztl 会被 serve 循环自动回传。

配置：同目录 ab_config.json（安装器 / 守护进程写入，缺失时用默认值）：
    server_url / payload_dir / inbox_dir / outbox_dir /
    poll_interval / default_to / import_settle_sec

本模块不依赖 zbrush.commands（懒加载），可在普通 Python 里导入做单测。
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "ab_config.json")
HEARTBEAT_NAME = "zb_bridge.heartbeat"

MENU = "Zplugin:AssetBridge"
BTN_TOGGLE = MENU + ":ToggleBridge"
BTN_SEND = MENU + ":SendCurrentModel"
BTN_REPLY = MENU + ":SendBackToSender"

DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:27631",
    "payload_dir": "",
    "inbox_dir": "",            # 留空 = <payload>/zbrush_inbox
    "outbox_dir": "",           # 留空 = <payload>/zbrush_outbox
    "poll_interval": 2.0,
    "default_to": "blender",    # 发送目标 app id（无回传对象时的默认）
    "import_settle_sec": 2.0,   # 每次导入后等待（ZBrush 需要时间完成导入）
    "upload_timeout": 300.0,
}

# 模型类扩展名（outbox 只收这些）
MODEL_EXTS = {".obj", ".ztl", ".stl", ".ply", ".fbx"}

# 127.0.0.1 必须绕过系统代理（本机代理会劫持回环，表现为 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config(path=None):
    """读 ab_config.json；缺文件/坏文件都退回默认值。"""
    cfg = dict(DEFAULT_CONFIG)
    p = path or CONFIG_FILE
    try:
        if os.path.isfile(p):
            data = json.loads(_read_text(p))
            for k in DEFAULT_CONFIG:
                if k in data and data[k] not in (None, ""):
                    cfg[k] = data[k]
    except Exception:
        pass
    _resolve_dirs(cfg)
    return cfg


def _resolve_dirs(cfg):
    if not cfg.get("payload_dir"):
        return
    if not cfg.get("inbox_dir"):
        cfg["inbox_dir"] = os.path.join(cfg["payload_dir"], "zbrush_inbox")
    if not cfg.get("outbox_dir"):
        cfg["outbox_dir"] = os.path.join(cfg["payload_dir"], "zbrush_outbox")


def heartbeat_path(cfg):
    return os.path.join(cfg.get("payload_dir") or PLUGIN_DIR, HEARTBEAT_NAME)


# ---------------------------------------------------------------------------
# 纯逻辑（单测覆盖）
# ---------------------------------------------------------------------------

def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def parse_obj_bbox(path):
    """解析 OBJ 的包围盒与头部注释（#Auto scale 等）。单位校准/回执校验用。"""
    lo = [1e30] * 3
    hi = [-1e30] * 3
    header = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#") and len(header) < 8:
                header.append(line.strip())
            elif line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                    except ValueError:
                        continue
                    for i, v in enumerate((x, y, z)):
                        lo[i] = min(lo[i], v)
                        hi[i] = max(hi[i], v)
    if lo[0] > hi[0]:
        return {"size": [0.0, 0.0, 0.0], "min": [0.0, 0.0, 0.0],
                "max": [0.0, 0.0, 0.0], "header": header}
    return {"size": [round(hi[i] - lo[i], 6) for i in range(3)],
            "min": [round(v, 6) for v in lo],
            "max": [round(v, 6) for v in hi],
            "header": header}


def scan_inbox(inbox_dir):
    """列出待处理任务（*.json，排除 *.done.json / *.tmp）。按修改时间排序。"""
    out = []
    if not inbox_dir or not os.path.isdir(inbox_dir):
        return out
    for name in os.listdir(inbox_dir):
        if not name.endswith(".json") or name.endswith(".done.json"):
            continue
        p = os.path.join(inbox_dir, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((st.st_mtime, p))
    out.sort()
    tasks = []
    for _, p in out:
        try:
            data = json.loads(_read_text(p))
            if isinstance(data, dict) and data.get("transfer_id"):
                tasks.append((p, data))
        except Exception:
            continue
    return tasks


def write_receipt(inbox_dir, transfer_id, status, imported=None, error=""):
    """写 <id>.done.json 回执（守护进程读完后会删）。"""
    receipt = {
        "transfer_id": transfer_id,
        "status": status,                 # ok / error
        "imported": list(imported or []),
        "error": error,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = os.path.join(inbox_dir, transfer_id + ".done.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def consume(path):
    """文件消费后即删（任务 json / 回执 / outbox 文件）。失败不影响主流程。"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def transfer_body(asset_name, to_app, from_app="zbrush", fmt="obj",
                  options=None, note=""):
    """构造 POST /transfers 的 body（与现有各发送端结构一致）。"""
    return {
        "asset_name": asset_name or "ZBrushExport",
        "kind": "models",
        "format": fmt,
        "to": {"app": to_app, "project": "", "content_path": ""},
        "from": {"app": from_app, "project": ""},
        "options": options or {},
        "note": note or ("ZBrush 发送 -> " + to_app),
    }


def build_send_options(tool_name, source_file, zbrush_version=""):
    """回传 options：合并发送端原 options + 来源标记（发送端据此重载而非新导入）。"""
    return {
        "editor": "zbrush",
        "zbrush_tool": tool_name or "",
        "source_file": source_file or "",
        "zbrush_version": zbrush_version or "",
    }


# ---------------------------------------------------------------------------
# HTTP（标准库，无代理 opener）
# ---------------------------------------------------------------------------

def http_json(method, url, body=None, timeout=15.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def http_put_file(url, path, timeout=300.0):
    with open(path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def publish_transfer(cfg, body, files, log=None):
    """建传输 -> 上传文件 -> publish。返回 (ok, transfer_id, message)。"""
    server = cfg["server_url"].rstrip("/")
    api = server + "/api/v1"
    try:
        created = http_json("POST", api + "/transfers", body)
    except Exception as exc:
        return False, "", "创建传输失败: {0!r}".format(exc)
    tid = (created or {}).get("transfer", {}).get("id", "")
    if not tid:
        return False, "", "创建传输失败：服务端未返回 id"
    for f in files:
        if not os.path.isfile(f):
            return False, tid, "待上传文件不存在: {0}".format(f)
        try:
            http_put_file(api + "/transfers/{0}/files/{1}".format(
                tid, urllib.request.quote(os.path.basename(f))), f,
                timeout=float(cfg.get("upload_timeout", 300.0)))
        except Exception as exc:
            return False, tid, "上传失败 {0}: {1!r}".format(os.path.basename(f), exc)
    try:
        http_json("POST", api + "/transfers/{0}/publish".format(tid), {})
    except Exception as exc:
        return False, tid, "publish 失败: {0!r}".format(exc)
    return True, tid, "已发布"


# ---------------------------------------------------------------------------
# ZBrush 侧（zbrush.commands 懒加载，全部主线程调用）
# ---------------------------------------------------------------------------

_STATE = {"running": False, "stop": False, "last_from": ""}


def _zbc():
    import zbrush.commands as zbc  # noqa: 只在 ZBrush 内可用
    return zbc


def zbrush_version():
    try:
        return str(_zbc().zbrush_info(0))
    except Exception:
        return ""


def import_files(paths, settle=None):
    """主线程导入 OBJ（免对话框）。返回导入的文件名列表。"""
    zbc = _zbc()
    settle = float(settle if settle is not None else 2.0)
    imported = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        zbc.set_next_filename(p)
        zbc.press("Tool:Import")
        time.sleep(settle)
        imported.append(os.path.basename(p))
    return imported


def export_current(path):
    """主线程导出当前工具到 path（免对话框）。等文件落盘后返回 True。"""
    zbc = _zbc()
    zbc.set_next_filename(path)
    zbc.press("Tool:Export")
    deadline = time.time() + 120.0
    while time.time() < deadline:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            time.sleep(0.4)          # 稳定期
            return True
        time.sleep(0.3)
    return False


def current_tool_name():
    try:
        return str(_zbc().get_active_tool_path())
    except Exception:
        return ""


def process_inbox(cfg, log=None):
    """serve 循环的一圈：处理 inbox 任务（导入 + 回执 + 消费）。"""
    inbox = cfg.get("inbox_dir") or ""
    if not inbox or not os.path.isdir(inbox):
        return 0
    done = 0
    for task_path, task in scan_inbox(inbox):
        tid = str(task.get("transfer_id", ""))
        files = [f for f in (task.get("files") or []) if f]
        from_app = str(task.get("from_app", "") or "")
        if from_app:
            _STATE["last_from"] = from_app      # 记住回传对象
        if not tid or not files:
            consume(task_path)
            continue
        try:
            imported = import_files(files, cfg.get("import_settle_sec"))
            status = "ok" if imported else "error"
            error = "" if imported else "没有文件被导入（文件不存在?）"
            write_receipt(inbox, tid, status, imported, error)
            if log:
                log("导入 {0} -> {1}".format(
                    "、".join(os.path.basename(f) for f in files),
                    "、".join(imported) or "失败"))
        except Exception as exc:
            write_receipt(inbox, tid, "error", [], repr(exc))
            if log:
                log("导入异常 {0}: {1!r}".format(tid, exc))
        consume(task_path)
        done += 1
    return done


def scan_outbox(outbox_dir):
    """稳定的待回传文件（mtime 年龄 > 2s 且大小不再变化）。"""
    if not outbox_dir or not os.path.isdir(outbox_dir):
        return []
    now = time.time()
    out = []
    for name in os.listdir(outbox_dir):
        ext = os.path.splitext(name)[1].lower()
        if ext not in MODEL_EXTS:
            continue
        p = os.path.join(outbox_dir, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if now - st.st_mtime < 2.0:
            continue
        out.append(p)
    return out


def process_outbox(cfg, log=None):
    """serve 循环的一圈：热文件夹兜底回传。"""
    outbox = cfg.get("outbox_dir") or ""
    if not outbox:
        return 0
    done = 0
    for f in scan_outbox(outbox):
        body = transfer_body(
            os.path.splitext(os.path.basename(f))[0],
            cfg.get("default_to", "blender"))
        ok, tid, msg = publish_transfer(cfg, body, [f], log)
        if ok:
            consume(f)
            done += 1
            if log:
                log("热文件夹回传 {0} -> {1}（{2}）".format(
                    os.path.basename(f), cfg.get("default_to"), tid))
        elif log:
            log("热文件夹回传失败 {0}: {1}".format(os.path.basename(f), msg))
    return done


def send_current(cfg, to_app=None, log=None):
    """[发送当前模型]：导出当前工具 -> HTTP 上传 -> publish。"""
    zbc = _zbc()
    to_app = to_app or cfg.get("default_to", "blender")
    tool = current_tool_name() or "ZBrushTool"
    name = tool.rsplit("/", 1)[-1] if "/" in tool else tool
    staging = os.path.join(cfg.get("payload_dir") or PLUGIN_DIR,
                           "_zbrush_send_" + uuid.uuid4().hex[:8])
    os.makedirs(staging, exist_ok=True)
    obj = os.path.join(staging, name + ".obj")
    try:
        if not export_current(obj):
            return False, "导出失败（未拿到文件）"
        bbox = parse_obj_bbox(obj)
        options = build_send_options(tool, name + ".obj", zbrush_version())
        body = transfer_body(name, to_app, options=options)
        ok, tid, msg = publish_transfer(cfg, body, [obj], log)
        if not ok:
            return False, msg
        if log:
            log("已发送 {0} -> {1}（{2}，bbox={3}）".format(
                name, to_app, tid, bbox["size"]))
        return True, "已发送 {0} -> {1}（{2}）".format(name, to_app, tid)
    except Exception as exc:
        return False, "发送失败: {0!r}".format(exc)
    finally:
        try:
            for n in os.listdir(staging):
                os.remove(os.path.join(staging, n))
            os.rmdir(staging)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# serve 循环（主线程）
# ---------------------------------------------------------------------------

def _touch_heartbeat(cfg):
    try:
        p = heartbeat_path(cfg)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        os.replace(tmp, p)
    except Exception:
        pass


def serve_loop(cfg, log=None):
    """主线程循环：迭代间 pump UI，停止按钮在 update 里得到处理机会。"""
    zbc = _zbc()
    _STATE["running"] = True
    _STATE["stop"] = False
    try:
        zbc.set_notebar_text("AssetBridge 桥接运行中（AssetBridge:ToggleBridge 停止）", 0.0)
        while not _STATE["stop"]:
            try:
                process_inbox(cfg, log)
                process_outbox(cfg, log)
            except Exception as exc:
                if log:
                    log("serve 循环异常: {0!r}".format(exc))
            _touch_heartbeat(cfg)
            zbc.update(1, True)
            time.sleep(float(cfg.get("poll_interval", 2.0)))
    finally:
        _STATE["running"] = False
        try:
            zbc.set_notebar_text("", 0.0)
        except Exception:
            pass


def toggle_bridge(cfg=None, log=None):
    """[启动/停止桥接] 按钮回调。"""
    cfg = cfg or load_config()
    if _STATE["running"]:
        _STATE["stop"] = True
        return "停止中…"
    serve_loop(cfg, log)          # 主线程阻塞，直到停止按钮置位
    return "已停止"


def on_send_current(cfg=None, log=None):
    cfg = cfg or load_config()
    zbc = _zbc()
    ok, msg = send_current(cfg, cfg.get("default_to"), log)
    try:
        zbc.set_notebar_text(msg, 1.0 if ok else 0.0)
    except Exception:
        pass
    return msg


def on_send_back(cfg=None, log=None):
    cfg = cfg or load_config()
    zbc = _zbc()
    to_app = _STATE.get("last_from") or cfg.get("default_to", "blender")
    ok, msg = send_current(cfg, to_app, log)
    try:
        zbc.set_notebar_text(msg, 1.0 if ok else 0.0)
    except Exception:
        pass
    return msg


# ---------------------------------------------------------------------------
# UI / 入口
# ---------------------------------------------------------------------------

def install_ui(log=None):
    """建 AssetBridge 子面板 + 三个按钮（常驻解释器，只装一次）。"""
    zbc = _zbc()
    zbc.add_subpalette(MENU, 0)
    zbc.add_button(BTN_TOGGLE, "启动/停止 AssetBridge 桥接（主线程循环，导入/回传）",
                   lambda item: toggle_bridge())
    zbc.add_button(BTN_SEND, "发送当前模型 -> " + "默认目标",
                   lambda item: on_send_current())
    zbc.add_button(BTN_REPLY, "发送当前模型 -> 最近一次发送者",
                   lambda item: on_send_back())
    return True


def autostart(cfg_path=None):
    """由安装器注入的 ZData\\Python\\init.py 调用：装 UI + 自动起 serve 循环。

    失败不拖垮 ZBrush 启动（init.py 里已有 try 兜底，这里再兜一层）。
    """
    try:
        cfg = load_config(cfg_path)
        install_ui()
        if cfg.get("inbox_dir") or cfg.get("payload_dir"):
            # 自动启动 serve 循环（主线程）。用户随时点 停止桥接 打断。
            toggle_bridge(cfg)
    except Exception as exc:
        try:
            print("AssetBridge 适配器启动失败: {0!r}".format(exc))
        except Exception:
            pass
    return True
