# -*- coding: utf-8 -*-
"""AssetBridge —— ZBrush 2026.1+ 适配器（官方 Python SDK）。

线程模型（2026-10-13 重构，血的教训版）
------------------------------------
ZBrush 内嵌 Python VM 把 GIL 留在**主线程**：`zbrush.commands` 只能在主线程调用，
后台线程调 zbc 会饿死；反过来，**主线程里任何长循环/同步等待都会直接把 ZBrush 冻死**
（旧版 autostart 一启动就进 serve 循环，用户实机验收时 ZBrush 卡死强退）。

因此本适配器：

  * **启动零阻塞**：autostart() 只建按钮 + 起一个「观察线程」后立即返回。
  * **观察线程只做纯文件 IO**（扫 inbox、写心跳/状态文件），**绝不调用 zbc**、
    不在主线程 sleep。它只负责「发现有待收任务」并落状态文件。
  * **所有 ZBrush 操作都在主线程、有界、单次**：按钮回调执行一轮
    导入/导出/回传，每步带超时与对话框探针（has_next_filename），
    单文件导入等待上限默认 15s，绝不无限期挂起。
  * **自动导入是显式可选的实验功能**（默认关）：开启后走主线程 pump 循环，
    但带单圈 60s 看门狗 + 总时长上限 + 随时可停，任何异常/超时自动退出循环。

功能：接收（inbox 任务文件 -> 主线程导入 OBJ，免对话框）、
      发送（按钮导出 + HTTP 建传输/上传/publish）、热文件夹回传。

配置：同目录 ab_config.json（安装器写入）：
    server_url / payload_dir / inbox_dir / outbox_dir /
    poll_interval / default_to / import_settle_sec / import_wait_sec /
    auto_max_sec

本模块不 import zbrush.commands（懒加载），可在普通 Python 里导入做单测。
"""

import json
import os
import threading
import time
import urllib.request
import uuid
from datetime import datetime

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "ab_config.json")
HEARTBEAT_NAME = "zb_bridge.heartbeat"
STATUS_NAME = "bridge_status.json"

MENU = "Zplugin:AssetBridge"
BTN_IMPORT = MENU + ":ImportPending"
BTN_SEND = MENU + ":SendCurrentModel"
BTN_REPLY = MENU + ":SendBackToSender"
BTN_OUTBOX = MENU + ":FlushOutbox"
BTN_AUTO = MENU + ":ToggleAuto"

DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:27631",
    "payload_dir": "",
    "inbox_dir": "",            # 留空 = <payload>/zbrush_inbox
    "outbox_dir": "",           # 留空 = <payload>/zbrush_outbox
    "poll_interval": 2.0,       # 观察线程轮询间隔（后台线程，不碰 zbc）
    "default_to": "blender",
    "import_settle_sec": 1.0,   # 导入 press 后的基础等待
    "import_wait_sec": 15.0,    # 单个文件导入完成判定的上限（超时不挂起，记警告）
    "upload_timeout": 300.0,
    "auto_max_sec": 1800.0,     # 自动导入模式总时长上限（看门狗）
}

MODEL_EXTS = {".obj", ".ztl", ".stl", ".ply", ".fbx"}

# 127.0.0.1 必须绕过系统代理（本机代理会劫持回环，表现为 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 运行期状态（观察线程只写 pending 计数等纯数据，不碰 zbc）
_STATE = {
    "pending": 0,           # 观察线程刷新：待收任务数
    "last_from": "",        # 最近一次任务的发送端（回传目标）
    "watcher": False,       # 观察线程是否已起
    "auto": False,          # 自动导入模式开关（主线程循环用）
    "lock": threading.Lock(),
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config(path=None):
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


def status_path(cfg):
    return os.path.join(cfg.get("payload_dir") or PLUGIN_DIR, STATUS_NAME)


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


def write_receipt(inbox_dir, transfer_id, status, imported=None, error="",
                  detail=None):
    """写 <id>.done.json 回执（守护进程读完后会删）。"""
    receipt = {
        "transfer_id": transfer_id,
        "status": status,                 # ok / error
        "imported": list(imported or []),
        "error": error,
        "detail": detail or {},
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
    return {
        "editor": "zbrush",
        "zbrush_tool": tool_name or "",
        "source_file": source_file or "",
        "zbrush_version": zbrush_version or "",
    }


# ---------------------------------------------------------------------------
# HTTP（标准库，无代理 opener；只在按钮回调/后台用，不占启动路径）
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
# 观察线程（纯 IO，绝不调用 zbrush.commands）
# ---------------------------------------------------------------------------

def _watch_loop(cfg, log=None):
    inbox = cfg.get("inbox_dir") or ""
    interval = max(0.5, float(cfg.get("poll_interval", 2.0)))
    while True:
        try:
            tasks = scan_inbox(inbox)
            with _STATE["lock"]:
                _STATE["pending"] = len(tasks)
                if tasks:
                    frm = str(tasks[0][1].get("from_app", "") or "")
                    if frm:
                        _STATE["last_from"] = frm
            _touch_heartbeat(cfg)
            _write_status(cfg)
        except Exception:
            pass                    # 观察线程永不外抛
        time.sleep(interval)


def start_watcher(cfg, log=None):
    """起观察线程（幂等）。**不调用任何 zbc**，ZBrush 启动路径零阻塞。"""
    with _STATE["lock"]:
        if _STATE["watcher"]:
            return False
        _STATE["watcher"] = True
    t = threading.Thread(target=_watch_loop, args=(cfg, log), daemon=True)
    t.start()
    if log:
        log("观察线程已启动（纯 IO，不碰 zbrush.commands）")
    return True


def _touch_heartbeat(cfg):
    try:
        p = heartbeat_path(cfg)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        os.replace(tmp, p)
    except Exception:
        pass


def _write_status(cfg):
    try:
        with _STATE["lock"]:
            obj = {"pending": _STATE["pending"],
                   "last_from": _STATE["last_from"],
                   "auto": _STATE["auto"],
                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        p = status_path(cfg)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主线程操作（有界、单次、带对话框探针）
# ---------------------------------------------------------------------------

def _zbc():
    import zbrush.commands as zbc  # noqa: 只在 ZBrush 内可用
    return zbc


def zbrush_version():
    try:
        return str(_zbc().zbrush_info(0))
    except Exception:
        return ""


def _dialog_suspected(zbc):
    """press 之前探对话框：有残留 next_filename 预设时，native 对话框很可能已弹出，
    此时再 press 会把主线程挂死。"""
    try:
        if hasattr(zbc, "has_next_filename") and zbc.has_next_filename():
            return True
    except Exception:
        pass
    return False


def _import_one(zbc, path, cfg):
    """主线程导入单个文件。返回 (ok, msg)。绝不无限期挂起。"""
    if not os.path.isfile(path):
        return False, "文件不存在: {0}".format(path)
    if _dialog_suspected(zbc):
        return False, "检测到待处理的文件名预设（疑似原生对话框挡路），跳过以防挂起"
    try:
        zbc.set_next_filename(path)
        zbc.press("Tool:Import")
    except Exception as exc:
        return False, "导入调用失败: {0!r}".format(exc)
    stem = os.path.splitext(os.path.basename(path))[0]
    deadline = time.time() + float(cfg.get("import_wait_sec", 15.0))
    detected = False
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            tool = str(zbc.get_active_tool_path() or "")
        except Exception:
            tool = ""
        if stem and stem in tool:
            detected = True
            break
    if detected:
        return True, ""
    return True, "已执行导入，但未在 {0:.0f}s 内确认工具切换（不挂起，按已导入记）".format(
        float(cfg.get("import_wait_sec", 15.0)))


def import_pending(cfg, log=None):
    """主线程：处理当前 inbox 里的全部任务（有界）。返回处理数。"""
    zbc = _zbc()
    inbox = cfg.get("inbox_dir") or ""
    if not inbox:
        return 0
    done = 0
    for task_path, task in scan_inbox(inbox):
        tid = str(task.get("transfer_id", ""))
        files = [f for f in (task.get("files") or []) if f]
        frm = str(task.get("from_app", "") or "")
        if frm:
            with _STATE["lock"]:
                _STATE["last_from"] = frm
        if not tid or not files:
            consume(task_path)
            continue
        imported, errors, details = [], [], {}
        for f in files:
            ok, msg = _import_one(zbc, f, cfg)
            if ok:
                imported.append(os.path.basename(f))
                if msg:
                    details[os.path.basename(f)] = msg
            else:
                errors.append("{0}: {1}".format(os.path.basename(f), msg))
        status = "ok" if imported else "error"
        error = "；".join(errors)
        write_receipt(inbox, tid, status, imported, error, details)
        if log:
            log("导入 {0} -> {1}{2}".format(
                "、".join(os.path.basename(f) for f in files),
                "、".join(imported) or "失败",
                ("（" + error + "）") if error else ""))
        consume(task_path)
        done += 1
    with _STATE["lock"]:
        _STATE["pending"] = 0
    _write_status(cfg)
    return done


def export_current(path):
    """主线程导出当前工具到 path（免对话框，有界等待）。"""
    zbc = _zbc()
    if _dialog_suspected(zbc):
        return False, "检测到待处理的文件名预设（疑似原生对话框挡路），跳过以防挂起"
    try:
        zbc.set_next_filename(path)
        zbc.press("Tool:Export")
    except Exception as exc:
        return False, "导出调用失败: {0!r}".format(exc)
    deadline = time.time() + 120.0
    while time.time() < deadline:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            time.sleep(0.4)          # 稳定期
            return True, ""
        time.sleep(0.3)
    return False, "导出超时（120s 未拿到文件）"


def current_tool_name():
    try:
        return str(_zbc().get_active_tool_path())
    except Exception:
        return ""


def send_current(cfg, to_app=None, log=None):
    """主线程：导出当前工具 -> HTTP 上传 -> publish（有界）。"""
    to_app = to_app or cfg.get("default_to", "blender")
    tool = current_tool_name() or "ZBrushTool"
    name = tool.rsplit("/", 1)[-1] if "/" in tool else tool
    staging = os.path.join(cfg.get("payload_dir") or PLUGIN_DIR,
                           "_zbrush_send_" + uuid.uuid4().hex[:8])
    os.makedirs(staging, exist_ok=True)
    obj = os.path.join(staging, name + ".obj")
    try:
        ok, msg = export_current(obj)
        if not ok:
            return False, msg
        bbox = parse_obj_bbox(obj)
        options = build_send_options(tool, name + ".obj", zbrush_version())
        body = transfer_body(name, to_app, options=options)
        ok, tid, msg = publish_transfer(cfg, body, [obj], log)
        if not ok:
            return False, msg
        if log:
            log("已发送 {0} -> {1}（{2}，bbox={3}）".format(name, to_app, tid, bbox["size"]))
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


def scan_outbox(outbox_dir):
    """稳定的待回传文件（mtime 年龄 > 2s）。"""
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


def process_outbox_once(cfg, log=None):
    """主线程：热文件夹兜底回传（有界单轮）。"""
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


# ---------------------------------------------------------------------------
# 自动导入模式（显式开启的实验功能：主线程 pump + 看门狗）
# ---------------------------------------------------------------------------

def auto_loop(cfg, log=None):
    """主线程 pump 循环。默认不开；开启后也有三重保险：
    单圈 60s 看门狗 / 总时长 auto_max_sec / 停止按钮随时置位。
    ZBrush 卡顿风险由调用方（用户显式开启）承担，UI 会有明确提示。"""
    zbc = _zbc()
    with _STATE["lock"]:
        _STATE["auto"] = True
    hard_deadline = time.monotonic() + float(cfg.get("auto_max_sec", 1800.0))
    try:
        zbc.set_notebar_text("AssetBridge 自动导入已开启（再点一次关闭；卡顿可随时停）", 0.0)
        while True:
            with _STATE["lock"]:
                if not _STATE["auto"]:
                    break
            if time.monotonic() >= hard_deadline:
                if log:
                    log("自动导入到达总时长上限，自动停止")
                break
            it = time.monotonic()
            try:
                import_pending(cfg, log)
                process_outbox_once(cfg, log)
            except Exception as exc:
                if log:
                    log("自动导入异常: {0!r}".format(exc))
            try:
                zbc.update(1, True)
            except Exception:
                pass
            dt = time.monotonic() - it
            if dt > 60.0:
                if log:
                    log("单圈耗时 {0:.1f}s 超过看门狗阈值，自动导入停止".format(dt))
                break
            time.sleep(max(0.5, float(cfg.get("poll_interval", 3.0))))
    finally:
        with _STATE["lock"]:
            _STATE["auto"] = False
        try:
            zbc.set_notebar_text("", 0.0)
        except Exception:
            pass
        _write_status(cfg)


def toggle_auto(cfg=None, log=None):
    cfg = cfg or load_config()
    with _STATE["lock"]:
        running = _STATE["auto"]
    if running:
        with _STATE["lock"]:
            _STATE["auto"] = False          # 循环下一轮退出
        return "自动导入：正在停止…"
    auto_loop(cfg, log)                 # 主线程阻塞直到停止/超时（实验功能，显式开启）
    return "自动导入：已停止"


# ---------------------------------------------------------------------------
# UI / 入口
# ---------------------------------------------------------------------------

def install_ui(log=None):
    """建 AssetBridge 子面板 + 按钮（常驻解释器，只装一次；纯 UI 注册，瞬时完成）。"""
    zbc = _zbc()
    zbc.add_subpalette(MENU, 0)
    zbc.add_button(BTN_IMPORT, "导入待收模型（inbox 里有任务时点；主线程单轮，有界）",
                   lambda item: import_pending(load_config()))
    zbc.add_button(BTN_SEND, "发送当前模型 -> 默认目标",
                   lambda item: on_send_current())
    zbc.add_button(BTN_REPLY, "发送当前模型 -> 最近一次发送者",
                   lambda item: on_send_back())
    zbc.add_button(BTN_OUTBOX, "立即回传热文件夹",
                   lambda item: on_flush_outbox())
    zbc.add_button(BTN_AUTO, "自动导入：开关（实验，主线程循环，可能卡顿，默认关）",
                   lambda item: toggle_auto())
    return True


def on_send_current(cfg=None, log=None):
    cfg = cfg or load_config()
    ok, msg = send_current(cfg, cfg.get("default_to"), log)
    _notebar(msg, ok)
    return msg


def on_send_back(cfg=None, log=None):
    cfg = cfg or load_config()
    with _STATE["lock"]:
        to_app = _STATE["last_from"] or cfg.get("default_to", "blender")
    ok, msg = send_current(cfg, to_app, log)
    _notebar(msg, ok)
    return msg


def on_flush_outbox(cfg=None, log=None):
    cfg = cfg or load_config()
    n = process_outbox_once(cfg, log)
    _notebar("已回传 {0} 个文件".format(n), True)
    return n


def _notebar(msg, ok):
    try:
        _zbc().set_notebar_text(msg, 1.0 if ok else 0.0)
    except Exception:
        pass


def autostart(cfg_path=None):
    """由安装器注入的 ZData\\Python\\init.py 调用。

    **零阻塞**：只建 UI + 起观察线程（纯 IO），立即返回。
    所有 ZBrush 操作都等用户点按钮（主线程、有界、带探针）。
    """
    try:
        cfg = load_config(cfg_path)
        install_ui()
        start_watcher(cfg)
        print("AssetBridge 适配器就绪：Zplugin -> AssetBridge（观察线程运行中，"
              "状态见 {0}）".format(status_path(cfg)))
    except Exception as exc:
        try:
            print("AssetBridge 适配器启动失败: {0!r}".format(exc))
        except Exception:
            pass
    return True
