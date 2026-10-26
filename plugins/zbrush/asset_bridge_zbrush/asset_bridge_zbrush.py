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
    导入/导出/回传，每步带超时与异常捕获（不再用 has_next_filename 预检——该值
    常态 True，预检会误杀全部导入，正是"ZBrush 收发全挂"的根因），
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
BTN_SEND_BLENDER = MENU + ":SendToBlender"
BTN_SEND_MAYA = MENU + ":SendToMaya"
BTN_SEND_UE = MENU + ":SendToUnreal"
BTN_SEND_SP = MENU + ":SendToSP"
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
    "import_wait_sec": 15.0,    # 单个文件导入完成判定的硬上限（超时跳过，不挂起）
    # 2026-10-20 修正（要求 C/D）：
    #   import_max_per_click：单次「接收」最多导入的任务数，超出的留在 inbox 下次再收
    #     （默认 3，可配置）。杜绝数十历史任务 × 长等待把主线程拖死。
    #   import_verify_sec：子工具数量变化判定的窗口（默认 5s）。press 同步返回后通常
    #     立即可见，窗口只是安全网；超时按当前数量下结论，绝不无限挂起。
    "import_max_per_click": 3,
    "import_verify_sec": 5.0,
    "upload_timeout": 300.0,
    "auto_max_sec": 1800.0,     # 自动导入模式总时长上限（看门狗）
    # 2026-10-17 导出几何合规（ZBrush -> UE 崩溃 / 模型极小修复）：
    #   zbrush_export_scale: 顶点固定倍率（默认 1.0，不改动原生尺寸）
    #   zbrush_export_normalize_cm: 当模型最大边长落在不可信区间（<1cm 或 >1e6cm，
    #     即真机「几毫米大」或异常巨大）时，归一化到该 cm 值（默认 100）。
    #     设为 0 可关闭归一化，仅用固定倍率。
    "zbrush_export_scale": 1.0,
    "zbrush_export_normalize_cm": 100.0,
    # 2026-10-17 修正（用户真机反馈 ZBrush->UE 需手动 Roll=90 才正向）：
    # 导出时对顶点做坐标轴旋转，把 ZBrush 坐标系对齐到 UE（Z-up）。
    # 默认绕 X 轴 +90° —— 即把 ZBrush 导出常见的 Y-up 转成 UE 的 Z-up
    # （(x,y,z)->(x,-z,y)，让模型「上」轴从 Y 落到 Z）。若真机实测是反向/躺错边，
    # 把 x 改 -90（或一个轴调正负）即可，纯几何、单测可验证。
    # 单位：度。三个轴可独立叠加（先 Rx 再 Ry 再 Rz）。设为全 0 关闭旋转。
    "zbrush_export_rotation": {"x": 90.0, "y": 0.0, "z": 0.0},
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
    "auto_thread": None,    # P0-2：后台自动循环线程对象（幂等保护用；None=未起）
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
    # 2026-10-26：改用 utf-8-sig 自动剥离 BOM。真机铁证：PowerShell
    # Set-Content -Encoding UTF8 写出的 json 带 EF BB BF（BOM），用纯 utf-8
    # 读会把 BOM 当首字符 -> JSONDecodeError -> scan_inbox 静默跳过 -> 用户手动
    # 测试 n=0 的真凶。utf-8-sig 在无 BOM 时与 utf-8 行为完全一致，安全。
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
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
    """列出待处理任务（*.json，排除 *.done.json / *.tmp）。按修改时间排序。

    返回 (tasks, skipped)：
      - tasks:   [(path, data), ...] 成功解析且含 transfer_id 的任务
      - skipped: [(name, reason, snippet), ...] 被跳过文件，snippet 为前 50 字符
        供调用方在 scan end 日志打印，杜绝「静默 n=0 盲人摸象」。
        2026-10-26 真机：PowerShell Set-Content -Encoding UTF8 写带 BOM 的 json 曾被
        静默跳过 = 用户手动测试 n=0 的真凶；BOM 现由 _read_text 的 utf-8-sig 剥离，
        此处仍对「解析失败 / 缺 transfer_id」打 WARN，便于以后排查任何跳过原因。
    """
    out = []
    if not inbox_dir or not os.path.isdir(inbox_dir):
        return [], []
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
    skipped = []   # [(name, reason, snippet_50)]
    for _, p in out:
        name = os.path.basename(p)
        try:
            raw = _read_text(p)
        except Exception as exc:
            skipped.append((name, "读取失败:{0}".format(type(exc).__name__), ""))
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            snippet = raw[:50] if isinstance(raw, str) else ""
            skipped.append((name, "JSON解析失败:{0}".format(type(exc).__name__), snippet))
            continue
        if isinstance(data, dict) and data.get("transfer_id"):
            tasks.append((p, data))
        else:
            snippet = raw[:50] if isinstance(raw, str) else ""
            skipped.append((name, "无transfer_id或非dict", snippet))
    return tasks, skipped


def _resolve_inbox_path(cfg):
    """解析真正用于扫描的 inbox 目录，规避「插件在扫一个空/错目录」的盲人摸象 bug。

    2026-10-20 真机铁证：ab_config.json 的 payload_dir/inbox_dir 被写成单数
    runtime\\payload（服务端 tray_config.json 与 daemon 约定是复数 runtime\\payloads），
    导致 scan_inbox 静默扫一个不存在的目录、永远 n=0，且心跳/状态也写错目录使
    daemon alive=false。本函数按以下优先级解析真实目录并自愈：
      ① ab_config.json 显式 inbox_dir；
      ② payload_dir/zbrush_inbox；
      ③ 上述各路径的 payload<->payloads 单复数互换变体。
    返回第一个存在的目录；若都不存在，返回配置的 inbox_dir（让上层用原路径报
    「目录不存在」错误，便于定位）。返回 (path, note)，note 非空表示发生了自愈替换。
    """
    inbox = cfg.get("inbox_dir") or ""
    pd = cfg.get("payload_dir") or ""
    cands = []
    if inbox:
        cands.append(inbox)
    if pd:
        cands.append(os.path.join(pd, "zbrush_inbox"))
    # 单复数互换变体（payload <-> payloads）
    for c in list(cands):
        if "payloads" in c:
            cands.append(c.replace("payloads", "payload"))
        elif "payload" in c:
            cands.append(c.replace("payload", "payloads", 1))
    for c in cands:
        if c and os.path.isdir(c):
            note = "" if c == inbox else "自愈：配置路径不存在，改用 {0}".format(c)
            return c, note
    return inbox, ""


def write_receipt(inbox_dir, transfer_id, status, imported=None, error="",
                  detail=None):
    """写 <id>.done.json 回执到 done/ 子目录（守护进程读完后会删）。

    2026-10-18 终局修复：旧版把回执写在 inbox 顶层，守护挂掉时会在顶层堆积
    *.done.json，ZBrush 再次扫描时被历史垃圾拖慢/卡死。现统一写进 done/，
    inbox 顶层只放「待处理任务」，扫描恒干净。
    """
    d = done_dir(inbox_dir)
    target = os.path.join(d, transfer_id + ".done.json") if d else \
        os.path.join(inbox_dir, transfer_id + ".done.json")
    receipt = {
        "transfer_id": transfer_id,
        "status": status,                 # ok / error
        "imported": list(imported or []),
        "error": error,
        "detail": detail or {},
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, target)
    return target


def done_dir(inbox_dir):
    """inbox 下的 done/ 子目录：被消费的任务文件与回执都归集到这里，
    绝对不留在 inbox 顶层（否则 ZBrush 扫描会反复扫到历史垃圾而卡死）。"""
    d = os.path.join(inbox_dir, "done") if inbox_dir else ""
    try:
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _sweep_legacy_receipts(inbox_dir, cfg=None):
    """把 inbox 顶层遗留的 *.done.json（旧版守护写的）移入 done/，避免堆积。"""
    if not inbox_dir or not os.path.isdir(inbox_dir):
        return 0
    d = done_dir(inbox_dir)
    n = 0
    try:
        for name in os.listdir(inbox_dir):
            if name.endswith(".done.json"):
                try:
                    os.replace(os.path.join(inbox_dir, name),
                               os.path.join(d, name))
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    if n and cfg is not None:
        diag_log(cfg, "swept {0} legacy receipt(s) into done/".format(n))
    return n


def consume(path):
    """文件消费后移入 done/ 子目录（保留可追溯，绝不留在 inbox 顶层导致扫描堆积）。

    2026-10-18 终局修复：旧版直接 os.remove 让任务文件消失，但回执仍在顶层堆积。
    现统一把消费过的任务文件也移进 done/，inbox 顶层只保留「待处理」，ZBrush 扫描
    恒为干净短列表，不会被历史垃圾卡死。
    """
    try:
        if path and os.path.isfile(path):
            parent = os.path.dirname(path)
            d = done_dir(parent)
            if d and d != parent:
                os.replace(path, os.path.join(d, os.path.basename(path)))
                return True
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
    # 2026-10-16 心跳铁证修正（用户实机：23:54 重启后心跳 mtime 一动不动）：
    # 本线程逻辑无误（无条件每 interval 写心跳、scan_inbox 空目录安全返回、
    # _touch_heartbeat 自吞异常），但 **ZBrush 内嵌 VM 把 GIL 留在主线程**
    # （见下方 autostart 内 P0-2 实证引文），autostart 返回后本线程拿不到
    # GIL、根本不会被调度——只有主线程正在跑 python（按钮/ auto_loop）时
    # 才能分到时间片。故心跳真实语义 = 「主线程 python 最近是否活跃」。
    # 持续心跳的唯一路径 = 主线程 idle/timer pump（见 TODO 里程碑 1）。
    # 本线程内的 touch 保留作双保险（主线程活跃期间仍能补写）。
    while True:
        try:
            # scan_inbox 现返回 (tasks, skipped) 元组；watcher 只用 tasks，skipped 丢弃。
            tasks, _skipped = scan_inbox(inbox)
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


_DIAG_LOCK = threading.Lock()

def diag_log(cfg, msg):
    """诊断（2026-10-13 夜）：独立时间戳日志，定位「ZBrush 卡死在哪一步」。
    写 <payload_dir 或插件目录>/zb_bridge_diag.log；观察线程与主线程都可调。
    绝不抛异常（诊断日志不能反过来弄崩宿主）。"""
    try:
        base = cfg.get("payload_dir") or cfg.get("inbox_dir") or PLUGIN_DIR
        d = os.path.dirname(base) if base.endswith(".json") else base
        if not d or not os.path.isdir(d):
            d = PLUGIN_DIR
        p = os.path.join(d, "zb_bridge_diag.log")
        line = "{0} [zb] {1}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], msg)
        with _DIAG_LOCK:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def zbrush_version():
    try:
        return str(_zbc().zbrush_info(0))
    except Exception:
        return ""


# 2026-10-16 用户真机纠正：原 _dialog_suspected() 用 has_next_filename() 做「对话框挡路」
# 预检，但该 API 在 ZBrush 里**常态返回 True**（默认就有"下一个文件名"），导致每次
# 导入都被误判为"对话框挡路"而 abort——正是"ZBrush 收发全挂"的根因。已删除该预检，
# 改为直接调导入/导出 API + 异常捕获（见 _import_one / export_current）。


def _safe_subtool_count(zbc):
    """安全读取当前工具的子工具数量（ZBrush Python SDK：zbc.get_subtool_count()）。

    2026-10-20 修正依据：这是已确认的 2026.1 SDK API（Maxon 官方示例
    mod_subtool_export 即用此函数遍历子工具）。用它做「导入前后数量变化」判定，
    取代已证明不可靠的 get_active_tool_path() 字符串匹配。
    """
    try:
        return int(zbc.get_subtool_count())
    except Exception:
        return 0


def _safe_tool_path(zbc):
    """安全读取当前工具路径（仅供「工具是否被替换」的辅助判定，绝不作为成功判据）。"""
    try:
        return str(zbc.get_active_tool_path() or "")
    except Exception:
        return ""


def _import_one(zbc, path, cfg, append=False):
    """主线程导入单个文件。返回 (ok, msg)。绝不无限期挂起。

    导入成功判定（要求 A，彻底弃用 get_active_tool_path 字符串匹配）：
        · 依据：zbc.press("Tool:Import" / "Tool:SubTool:Append") 在 ZBrush 内嵌 VM 中
          是**同步阻塞**的——调用返回即导入操作完成（或失败），无需事后轮询工具路径。
          实测 get_active_tool_path() 导入后返回的是工具所在「目录」（不含文件名），
          永不含文件 stem，作为成功判据必然永不命中（真机铁证：remain 递减却 never
          detected）。故删除整段 get_active_tool_path 轮询。
        · 判定改用「导入前后子工具数量变化」（要求 A 明确点名的依据）：
            - Append：after == before + 1（子工具数恰好 +1）
            - Import：press 同步返回后工具应已承载网格（after >= 1），且工具被替换
              （after != before 或工具路径变化）；after == 0 视为失败。
        · 判定窗口 bounded（import_verify_sec，默认 5s），超时按当前数量下结论，
          绝不无限挂起。

    子工具追加（要求 E，禁止盲猜 press）：
        · 追加只用正确的 ZScript 命令 [IPress, "Tool:SubTool:Append"]（即
          zbc.press("Tool:SubTool:Append")）。禁止在追加失败时盲猜降级到
          Tool:Import——那会生成独立工具、造成子工具互相覆盖（正是「只剩最后 1 个」
          的真凶）。失败则重试一次，仍失败如实报失败（让用户看见，而非静默丢模型）。

    主线程非阻塞（要求 B）：禁止长 sleep 轮询冻死 UI。验证窗口内每次迭代调用
        zbc.update(1, True)（ZBrush 官方「泵」消息、保持 UI 可响应）+ 极短
        sleep(0.1) 防 100% CPU；单次接收另有 import_max_per_click 上限（默认 3），
        杜绝数十任务 × 长等待把主线程拖死。
    """
    diag_log(cfg, "import_one enter path={0} append={1}".format(path, append))
    if not os.path.isfile(path):
        diag_log(cfg, "import_one abort: file missing")
        return False, "文件不存在: {0}".format(path)
    try:
        before = _safe_subtool_count(zbc)
        before_path = _safe_tool_path(zbc)
    except Exception:
        before, before_path = 0, ""
    # 2026-10-16 纠正：不再用 has_next_filename 预检（常态 True，会误杀全部导入）。
    # 改为直接调导入 API + 异常捕获（更安全，不会因常真值 abort 全部导入）。
    _t0 = time.time()
    def _dt():
        return round(time.time() - _t0, 3)
    try:
        diag_log(cfg, "import_one set_next_filename before({0}s)".format(_dt()))
        zbc.set_next_filename(path)
        diag_log(cfg, "import_one set_next_filename after({0}s)".format(_dt()))
        if append:
            # 2026-10-26 真机铁证：Tool:SubTool:Append / Insert 在程序化 [IPress] 场景下
            # 会自己弹文件对话框并忽略 FileNameSetNext（ZBrushCentral / Maxon 论坛多份实测；
            # 用户实机 press 后 0.036s 返回、5s 验证 before=after=1 失败）。故弃用 Append，
            # 改用纯内存、零对话框的可靠套路：
            #   ① Tool:SubTool:Duplicate —— 复制当前子工具（纯内存，绝不弹框）
            #   ② Tool:Import —— 用已验证无对话框的导入，把复制出的子工具几何替换为新文件
            # 结果：子工具数 +1（与 Append 等价），但不依赖会弹框的 Append/Insert。
            try:
                diag_log(cfg, "import_one press(Tool:SubTool:Duplicate) before({0}s)".format(_dt()))
                zbc.press("Tool:SubTool:Duplicate")
                diag_log(cfg, "import_one press(Tool:SubTool:Duplicate) after({0}s)".format(_dt()))
            except Exception as exc:
                diag_log(cfg, "import_one Duplicate EXC({0}s): {1!r}".format(_dt(), exc))
                return False, "子工具复制失败（Duplicate）: {0!r}".format(exc)
            try:
                zbc.set_next_filename(path)
                diag_log(cfg, "import_one press(Tool:Import)[after-Duplicate] before({0}s)".format(_dt()))
                zbc.press("Tool:Import")
                diag_log(cfg, "import_one press(Tool:Import)[after-Duplicate] after({0}s)".format(_dt()))
            except Exception as exc:
                diag_log(cfg, "import_one Import-after-Duplicate EXC({0}s): {1!r}".format(_dt(), exc))
                return False, "追加导入失败（Import after Duplicate）: {0!r}".format(exc)
        else:
            diag_log(cfg, "import_one press(Tool:Import) before({0}s)".format(_dt()))
            zbc.press("Tool:Import")
            diag_log(cfg, "import_one press(Tool:Import) after({0}s)".format(_dt()))
    except Exception as exc:
        diag_log(cfg, "import_one api EXC({0}s): {1!r}".format(_dt(), exc))
        return False, "导入调用失败: {0!r}".format(exc)

    # 导入成功判定：子工具数量变化（要求 A）。press 同步返回后数量通常立即可见，
    # 这里仅作 bounded 安全网，绝不无限挂起。
    verify_sec = float(cfg.get("import_verify_sec", 5.0))
    deadline = time.time() + verify_sec
    ok = False
    after = before
    after_path = before_path
    while time.time() < deadline:
        try:
            after = _safe_subtool_count(zbc)
            after_path = _safe_tool_path(zbc)
        except Exception:
            after, after_path = 0, ""
        if append:
            if after == before + 1:
                ok = True
                break
        else:
            if after >= 1 and (after != before
                               or (before_path and after_path and before_path != after_path)):
                ok = True
                break
        # 让出主线程，保持 ZBrush UI 可响应（非阻塞式轮询）
        try:
            zbc.update(1, True)
        except Exception:
            pass
        time.sleep(0.1)
    if not ok:
        try:
            after = _safe_subtool_count(zbc)
            after_path = _safe_tool_path(zbc)
        except Exception:
            after, after_path = 0, ""
        if append:
            ok = (after == before + 1)
        else:
            ok = (after >= 1 and (after != before
                                  or (before_path and after_path and before_path != after_path)))
    diag_log(cfg, "import_one verify append={0} before={1} after={2} ok={3}".format(
        append, before, after, ok))
    # 验收证据（要求 E/F）：明确的导入结果行（不再无条件 SUCCESS）
    result_txt = "SUCCESS" if ok else "FAIL(未确认子工具数量变化)"
    diag_log(cfg, "import_one result path={0} ok={1} {2}".format(path, ok, result_txt))
    if ok:
        return True, ""
    return False, "已执行导入但未确认子工具数量变化（import_verify_sec 内超时）"


def import_pending(cfg, log=None):
    """主线程：处理当前 inbox 里的全部任务（有界、容错、不卡死）。返回处理数。

    2026-10-18 终局修复（真机：只收第一个、后续 n=0 / 扫描卡死）：
      - 进入先 sweep 顶层遗留 *.done.json 进 done/（旧版守护写顶层的残留）。
      - 任务按修改时间**倒序**（最新优先），用户刚发的排最前，历史垃圾沉底。
      - 单轮**上限**（import_max_per_click，默认 3，要求 C），避免数十个历史垃圾任务
        每个都走 ZBrush 等待 -> 主线程被拖死（"卡死"根因之一）。
      - **缺文件任务直接判错并消费**，绝不调 _import_one（不花 15s 等一个不存在
        的文件），历史垃圾（源文件早被清掉）因此被秒清。
      - 每个任务包 try/except：一个坏任务（异常/导入失败）绝不让整轮循环中断，
        保证队列里**所有合法任务都被处理干净**，不会"处理完第一个就 return/卡死"。
    """
    diag_log(cfg, "import_pending called")   # 按钮反馈：即便 inbox 为空也留痕
    zbc = _zbc()
    _touch_heartbeat(cfg)      # 主线程触点补心跳（2026-10-16，GIL 饿死修正）
    inbox_cfg = cfg.get("inbox_dir") or ""
    if not inbox_cfg:
        diag_log(cfg, "import_pending: inbox_dir 为空（payload_dir 未配置？请重跑安装器）")
        _notebar("未配置 payload_dir / inbox 为空，无法导入（请重跑 python install.py）", False)
        return 0
    # 2026-10-20 防「盲人摸象」：ab_config.json 的 payload_dir/inbox_dir 若与服务端
    # tray_config.json 的 payload_dir 不一致（典型为 payload 单数 vs payloads 复数），
    # scan_inbox 会静默扫一个不存在/空的目录、永远 n=0。这里先自愈（单复数互换 /
    # 退 payload_dir/zbrush_inbox），并用日志把真实扫描路径和错配警示打出来。
    inbox, heal_note = _resolve_inbox_path(cfg)
    if heal_note:
        diag_log(cfg, "import_pending: " + heal_note)
    if not inbox or not os.path.isdir(inbox):
        diag_log(cfg, "import_pending: inbox 目录不存在！inbox={0}".format(inbox or inbox_cfg))
        diag_log(cfg,
                 "  -> 请核对 ab_config.json 的 payload_dir/inbox_dir 是否与服务端 "
                 "tray_config.json 的 payload_dir 一致（常见坑：payload 单数 vs payloads 复数写错，"
                 "导致插件在扫一个空/错目录）")
        _notebar("inbox 目录不存在，无法导入（请检查 ab_config.json 路径是否对齐服务端）",
                 False)
        return 0
    _sweep_legacy_receipts(inbox, cfg)
    diag_log(cfg, "import_pending scan begin inbox={0} heal_note={1}".format(
        inbox, heal_note or ""))
    tasks, skipped = scan_inbox(inbox)
    diag_log(cfg, "import_pending scan end n={0} skipped={1} {2}".format(
        len(tasks), len(skipped),
        ("跳过列表=[" + "; ".join("{0}({1})".format(n, r) for n, r, _ in skipped) + "]")
        if skipped else "跳过列表=[]"))
    for sname, sreason, ssnip in skipped:
        diag_log(cfg, "  [scan-skip] {0}: {1} 前50字符={2!r}".format(sname, sreason, ssnip))
    if not tasks:
        with _STATE["lock"]:
            _STATE["pending"] = 0
        _write_status(cfg)
        return 0
    # 倒序：最新优先（scan_inbox 已按 mtime 升序，反转即最新在前）
    tasks = list(reversed(tasks))
    max_per_click = int(cfg.get("import_max_per_click", 3))   # 要求 C：默认 3，可配置
    done = 0
    total_tasks = len(tasks)
    # 2026-10-19 修复（连发多个只留最后 1 个 / 一对一只有第一次）：跨整个 click 的
    # 所有任务、所有文件维护「是否已导入过至少一个文件」。第一个文件用 append=False
    # （Tool:Import 创建工具），其后所有文件用 append=True（Duplicate+Tool:Import 追加
    # 子工具；Tool:SubTool:Append 程序化会弹文件框已弃用，见 _import_one）。这样「连续
    # 发 3 个点 1 次接收」会生成 3 个追加子工具，而非互相覆盖。
    imported_any = False
    total_imported = 0
    total_errors = 0
    for task_path, task in tasks:
        if done >= max_per_click:
            diag_log(cfg, "import_pending 达到单轮上限 {0}，剩余留待下次点击".format(
                max_per_click))
            break
        tid = str(task.get("transfer_id", ""))
        files = [f for f in (task.get("files") or []) if f]
        frm = str(task.get("from_app", "") or "")
        if frm:
            with _STATE["lock"]:
                _STATE["last_from"] = frm
        if not tid or not files:
            consume(task_path)
            done += 1
            continue
        # 缺文件：直接判错 + 消费（历史垃圾快速清理，绝不进 ZBrush 15s 等待）
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            diag_log(cfg, "import_pending skip-missing tid={0} missing={1}".format(
                tid, [os.path.basename(f) for f in missing]))
            write_receipt(inbox, tid, "error", [],
                          "源文件缺失（可能已被清理）: " +
                          "、".join(os.path.basename(f) for f in missing))
            consume(task_path)
            done += 1
            continue
        # 要求 F：每个任务打 begin / end（带序号 i/N 与耗时），卡住时能定位第几个
        i = done + 1
        task_t0 = time.time()
        imported, errors, details = [], [], {}
        diag_log(cfg, "import {0}/{1}: tid={2} begin files={3} from={4}".format(
            i, total_tasks, tid, [os.path.basename(f) for f in files], frm))
        try:
            for f in files:
                diag_log(cfg, "import_pending import-begin {0}".format(
                    os.path.basename(f)))
                ok, msg = _import_one(zbc, f, cfg, append=imported_any)
                imported_any = True
                diag_log(cfg, "import_pending file={0} ok={1} msg={2}".format(
                    os.path.basename(f), ok, msg[:80]))
                if ok:
                    imported.append(os.path.basename(f))
                    if msg:
                        details[os.path.basename(f)] = msg
                else:
                    errors.append("{0}: {1}".format(os.path.basename(f), msg))
        except Exception as exc:
            diag_log(cfg, "import_pending task EXC tid={0}: {1!r}".format(tid, exc))
            errors.append("处理异常: {0!r}".format(exc))
        # 结果标签：成功 / 超时 / 失败（要求 F）
        if imported:
            result_label = "成功"
        elif any(("import_verify_sec" in e or "超时" in e) for e in errors):
            result_label = "超时"
        else:
            result_label = "失败"
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
        total_imported += len(imported)
        total_errors += len(errors)
        diag_log(cfg, "import {0}/{1}: tid={2} end 耗时={3}ms 结果={4}".format(
            i, total_tasks, tid, int(round((time.time() - task_t0) * 1000)),
            result_label))
    # 2026-10-19 真机排查：单次「接收」到底扫了几条任务 / 处理了多少文件，一眼可见。
    # 若 scanned>0 但 total_imported 偏少，说明某些文件在 _import_one 失败（看上方
    # import_one 的 before/after/dt 时间戳判断是异常还是 Hang）。
    # 要求 C：明确告知本次导入几个、剩几个留 inbox（下次点接收继续）。
    diag_log(cfg, "import_pending 本次导入 {0} 个任务，剩余 {1} 个留在 inbox（下次点接收继续）".format(
        done, total_tasks - done))
    diag_log(cfg, "import_pending 结束: scanned={0} consumed={1} 文件导入={2} 文件失败={3}".format(
        len(tasks), done, total_imported, total_errors))
    with _STATE["lock"]:
        _STATE["pending"] = 0
    _write_status(cfg)
    return done


# 2026-10-18 P1：原函数 triangulate_current() 已删除——其内部的
# `press("Tool:Geometry:Triangulate")` 在真机报 SystemError 并会搞崩 ZBrush 内部
# 状态。三角化现由 _postprocess_obj() 纯几何扇三角化兜底（与 ZBrush 版本无关）。


def _build_rotation_fn(cfg):
    """根据 zbrush_export_rotation（{x,y,z} 度）构造一个顶点旋转函数。

    返回 None 表示无需旋转。旋转顺序：先 Rx，再 Ry，再 Rz（单轴时等价）。
    纯几何、确定性强，可在普通 Python 单测验证。
    """
    import math
    rot = cfg.get("zbrush_export_rotation") if cfg else None
    if not isinstance(rot, dict):
        return None
    rx = float(rot.get("x", 0.0) or 0.0)
    ry = float(rot.get("y", 0.0) or 0.0)
    rz = float(rot.get("z", 0.0) or 0.0)
    if rx == 0.0 and ry == 0.0 and rz == 0.0:
        return None

    def _rot(v):
        x, y, z = v[0], v[1], v[2]
        if rx:
            a = math.radians(rx); c, s = math.cos(a), math.sin(a)
            y, z = y * c - z * s, y * s + z * c
        if ry:
            a = math.radians(ry); c, s = math.cos(a), math.sin(a)
            x, z = x * c + z * s, -x * s + z * c
        if rz:
            a = math.radians(rz); c, s = math.cos(a), math.sin(a)
            x, y = x * c - y * s, x * s + y * c
        return [x, y, z]
    return _rot


def _postprocess_obj(path, cfg):
    """OBJ 导出后处理（纯几何，确定性强、可在普通 Python 单测验证）。

    修复 ZBrush -> UE 导出的四类问题（用户真机反馈）：
      1. **坐标轴旋转（2026-10-17 新增）**：按 zbrush_export_rotation 把顶点从
         ZBrush 坐标系对齐到 UE（默认绕 X +90°，即把 ZBrush 导出的 Y-up 转成
         UE 的 Z-up，消除「导入后需手动 Roll=90 才正向」）。
      2. **三角化**：把 n-gon 扇三角化（fan），消除非三角面导致的导入崩溃。
      3. **UV 索引容错**：若任一 face 引用了 UV，则保证**所有** face 都引用合法
         UV（缺失的 face 补 (0,0) UV），杜绝 `UVs.IsValidIndex` 越界崩溃；
         纯无 UV 的网格（ZBrush 未展开 UV 时）保持原样不动（importer 自行补）。
        4. **缩放对齐 UE cm**：ZBrush OBJ 无量纲、原生单位极小（真机表现为「几毫米
         大」，真实模型导出仅 2 单位）。**统一按 zbrush_export_normalize_cm（默认 100）
         归一化到厘米级**（再叠 zbrush_export_scale 倍率）；仅当显式关闭
         （normalize_cm<=0）时保留原生尺寸。前后 bbox 写入 diag 供真机核对。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        diag_log(cfg, "_postprocess_obj 读失败（跳过）: {0!r}".format(exc))
        return False

    verts, vts, vns = [], [], []          # 数值列表
    faces = []                            # 每 face: list of (v, vt_or_None, vn_or_None)
    headers = []                          # 非几何行（注释/分组/材质）原样保留
    has_uv = False
    has_vn = False

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            headers.append(line.rstrip("\n"))
            continue
        if s.startswith("v "):
            parts = s.split()
            verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif s.startswith("vt "):
            parts = s.split()
            vts.append([float(parts[1]), float(parts[2]) if len(parts) > 2 else 0.0])
        elif s.startswith("vn "):
            parts = s.split()
            vns.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif s.startswith("f "):
            toks = s.split()[1:]
            face = []
            ok = True
            for t in toks:
                # 格式: v / v/vt / v/vt/vn / v//vn
                segs = t.split("/")
                try:
                    vi = int(segs[0])
                except ValueError:
                    ok = False
                    break
                vti = int(segs[1]) if len(segs) > 1 and segs[1] != "" else None
                vni = int(segs[2]) if len(segs) > 2 and segs[2] != "" else None
                face.append((vi, vti, vni))
                if vti is not None:
                    has_uv = True
                if vni is not None:
                    has_vn = True
            if ok and face:
                faces.append(face)
            else:
                headers.append(line.rstrip("\n"))   # 解析失败的面原样保留
        else:
            headers.append(line.rstrip("\n"))

    if not verts or not faces:
        diag_log(cfg, "_postprocess_obj: 无几何，跳过（{0}）".format(path))
        return False

    # --- 坐标轴旋转（ZBrush -> UE 坐标系对齐，默认 Rx+90°）---
    rot_fn = _build_rotation_fn(cfg)
    if rot_fn is not None:
        for v in verts:
            v[0], v[1], v[2] = rot_fn(v)

    # --- 缩放：先算原生 bbox ---
    lo = [min(v[i] for v in verts) for i in range(3)]
    hi = [max(v[i] for v in verts) for i in range(3)]
    size_before = [hi[i] - lo[i] for i in range(3)]
    max_ext = max(size_before) if max(size_before) > 0 else 0.0

    base_scale = float(cfg.get("zbrush_export_scale", 1.0) or 1.0)
    normalize_cm = float(cfg.get("zbrush_export_normalize_cm", 100.0) or 0.0)
    # ZBrush OBJ 无量纲：统一归一化到 normalize_cm（默认 100cm）。仅当显式关闭
    # （normalize_cm<=0）时保留原生尺寸。这样无论导出是 2 单位还是 50 单位，
    # 进 UE 都是一致厘米级（用户真机：真实模型导出 2 单位，旧阈值 <1.0 漏判 -> 极小）。
    scale = base_scale
    if normalize_cm > 0 and max_ext > 0:
        scale = (normalize_cm / max_ext) * base_scale
    if scale != 1.0:
        for v in verts:
            v[0] *= scale
            v[1] *= scale
            v[2] *= scale
        lo = [lo[i] * scale for i in range(3)]
        hi = [hi[i] * scale for i in range(3)]
    size_after = [hi[i] - lo[i] for i in range(3)]

    # --- UV 容错：统一补齐缺失 UV ---
    uv_added = 0
    if has_uv:
        for face in faces:
            for j, (vi, vti, vni) in enumerate(face):
                if vti is None:
                    vts.append([0.0, 0.0])
                    vti = len(vts)
                    face[j] = (vi, vti, vni)
                    uv_added += 1
    if has_vn:
        for face in faces:
            for j, (vi, vti, vni) in enumerate(face):
                if vni is None:
                    vns.append([0.0, 0.0, 1.0])
                    vni = len(vns)
                    face[j] = (vi, vti, vni)

    # --- 扇三角化（fan）所有 face ---
    tri_faces = []
    for face in faces:
        n = len(face)
        if n < 3:
            continue
        if n == 3:
            tri_faces.append(face)
        else:
            for k in range(1, n - 1):
                tri_faces.append([face[0], face[k], face[k + 1]])

    # --- 写回（规范顺序：header -> v -> vt -> vn -> f）---
    out = []
    out.extend(headers)
    out.append("# AssetBridge post-process: triangulated={0} uv_added={1} "
               "scale={2:.6g}".format(len(tri_faces), uv_added, scale))
    for v in verts:
        out.append("v {0} {1} {2}".format(v[0], v[1], v[2]))
    for vt in vts:
        out.append("vt {0} {1}".format(vt[0], vt[1]))
    for vn in vns:
        out.append("vn {0} {1} {2}".format(vn[0], vn[1], vn[2]))
    for f in tri_faces:
        toks = []
        for (vi, vti, vni) in f:
            if vti is not None and vni is not None:
                toks.append("{0}/{1}/{2}".format(vi, vti, vni))
            elif vti is not None:
                toks.append("{0}/{1}".format(vi, vti))
            elif vni is not None:
                toks.append("{0}//{1}".format(vi, vni))
            else:
                toks.append("{0}".format(vi))
        out.append("f " + " ".join(toks))

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
    except Exception as exc:
        diag_log(cfg, "_postprocess_obj 写失败（跳过）: {0!r}".format(exc))
        return False

    diag_log(cfg, "_postprocess_obj done: tris={0} uv_added={1} scale={2:.6g} "
             "rot={3} bbox_before=({4}) bbox_after=({5})".format(
                 len(tri_faces), uv_added, scale,
                 cfg.get("zbrush_export_rotation") if cfg else None,
                 ", ".join("{0:.4g}".format(x) for x in size_before),
                 ", ".join("{0:.4g}".format(x) for x in size_after)))
    return True


def export_current(path, cfg=None):
    """主线程导出当前工具到 path（免对话框，有界等待）。

    2026-10-17 修正（用户真机反馈 ZBrush->UE 崩溃 / 模型极小）：
      - 导出后 _postprocess_obj 兜底：扇三角化（纯几何，不依赖 ZBrush 原生
        Triangulate API，避免 SystemError 搞崩 ZBrush）+ UV 索引容错 + 缩放对齐 UE cm。
    """
    zbc = _zbc()
    # 2026-10-16 纠正：不再用 has_next_filename 预检（常态 True，会误杀导出）。
    # 2026-10-18 P1：删除原生 Tool:Geometry:Triangulate 调用（真机报 SystemError、
    # 会搞崩 ZBrush 内部状态）；三角化改由 _postprocess_obj 纯几何扇三角化兜底。
    try:
        zbc.set_next_filename(path)
        zbc.press("Tool:Export")
    except Exception as exc:
        return False, "导出调用失败: {0!r}".format(exc)
    deadline = time.time() + 120.0
    while time.time() < deadline:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            time.sleep(0.4)          # 稳定期
            _postprocess_obj(path, cfg or load_config())
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
    # 2026-10-17 修正（用户真机反馈）：get_active_tool_path() 在部分 ZBrush 版本 /
    # 导入场景下返回的是**被导入文件的完整路径**（如
    # E:\...\runtime\payloads\<tid>\SM_Cube），而非干净子工具名。旧代码用
    # rsplit("/",1) 只处理 / 分隔符，遇 Windows 反斜杠时整条路径被当名字 ->
    # 回传给 UE 变成一长串路径名（被 sanitize_asset_name 转成下划线串）。
    # 修复：用 os.path.basename（同时处理 / 与 \）+ 去扩展名，拿干净 basename。
    name = os.path.basename(tool)
    name = os.path.splitext(name)[0]
    if not name or name in (".", ".."):
        name = "ZBrushTool"
    staging = os.path.join(cfg.get("payload_dir") or PLUGIN_DIR,
                           "_zbrush_send_" + uuid.uuid4().hex[:8])
    os.makedirs(staging, exist_ok=True)
    obj = os.path.join(staging, name + ".obj")
    try:
        ok, msg = export_current(obj, cfg)
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
            diag_log(cfg, "auto_loop iteration begin")
            # 主线程心跳（2026-10-16）：auto_loop 运行期间主线程在跑 python，
            # 每轮补写心跳，daemon 的 zb_bridge_alive 在此期间为真
            _touch_heartbeat(cfg)
            # 细埋点（2026-10-14 P0-3）：三段各自计时，卡死时最后一条
            # 完整日志 = 卡点（收件箱/回传/pump 三阶段分离）
            t_imp = time.monotonic()
            diag_log(cfg, "auto_loop import_pending begin")
            try:
                import_pending(cfg, log)
            except Exception as exc:
                if log:
                    log("自动导入异常: {0!r}".format(exc))
            diag_log(cfg, "auto_loop import_pending done dt={0:.2f}s".format(
                time.monotonic() - t_imp))
            t_out = time.monotonic()
            diag_log(cfg, "auto_loop outbox begin")
            try:
                process_outbox_once(cfg, log)
            except Exception as exc:
                if log:
                    log("自动导入异常: {0!r}".format(exc))
            diag_log(cfg, "auto_loop outbox done dt={0:.2f}s".format(
                time.monotonic() - t_out))
            t_pump = time.monotonic()
            diag_log(cfg, "auto_loop api zbc.update(1,True) before")
            try:
                zbc.update(1, True)
            except Exception:
                pass
            diag_log(cfg, "auto_loop api zbc.update(1,True) after dt={0:.2f}s".format(
                time.monotonic() - t_pump))
            dt = time.monotonic() - it
            diag_log(cfg, "auto_loop iteration done dt={0:.2f}s".format(dt))
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
    zbc.add_button(BTN_SEND, "发送当前模型 -> 默认目标（ab_config.json 的 default_to）",
                   lambda item: on_send_current())
    # 每目标一按钮（ZBrush API 只有 add_button 没有下拉；2026-10-14 定案）。
    # Maya/Blender/UE 收 OBJ 的导入分支已齐（同日 P0-4），四端双向闭环。
    zbc.add_button(BTN_SEND_BLENDER, "发送当前模型 -> Blender",
                   lambda item: on_send_current(to_app="blender"))
    zbc.add_button(BTN_SEND_MAYA, "发送当前模型 -> Maya",
                   lambda item: on_send_current(to_app="maya"))
    zbc.add_button(BTN_SEND_UE, "发送当前模型 -> Unreal Engine",
                   lambda item: on_send_current(to_app="unreal"))
    zbc.add_button(BTN_SEND_SP, "发送当前模型 -> Substance Painter",
                   lambda item: on_send_current(to_app="substance_painter"))
    zbc.add_button(BTN_REPLY, "发送当前模型 -> 最近一次发送者（兜底）",
                   lambda item: on_send_back())
    zbc.add_button(BTN_OUTBOX, "立即回传热文件夹",
                   lambda item: on_flush_outbox())
    zbc.add_button(BTN_AUTO, "自动导入：开关（实验，主线程循环，可能卡顿，默认关）",
                   lambda item: toggle_auto())
    return True


def on_send_current(cfg=None, log=None, to_app=None):
    """发送当前模型。to_app 为空时用 default_to；按钮回调带明确目标。"""
    cfg = cfg or load_config()
    _touch_heartbeat(cfg)      # 主线程触点补心跳（2026-10-16，GIL 饿死修正）
    ok, msg = send_current(cfg, to_app or cfg.get("default_to"), log)
    _notebar(msg, ok)
    return msg


def on_send_back(cfg=None, log=None):
    cfg = cfg or load_config()
    _touch_heartbeat(cfg)      # 主线程触点补心跳（2026-10-16，GIL 饿死修正）
    with _STATE["lock"]:
        to_app = _STATE["last_from"] or cfg.get("default_to", "blender")
    ok, msg = send_current(cfg, to_app, log)
    _notebar(msg, ok)
    return msg


def on_flush_outbox(cfg=None, log=None):
    cfg = cfg or load_config()
    _touch_heartbeat(cfg)      # 主线程触点补心跳（2026-10-16，GIL 饿死修正）
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

    **零阻塞**：只建 UI + 起观察线程（纯 IO，绝不碰 zbc），立即返回。
    所有 ZBrush 操作（zbc.* 调用）都只在**主线程**执行——ZBrush 的嵌入 Python
    把 GIL 锁在主线程，后台线程调用 zbc 会饿死/无效（Maxon ZBrush 2026 Python
    SDK 明确：Main thread only）。因此：
      - 自动收发循环 auto_loop 只能由用户点「自动导入」按钮（toggle_auto）在
        主线程拉起（有界、带看门狗、可随时停）；
      - 后台线程只做纯 IO 的观察（inbox 轮询 + 心跳 + 状态），提供 daemon 侧
        的 alive 判定，不碰任何 zbc。
    2026-10-15 P0-2 复核：早前「后台 daemon 线程自动起 auto_loop」方案因在
    非主线程调 zbc 而无效（且可能搞崩宿主），已移除。需要「启动即自动收发」
    的连续行为，正确载体是 ZBrush 主线程的 ZScript 计时器（按下 AUTO 按钮），
    属后续增强，需真机验证。
    """
    try:
        cfg = load_config(cfg_path)
        diag_log(cfg, "autostart enter")
        diag_log(cfg, "autostart install_ui before")
        install_ui()
        diag_log(cfg, "autostart install_ui done")
        diag_log(cfg, "autostart start_watcher before")
        start_watcher(cfg)
        diag_log(cfg, "autostart start_watcher done")
        # 注意：不再后台自动起 auto_loop（非主线程调 zbc 无效）。auto_loop 只在
        # 用户点「自动导入」按钮时于主线程运行（见 toggle_auto）。观察线程（纯
        # IO）已在 start_watcher 内拉起，负责心跳与 alive 判定。
        # TODO(2026-10-15, issue: ZBrush auto_loop 启动即自动收发): 当前 auto_loop
        #   仅由用户点「自动导入」按钮（主线程）触发。将来要「启动即自动收发」，
        #   正确载体必须是 **ZBrush 主线程的 idle/timer 调度**（例如 ZScript 计时器
        #   或 zbc 主线程 pump 循环），**绝不能是后台线程调 zbc**——ZBrush 内嵌 VM
        #   把 GIL 锁在主线程，后台线程调 zbc.* 会饿死/无效（见上 P0-2 实证）。
        #   触发条件（须全部满足才改，2026-10-15 用户定稿的量化标准）：
        #   ① 手动导入/导出在真机**连续 10 次**成功（中途无一次卡死、无一次
        #      需要强退 ZBrush、无一次传输丢失），跨至少 2 个不同 ZTool 验证；
        #   ② 期间 diag.log 无 "auto_loop EXC" / 看门狗超时记录；
        #   ③ 用户在会话里明确批准改自动方案。
        #   之后先评估主线程 idle 轮询对 ZBrush 交互的占用（建议 idle 间隔
        #   ≥ 2s 起步实测），并保留单圈看门狗 + 总时长上限 + 随时可停。
        #   **里程碑 1（2026-10-16，先于自动收发落地，不动 auto_loop）**：
        #   主线程 idle/timer pump 只做一件事——每 2~5s 调 _touch_heartbeat
        #   （纯 IO，不碰 zbc 收发），让 daemon 的 zb_bridge_alive 在 ZBrush
        #   空闲时也为真。背景：纯 IO 观察线程在 autostart 返回后拿不到 GIL
        #   （内嵌 VM 把 GIL 留在主线程，见 _watch_loop 注释与用户实机铁证
        #   2026-10-16：重启后心跳 mtime 一动不动），心跳只反映「主线程
        #   python 最近是否活跃」。alive=false 时 daemon 走 -script 兜底，
        #   功能不受损，但快速路径（inbox 握手）只在主线程活跃期可用。
        #   改前需在本 TODO 更新进展，不要默默开后台线程。
        # 诊断（P0-2 附赠）：打印插件心跳/状态目录，便于用户实机核对
        # 「插件心跳目录」是否等于 daemon 的 runtime 目录——alive=false 的
        # 常见独立根因是 ab_config.json 的 payload_dir 为空（写到 ZStartup
        # 插件目录），与 daemon runtime 错位。仅打印路径，不改逻辑。
        diag_log(cfg, "autostart diag payload_dir={0}".format(cfg.get("payload_dir")))
        diag_log(cfg, "autostart diag heartbeat={0}".format(heartbeat_path(cfg)))
        diag_log(cfg, "autostart diag status={0}".format(status_path(cfg)))
        # 主线程即时心跳（2026-10-16）：autostart 本身就是一次主线程 python
        # 活动，写一次心跳——否则「重启后直到用户点按钮前」心跳 mtime 完全
        # 不动（用户实机铁证），daemon 侧无从区分「刚重启」与「配置错位」。
        _touch_heartbeat(cfg)
        print("AssetBridge 适配器就绪：Zplugin -> AssetBridge（观察线程运行中，"
              "状态见 {0}；点「自动导入」按钮在主线程启动收发循环）".format(status_path(cfg)))
    except Exception as exc:
        try:
            diag_log(cfg if 'cfg' in dir() else {}, "autostart EXC: {0!r}".format(exc))
            print("AssetBridge 适配器启动失败: {0!r}".format(exc))
        except Exception:
            pass
    return True
