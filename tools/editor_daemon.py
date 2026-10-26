# -*- coding: utf-8 -*-
"""Asset Bridge —— Photoshop / SAI2 外部编辑器守护进程。

为什么需要守护进程：
    Photoshop / SAI2 没有可编程的桥接 API（SP 有 JS API、Toolbag 有 mset 插件），
    「发送到 PS → 编辑 → 保存后发送端自动重新加载」只能由外部进程代劳：

      1. 轮询中转服务里 to.app = photoshop / sai2 的贴图传输（kind=textures）
      2. claim 后用对应 exe 打开文件（PS / SAI2 都支持命令行传文件路径）
      3. 监听文件 (mtime,大小) 连续双采样 + 同目录新图片文件（覆盖「另存为」）
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
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
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
# app 标签别名：UE/插件偶发用 "ps" 简写，daemon 内部一律归一成 config 键 "photoshop"，
# 避免「config/DEFAULT_APPS 用 photoshop，策略字典用 ps」的键不匹配导致 PS 任务被静默丢弃。
APP_ALIASES = {"ps": "photoshop"}

def _norm_app(app):
    return APP_ALIASES.get(app, app)

# ZBrush 专用目录（payload 下）
ZB_INBOX = "zbrush_inbox"        # 任务文件目录：守护进程写 <tid>.json，ZBrush 适配器消费
ZB_OUTBOX = "zbrush_outbox"      # 热文件夹：用户手动导出，守护进程自动回传
ZB_DONE = "done"                 # 2026-10-18 终局：被消费的任务文件 + 回执归集到此子目录，
                                 #   绝不留在 inbox 顶层（防 ZBrush 扫描历史垃圾卡死）
ZB_HEARTBEAT = "zb_bridge.heartbeat"   # ZBrush 侧常驻桥接循环写的心跳（判断 worker 存活）
ZB_NOTIFY = "notify.json"        # D3 托盘提示：ZBrush 运行中且 worker 未接管的落盘通知
ZB_IMPORT_TIMEOUT_SEC = 300.0    # 任务投递后等回执的上限
ZB_IMPORT_MAX_RENEWALS = 2        # 2026-10-18 P0：handshake 任务文件滞留时的续期上限，超出即彻底清理（防永久 claimed 卡死）
ZB_POPEN_TIMEOUT_SEC = 90.0      # 一次性 -script 拉起后等回执的上限（已弃用：见下）
ZB_BRIDGE_MAX_AGE = 12.0         # 心跳 freshness 阈值（秒）

# ZBrush 适配器源码位置（一次性 -script 兜底用）。
# 2026-10-16 用户实机纠正：ZBrush 单实例，已运行时 spawn 新实例必然超时/冲突，
# 故 handle_zbrush 已**废弃一次性 -script 拉起路径**，统一走 inbox 文件握手
# （用户点「导入待收模型」消费）。本常量仅作历史保留，不再被主动调用。
ZB_ONE_SHOT = REPO_ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "one_shot_import.py"

# 认识的模型扩展名（outbox 只收这些）
MODEL_EXTS = {".obj", ".ztl", ".stl", ".ply", ".fbx"}

# 认识的图片扩展名（另存为新文件时只收这些；.sai2 等原生格式不收）
IMAGE_EXTS = {
    ".png", ".psd", ".tga", ".jpg", ".jpeg", ".bmp", ".exr", ".tif", ".tiff",
}

POLL_INTERVAL_SEC = 2.0        # 主循环周期（空闲默认；活跃 PS/SAI2 会自适应降到 0.5s）
SETTLE_SEC = 1.5               # 【已废弃，2026-10-16 P1-C】原固定稳定等待；现由 settle 自适应（连续 2 采样稳定 + 尺寸分档兜底）替代
EXIT_GRACE_SEC = 60.0          # 编辑器进程退出后、无保存行为的宽限期
# 已成功回传（returns>0）的 watch 在编辑器退出后保留的「关窗重发」复用宽限期：
# 短于 EXIT_GRACE_SEC，用于让同一文件在刚关窗后短时间内重发能复用窗口（reopen
# 分支），超期则只清 watch、不翻写已 done 的传输。见 Watch.advance / Daemon.handle。
REUSE_GRACE_SEC = 3.0           # 关窗重发复用窗口宽限（> 轮询延迟，< soak 5s 清理上限）
LAUNCH_DELEGATE_SEC = 3.0      # 启动后秒退 = 委托给已运行实例，不能当成用户关闭
WATCH_IDLE_SEC = 600.0         # 无保存行为的最长监视时长（编辑器进程已退后才兜底解 watch）
COPY_RETRY = 3                 # 拷贝保存文件的重试次数（PS 可能短暂占用文件）
COPY_RETRY_WAIT = 0.6
HEARTBEAT_EVERY = 15           # 每多少个循环发一次心跳
LOG_RL_SEC = 30.0              # 同类日志限频间隔

# 歧义（有窗口但标题不含目标文件）时的默认策略，按 app 区分（2026-10-16 P0 修正）。
# 背景：PS/SAI2 单窗口多标签时，顶层标题只显示活动标签，后台标签的画布是**隐藏
# MDI 子窗口**（IsWindowVisible=False），本探针在 _collect 里会跳过隐藏窗口 ->
# 多标签 A→B→A 这类场景必落「歧义」。两种 app 对歧义的处理代价相反：
#   · ps   : "reopen" —— 用户实机验证 PS 对已打开文件再 Popen 只聚焦现有文档、
#            不弹窗不重复打开（「重新发送没反应不弹窗，笔画也在」），歧义重开 benign。
#   · sai2 : "keep"   —— SAI2 对「打开已打开文件」会弹「是否放弃当前编辑重新打开」
#            确认框（第一轮用户抱怨根因）。若歧义默认重开，A→B→A 会重新引入该弹窗。
#            故 SAI2 歧义默认 keep（只去重不重开、不弹窗）。窗口确实被关再发的极小
#            概率静默丢失风险用户已知情接受（见 release notes 第二节）。
# editor_daemon.json 的 "reuse_ambiguous": {"<app>": "reopen"|"keep"} 可针对单个
# app 覆盖此处默认——但默认值已经正确，用户不必手动配。
AMBIGUOUS_DEFAULT = {"photoshop": "reopen", "sai2": "keep"}

# 复用分支「窗口已开着」时，是否把窗口带到前台（2026-10-16 实机 P0 修正）：
# - ps  ：保持聚焦（用户实测 PS 多标签正常，已打开文件再聚焦只聚焦不重复不弹窗）。
# - sai2：noop（SAI2 把「外部进程 SetForegroundWindow 聚焦到已打开文件」当成重开请求
#         并弹「放弃当前编辑重新打开」确认框——实机实证；无实测证据前一律不碰窗口）。
# 未知 app 默认 focus（数据安全优先：宁可聚焦提醒，也不静默丢发送）。
REUSE_FOCUS_ON_OPEN = {"photoshop": "focus", "sai2": "noop"}

# 2026-10-16 P1-A'：自适应轮询间隔（三档，见 Daemon._current_poll_interval）。
#   1) 任意 watch 正在 settle（有待发布变更、尚未 finalize）-> SETTLE_SAMPLE_SEC
#      （settle 子系统需要细粒度采样，确保「停笔 ~300ms 内发布」）
#   2) 存在活跃 PS/SAI2 监视会话（编辑器开着、尚未稳定变更）-> FAST_POLL_SEC
#   3) 其余（空闲 / 仅 Blender 等慢节奏 app）-> 默认 2.0s（CPU ~0.38% 单核）
FAST_POLL_SEC = 0.5
FAST_APPS = ("photoshop", "sai2")
SETTLE_SAMPLE_SEC = 0.15       # settle 子系统采样节拍（150ms）
SETTLE_STABLE_SEC = 0.3        # 无可靠写入间隔（仅 1 次写入 / watcher 未续报）时的
                               # 保守固定窗口；足以桥接分块写间隔、杜绝半写回传。
# 2026-10-18 P1 修复（真机日志 + 无头回归测试双实证）：用户真机日志
# `last_write_gap=0.019s` 但 `adaptive_window=0.300s` 卡死——根因是「固定
# SETTLE_STABLE_SEC 下限」永远压制了「按实际块间隔收窄」的自适应窗口。改为：
#   · watcher 测得**真实正间隔**（last_write_gap>0）-> 窗口 = max(SETTLE_MIN_WINDOW,
#     min(gap, STABLE_GAP_CAP)*STABLE_GAP_SAFETY)。gap*SAFETY 保证窗口 > 真实写入间隔
#     （真机 0.019s -> 0.12s 级窗口，比 0.3s 快 ~2.5×）；快存（gap 小）窗口随尾部节奏收敛。
#   · 仅 1 次写入 / watcher 未续报（last_write_gap==0，无头测试里常见：目录监听只捕获
#     到首写、后续块未续报）-> 退化 SETTLE_STABLE_SEC=0.3，**绝不**用极小窗口。
#     （初版用 0.05s 极小窗口压延迟，却因 stale gap=0 被误判为「极小间隔」触发提前
#      finalize、回传半写——已被 P1-3 无头回归测试抓出并修复。）
# SETTLE_MIN_WINDOW 是「有真实间隔时」的安全地板：必须 > 可能的块间隔（测试 0.1s 级），
# 否则在「写间隔 < 窗口」时仍会误发；取 0.12s 即在测试 0.1s 块间隔上有余量、又远低于
# 旧 0.3s、真机实测 save->detect 可压到 <0.2s。
SETTLE_MIN_WINDOW = 0.12
STABLE_GAP_SAFETY = 1.8
STABLE_GAP_CAP = 2.0

# 2026-10-17 修正（用户真机反馈）：PS/SAI2 在 Windows 上是**原地覆盖写**，
# 不是 tmp->rename——实测 ctime_ns 不变（见 ctime 探针），原 rename/ctime 分支
# 永远不触发，settle 只能走「稳定 0.3s」兜底，save->detect 卡在 ~1.0s。
# 改用 Windows 原生 ReadDirectoryChangesW（见 WinDirWatcher）在每次写入后即时唤醒
# 主循环（paced_wait），让 mtime/size 双采样 settle 以高节拍快速判定保存完成
# （~0.45s，远快于旧 ~1.0s）。watcher 只做唤醒，finalize 一律走 settle 分支。
FS_DEBOUNCE_SEC = 0.12         # 文档化常量：单纯「最后写入距今」去抖曾用于 finalize，
                               # 但分块保存会把块间间隔误判为完成（真机 e2e returns=[1,1]），
                               # 故已不再直接驱动 finalize；保留供参考。
# 2026-10-17 修正（用户真机证伪）：原 0x00000001|0x00000002 只含 FILE_NAME|DIR_NAME，
# **漏了 FILE_NOTIFY_CHANGE_LAST_WRITE(0x10)**。PS/SAI2 是**原地覆盖写**，保存只触发
# ACTION_MODIFIED(3)（= last-write 变更），不在 FILE_NAME 过滤里 -> watcher 永远收不到
# 事件 -> get_last_change 恒为 0 -> 只能走 stable 兜底（用户日志全 stable 即此因）。
# 此前探针用 os.replace(改名) 验证，改名走 FILE_NAME 故「通过」，掩盖了原地写漏报。
# 修正：FILE_NAME | SIZE | LAST_WRITE（0x1|0x8|0x10），原地写/改名/变尺寸全覆盖。
FS_WATCH_FILTER = 0x00000001 | 0x00000008 | 0x00000010   # FILE_NOTIFY_CHANGE_FILE_NAME | SIZE | LAST_WRITE

# 2026-10-17 结论（真机 e2e 证伪 + 修复）：WinDirWatcher 只用于「有写入即唤醒主循环」
# （见 run() 的 paced_wait + _changed），**绝不**直接驱动 finalize。原因：单纯用
# 「最后写入距今 >= FS_DEBOUNCE_SEC」去抖，在分块保存（编辑器每次 flush/fsync 把相邻
# 块的间隔拉长到 0.13~0.37s，超过 0.12s）时会把「块之间的间隔」误判成「保存完成」，
# 回传出半写文件（真机 e2e 实测 returns=[1,1]，P1-C 半写红线被击穿）。单一「安静间隔」
# 阈值永远无法区分「块间间隔」与「真正停笔」——因为间隔本身会变（fsync 抖动）。
# 真正安全且足够快的判定由下方 advance() 的 mtime/size 双采样 settle 负责：它**任意
# 一次变化都会重置稳定计时**（changed 即重置 stable_since_mono），要求连续稳定达到
# SETTLE_STABLE_SEC 才 finalize；配合 _current_poll_interval 在保存进行中降到
# SETTLE_SAMPLE_SEC=0.15s，停笔后 ~0.3s 内即可回传（~0.45s，远快于旧 ~1.0s，且红线守住）。
# 故 watcher 仅作唤醒，finalize 一律走 settle 分支。

# 主循环最小周期（秒）。ReadDirectoryChangesW 每次写入都会置位 `_changed`，
# 若完全依赖「有变更即唤醒」会让 run() 在高频保存时忙等（每秒数千轮 cycle），
# 既烧 CPU 又冲刷中转服务 HTTP、拖垮 claim。限频到 ~20Hz 足以把 save->detect
# 控制在 50ms 量级，同时杜绝忙等。（2026-10-17 补强，修复 e2e 抖动根因）
MIN_CYCLE_SEC = 0.05

# 2026-10-16 P1-C：settle 尺寸分档兜底上限（仅「持续变化超预期」时启用）。
# 表：文件当前字节数 -> 最大等待秒；大文件慢写场景不丢内容。
_SETTLE_FALLBACK_TIERS = (
    (256 * 1024, 0.8),
    (1 * 1024 * 1024, 1.2),
    (2 * 1024 * 1024, 1.6),
    (4 * 1024 * 1024, 2.0),
    (8 * 1024 * 1024, 2.8),
    (16 * 1024 * 1024, 3.8),
    (64 * 1024 * 1024, 6.0),
    (float("inf"), 10.0),
)


def settle_fallback_maxsec(size_bytes: int) -> float:
    """按当前文件尺寸查表返回 settle 兜底最大等待秒。"""
    n = int(size_bytes or 0)
    for threshold, secs in _SETTLE_FALLBACK_TIERS:
        if n < threshold:
            return secs
    return 10.0

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
    """GBK 控制台下以 UTF-8 写出，避免中文乱码（pythonw 运行时 stdout 为 None）。

    修复：直接 sys.stdout.write 在 GBK 代码页控制台会把 UTF-8 字符串按 GBK 解释
    而显示乱码。改为走 stdout.buffer 显式 encode("utf-8")，文件日志本身已是
    utf-8（log() 以 encoding="utf-8" 打开），不受此影响。
    """
    try:
        if sys.stdout is not None:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write((text + "\n").encode("utf-8", "replace"))
                buf.flush()
            else:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """Windows 下不依赖 tasklist 的进程存活检查。

    2026-10-14：增加退出码判定。只凭 OpenProcess 句柄能打开就判「活着」
    是不够的——进程已退出但父进程尚未回收（zombie）时句柄同样打得开，
    会把死进程当活的（--stop 干等 20s 超时、以及旧实例变 zombie 后
    新实例被「已有实例在运行」挡在门外的根因）。
    GetExitCodeProcess 返回 STILL_ACTIVE(259) 才是真活着。
    """
    if not pid or pid <= 0:
        return False
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED(5) = 进程存在但没权限开句柄
            return ctypes.windll.kernel32.GetLastError() == 5
        try:
            exit_code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            if ok and exit_code.value != STILL_ACTIVE:
                return False        # 已退出（可能只是还没被父进程回收）
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _process_image_name(pid: int) -> str:
    """返回进程映像全路径（ctypes，无需 psutil）。失败返回空串。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _process_start_age(pid: int) -> float:
    """进程已运行秒数；取不到返回极大值（视为很老）。"""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 1e9
        try:
            ct = ctypes.c_ulonglong(0)
            et = ctypes.c_ulonglong(0)
            kt = ctypes.c_ulonglong(0)
            ut = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetProcessTimes(
                    handle, ctypes.byref(ct), ctypes.byref(et),
                    ctypes.byref(kt), ctypes.byref(ut)):
                epoch = 11644473600.0   # FILETIME(1601) -> Unix(1970) 秒
                creation_s = ct.value / 1e7 - epoch
                return max(0.0, time.time() - creation_s)
            return 1e9
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return 1e9


def _is_our_daemon(pid: int) -> bool:
    """pid 是否为本编辑器守护（pythonw/python 映像）。靠 pid 文件归属 + 映像名兜底，
    避免 pid 复用误判（别的进程占了旧 pid 号）。"""
    name = _process_image_name(pid)
    if not name:
        return False
    low = name.lower()
    return "python" in low      # 守护由 pythonw.exe / python.exe 运行


def _force_kill_pid(pid: int) -> None:
    """强制结束进程（taskkill /F）。失败静默。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        pass


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


def _request_full(method: str, path: str, body=None, server: str = DEFAULT_SERVER,
                  timeout: float = HTTP_TIMEOUT):
    """同 _request，但返回 (HTTP 状态码, 应答体 dict)；4xx/5xx 不抛异常。
    供「必须记录 server 应答」的关键调用（回传 publish）使用。"""
    url = server + API + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            body_json = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body_json = {}
        return exc.code, body_json


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


def _atomic_write_json(path: Path, obj, retries: int = 1, retry_delay: float = 0.1) -> None:
    """写临时文件 + os.replace 原子替换。

    retries>1 时在失败时重试（默认 3 次 × 100ms），用于规避杀软锁文件 /
    托盘并发读导致的瞬时 PermissionError(13)。临时文件用 mkstemp 生成唯一名，
    避免两进程/两线程共用同一 `xxx.tmp` 互相踩（并发写冲突）。
    """
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    last_exc = None
    for i in range(max(1, retries)):
        fd, tmp_str = tempfile.mkstemp(dir=str(path.parent),
                                       prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_str, str(path))
            return
        except Exception as exc:  # PermissionError / 文件被占用等
            last_exc = exc
            try:
                os.remove(tmp_str)
            except Exception:
                pass
            if i < retries - 1:
                time.sleep(retry_delay)
    raise last_exc


# ---------------------------------------------------------------------------
# ZBrush 握手（任务文件 + 回执 + 心跳 + 托盘通知）
# ---------------------------------------------------------------------------

def zb_dirs(payload_dir: str):
    inbox = os.path.join(payload_dir, ZB_INBOX) if payload_dir else ""
    outbox = os.path.join(payload_dir, ZB_OUTBOX) if payload_dir else ""
    hb = os.path.join(payload_dir, ZB_HEARTBEAT) if payload_dir else ""
    return inbox, outbox, hb


def zb_done_dir(payload_dir: str):
    """inbox 下的 done/ 子目录：被消费的任务文件 + 回执都归集到这里。"""
    inbox, _, _ = zb_dirs(payload_dir)
    d = os.path.join(inbox, ZB_DONE) if inbox else ""
    try:
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def zb_bridge_alive(payload_dir: str, max_age: float = ZB_BRIDGE_MAX_AGE) -> bool:
    """ZBrush 侧常驻桥接循环是否活着（心跳 freshness）。"""
    _, _, hb = zb_dirs(payload_dir)
    if not hb or not os.path.isfile(hb):
        return False
    try:
        return (time.time() - os.stat(hb).st_mtime) <= max_age
    except OSError:
        return False


def zbrush_process_running() -> bool:
    """可靠判断 ZBrush 是否在运行（ctypes 进程快照，不依赖 GIL 心跳）。

    ZBrush 是**单实例**：已有实例在运行时，再 spawn 新实例必然超时/冲突，
    守护进程侧的「一次性 -script 拉起」路径正是因此失败（用户实机 2026-10-16
    铁证：已用一次性脚本拉起 -> 超时，因为本机 ZBrush 已经在跑）。故投递决策以
    「进程是否在运行」为准，而非插件心跳（内嵌 VM 把 GIL 留在主线程，观察线程
    拿不到调度 -> 心跳经常不新鲜，误判为「桥接不在」而错误走 spawn 路径）。
    非 Windows 环境（测试桩）安全返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.CHAR * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return False
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(pe)
            found = False
            if kernel32.Process32First(snapshot, ctypes.byref(pe)):
                while True:
                    name = pe.szExeFile.decode("mbcs", "ignore")
                    if name.lower() == "zbrush.exe":
                        found = True
                        break
                    if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                        break
            return found
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
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
    d = zb_done_dir(payload_dir)
    return os.path.join(d, tid + ".done.json") if d else ""


def zb_take_receipt(payload_dir: str, tid: str):
    """读回执并消费（删除）。没有则返回 None。"""
    p = zb_receipt_path(payload_dir, tid)
    if not p or not os.path.isfile(p):
        return None
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8-sig"))
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
        # 2026-10-17 新增（用户需求）：发给 PS/SAI2 后是否强制把编辑器窗口
        # 拉到前台。默认关闭——SAI2 聚焦会触发「放弃当前编辑重新打开」确认框，
        # 故默认不碰窗口；开启时用户接受该代价，无视 REUSE_FOCUS_ON_OPEN 的 noop 策略。
        "focus_on_open": False,
    }
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            if str(data.get("server_url", "")).strip():
                cfg["server"] = str(data["server_url"]).strip()
            for name, exe in (data.get("apps") or {}).items():
                if exe:
                    cfg["apps"][str(name)] = str(exe)
            if str(data.get("zbrush_outbox_to", "")).strip():
                cfg["zbrush_outbox_to"] = str(data["zbrush_outbox_to"]).strip()
            if data.get("focus_on_open") is True:
                cfg["focus_on_open"] = True
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

# 2026-10-14 P0：原 _file_locked()（msvcrt O_RDWR 锁探测）已移除，勿重新引入。
# 实测证据（用户机器，三条独立现象）：
#   1) watch 存在时 SAI2 操作明显迟钝（不跟手），watch 消失后立刻恢复；
#   2) 单独跑 O_RDONLY 探测同一文件 30 次，SAI2 无卡顿；
#   3) Photoshop 全程不卡——PS 读文件后不监听文件变更。
# 即 O_RDWR 打开会触发 SAI2 的文件变更监听导致反复重读/刷新，而「持锁」
# 这个信号对 SAI2 本身不可靠（不持锁）。监视的保留/解除改用
# 「编辑器进程存活 + 距上次保存的空闲时长（WATCH_IDLE_SEC）」判定，
# 不值得为不可靠的信号牺牲编辑器流畅度。


# ---------------------------------------------------------------------------
# WinDirWatcher：Windows 原生目录变更监听（ReadDirectoryChangesW）
# ---------------------------------------------------------------------------
# 2026-10-17 修正（用户真机反馈）：PS/SAI2 在 Windows 是**原地覆盖写**，不是
# tmp->rename，ctime_ns 不变 -> 原 ctime/rename 分支永不触发，settle 只能走
# 0.3s 稳定兜底（save->detect ~1.0s）。改用 ReadDirectoryChangesW 在每次写入后
# 即时唤醒主循环（paced_wait），让 mtime/size 双采样 settle 快速判定保存完成
# （~0.45s，远快于旧 ~1.0s）。watcher 只做唤醒，finalize 走 settle 分支。
# 设计：每个被监视目录一条后台线程，ReadDirectoryChangesW 同步阻塞等变更，
# 把受影响的「绝对文件路径 -> 单调时钟」写进共享字典，并置位变更事件唤醒主循环。
# 非 Windows 平台 available=False，调用方退回 mtime 轮询（功能等价、仅稍慢）。
class WinDirWatcher(object):
    def __init__(self):
        self.available = (sys.platform == "win32")
        self._lock = threading.Lock()
        self._last = {}          # abspath -> monotonic 最后变更时刻
        self._ref = {}           # abspath(dir) -> 引用计数（多个 watch 同目录）
        self._threads = {}       # abspath(dir) -> 线程
        self._handles = {}       # abspath(dir) -> 目录句柄
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._log = None              # 调试日志回调（Daemon 注入 self.log）；None=静默

    # -- 公共 API ----------------------------------------------------------
    def _init_api(self):
        """配置 ReadDirectoryChangesW / CreateFileW / CloseHandle 的 ctypes 签名。
        不设 argtypes 时 ctypes 按 Python 类型推断，HANDLE 这类指针型参数在 64 位
        下可能传错——显式声明可消除该风险（真机实测前的关键加固）。"""
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                        ctypes.c_uint32, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_void_p]
            k32.ReadDirectoryChangesW.restype = ctypes.c_int
            k32.ReadDirectoryChangesW.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_int, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CancelIoEx.restype = ctypes.c_int
            k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        except Exception:
            pass

    def start(self):
        if not self.available:
            return
        # 线程在 add_dir 时按需创建，start 仅重置停止标志
        self._init_api()
        self._stop.clear()

    def add_dir(self, d):
        if not self.available or not d:
            return
        d = os.path.abspath(d)
        with self._lock:
            if self._ref.get(d, 0) > 0:
                self._ref[d] += 1
                return
            self._ref[d] = 1
        self._spawn(d)

    def remove_dir(self, d):
        if not self.available or not d:
            return
        d = os.path.abspath(d)
        with self._lock:
            n = self._ref.get(d, 0) - 1
            if n > 0:
                self._ref[d] = n
                return
            self._ref.pop(d, None)
            h = self._handles.pop(d, None)
            th = self._threads.pop(d, None)
        # 2026-10-19 真机修复（P0：纯查看退出 → 守护进程直接挂掉、daemon.log 无栈）：
        # 旧实现这里主线程 CloseHandle(h)，而监听线程 _loop 的 finally 也会 CloseHandle
        # 同一个 h → **双重 CloseHandle**。句柄被 OS 复用后第二次关闭可能误关进程其它句柄，
        # 触发进程级崩溃（ctypes 层，无 Python 栈，故日志里看不到）。
        # 改为：主线程只 CancelIoEx 取消阻塞中的 ReadDirectoryChangesW 让线程自行退出，
        # 句柄由线程 _loop 的 finally 独占关闭（单一关闭点），彻底消除竞争与双重关闭。
        if h:
            try:
                ctypes.windll.kernel32.CancelIoEx(h, None)
            except Exception:
                pass
        if th is not None:
            th.join(timeout=2.0)

    def get_last_change(self, path):
        if not self.available or not path:
            return 0.0
        with self._lock:
            return self._last.get(os.path.abspath(path), 0.0)

    def note_change(self, path):
        """外部（测试/兼容）手动标记某文件刚变更。"""
        if not path:
            return
        with self._lock:
            self._last[os.path.abspath(path)] = time.monotonic()
        self._changed.set()

    def wait(self, timeout):
        """主循环替代 time.sleep：有变更事件时立即唤醒。
        返回 "changed"（被变更事件唤醒）或 "timeout"（等满 timeout 才继续）。"""
        if not self.available:
            time.sleep(timeout)
            return "timeout"
        got = self._changed.wait(timeout)
        self._changed.clear()
        return "changed" if got else "timeout"

    def paced_wait(self, interval):
        """主循环每次轮询后的等待：变更即唤醒，但相邻两次间隔强制 >= MIN_CYCLE_SEC，
        避免高频写入把 `_changed` 持续置位导致 run() 忙等（每秒数千轮 cycle ->
        冲刷中转服务 HTTP、拖垮 claim）。空闲时仍是 wait(interval) 的正常节奏，
        仅在「变更密集」时被节流到 ~1/MIN_CYCLE_SEC。
        【调试】记录本次唤醒来源（changed=被 ReadDirectoryChangesW 唤醒 / timeout=纯轮询），
        用于定位 save->detect 偏大（唤醒是否真生效）的根因。"""
        c0 = time.monotonic()
        src = self.wait(interval)
        el = time.monotonic() - c0
        if el < MIN_CYCLE_SEC:
            time.sleep(MIN_CYCLE_SEC - el)
        if self._log and interval >= 0.1:
            self._log("[paced_wait] woke_by={0} interval={1:.2f}s el={2:.3f}s".format(
                src, interval, el))

    def stop(self):
        if not self.available:
            return
        self._stop.set()
        with self._lock:
            items = [(d, self._handles[d], self._threads[d])
                     for d in list(self._handles.keys())]
            self._handles.clear()
            self._threads.clear()
        # 仅取消阻塞中的 I/O 让监听线程退出；句柄由各自线程 _loop 的 finally 独占关闭，
        # 这里不再 CloseHandle（否则与线程竞争 → 双重关闭 → 进程级崩溃）。
        for d, h, th in items:
            try:
                ctypes.windll.kernel32.CancelIoEx(h, None)
            except Exception:
                pass
        for d, h, th in items:
            if th is not None:
                try:
                    th.join(timeout=2.0)
                except Exception:
                    pass

    # -- 内部 --------------------------------------------------------------
    def _spawn(self, d):
        try:
            h = self._open_dir(d)
        except Exception as exc:
            self.log_ext("WinDirWatcher 打开目录失败 {0}: {1!r}".format(d, exc))
            with self._lock:
                self._ref.pop(d, None)
            return
        if h is None:
            with self._lock:
                self._ref.pop(d, None)
            return
        with self._lock:
            self._handles[d] = h
        th = threading.Thread(target=self._loop, args=(d, h), daemon=True)
        th.start()
        with self._lock:
            self._threads[d] = th
        if self._log:
            self._log("[WinDirWatcher] watching dir={0}".format(d))

    def _open_dir(self, d):
        FILE_LIST_DIRECTORY = 0x0001
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        h = ctypes.windll.kernel32.CreateFileW(
            ctypes.c_wchar_p(d), FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if h is None or h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            return None
        return h

    def _loop(self, d, h):
        buf = ctypes.create_string_buffer(8192)
        overlapped = None
        bytes_ret = ctypes.c_uint32(0)
        try:
            while not self._stop.is_set():
                ok = ctypes.windll.kernel32.ReadDirectoryChangesW(
                    h, buf, ctypes.sizeof(buf), False,
                    FS_WATCH_FILTER, ctypes.byref(bytes_ret), overlapped, None)
                if not ok:
                    if self._log:
                        self._log("[WinDirWatcher] loop ended (ReadDirectoryChangesW returned 0) "
                                  "dir={0} -> 该目录监听已失效".format(d))
                    break
                self._parse(d, buf, bytes_ret.value)
        except Exception as exc:
            if self._log:
                self._log("[WinDirWatcher] loop exception dir={0}: {1!r} -> 监听失效".format(d, exc))
        finally:
            if self._log:
                self._log("[WinDirWatcher] thread exited dir={0}".format(d))
            try:
                ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass

    def _parse(self, d, buf, n):
        # FILE_NOTIFY_INFORMATION：NextEntryOffset(ULONG) Action(ULONG) FileNameLength(ULONG) FileName(WCHAR[])
        ACTION_MODIFIED = 3
        ACTION_ADDED = 1
        ACTION_RENAMED_NEW = 4
        off = 0
        now = time.monotonic()
        changed_any = False
        while off + 12 <= n:
            try:
                next_off, action, name_len = struct.unpack_from("<LLL", buf, off)
                name = buf[off + 12: off + 12 + name_len].decode(
                    "utf-16-le", "ignore")
            except Exception:
                break
            if action in (ACTION_MODIFIED, ACTION_ADDED, ACTION_RENAMED_NEW):
                full = os.path.join(d, name)
                with self._lock:
                    self._last[os.path.abspath(full)] = now
                changed_any = True
                if self._log:
                    self._log("[WinDirWatcher] CAUGHT action={0} name={1} mono={2:.3f}".format(
                        action, name, now))
            if next_off == 0:
                break
            off += next_off
        if changed_any:
            self._changed.set()

    def log_ext(self, msg):
        # 轻量日志（无 daemon 引用时静默）
        try:
            sys.stderr.write("[WinDirWatcher] " + msg + "\n")
        except Exception:
            pass


class Watch(object):
    def __init__(self, record: dict, files, proc, watcher=None, app_exe=""):
        self.tid = record["id"]
        self.app = _norm_app(str((record.get("to") or {}).get("app", "")).lower())
        self.asset_name = record.get("asset_name", "")
        frm = record.get("from") or {}
        self.from_app = _norm_app(str(frm.get("app", "")).lower())
        self.from_project = str(frm.get("project", "") or "")
        self.files = [f for f in (files or []) if f]
        self.dir = os.path.dirname(self.files[0]) if self.files else ""
        self.watcher = watcher          # WinDirWatcher（None=非 Windows/未启用）
        self.orig_options = record.get("options") or {}   # 回传时原样合并回去
        # 2026-10-15 用户复核返工：身份标识只在 __init__ 算一次并缓存到
        # self.identity，dedup_key() 直接复用缓存（不再含内容 sha256，见
        # asset_identity：回退分支只认主文件名，避免「同名不同内容不复用」）。
        self.identity = self.asset_identity(self.orig_options, self.files)
        self.proc = proc
        self.opened_at = now_iso()
        self.opened_mono = time.monotonic()
        self.proc_exited_at = None      # 单调时钟
        self.delegated = False          # 启动即退 = 委托给已在运行的实例
        self.app_exe = app_exe          # 编辑器可执行文件路径（委托场景探测真实进程是否还在）
        self.dirty_ts = 0.0             # 单调时钟
        self.dirty_names = []
        snap = self.snapshot()
        self.baseline = snap            # 上次「打开/回传」时的稳定快照
        # 2026-10-15 P1-3 返工（settle 半写文件）：上一轮快照。advance()
        # 逐文件比较 (mtime, size)，任一监视文件相对上一轮仍在变化就重置
        # 稳定计时。旧实现比较的是「变化文件集合」——同一文件持续写入时
        # 集合不变、计时器不重置，首次检测后 SETTLE_SEC 就把写了一半的
        # 大文件 finalize（3~10s 的保存会被拦腰截断）。
        self.last_snap = snap
        # 2026-10-14 P0（同一文件二次编辑不回传）：回传成功后 watch 不再
        # 无条件移除，而是复位基线继续监视，支持同一文件多次
        # 「改-保存-回传」。这三个字段支撑该状态机。
        self.returns = 0                # 已回传次数（brief 可见）
        self.last_activity_mono = time.monotonic()   # 最近一次保存/回传时刻
        self.finalize_at = None         # 本次 finalize 触发时刻（计时埋点，wall clock）
        # 2026-10-16 P1-C：settle 自适应状态
        self.stable_since_mono = None   # 首次出现「停笔稳定」采样的时刻（monotonic）
        self.settle_start_mono = None   # 首次检测到变更的时刻（monotonic）
        self.settle_max_wait = 0.0      # 本次 settle 的尺寸分档兜底上限（秒）
        self.last_settle_reason = ""    # 本次 finalize 触发原因：stable/fallback/rename
        self._last_ctime_dbg = 0.0      # ctime 调试日志限频（单调时钟）
        # 2026-10-18 自适应稳定窗口：记录本保存会话「相邻写入最大间隔」，据此抬高
        # 稳定判定窗口（见 advance()），避免分块保存块间间隔被误判为保存完成。
        self._prev_write_ts = 0.0       # 上一轮 advance 读到的 watcher 最后写入时刻（monotonic）
        self._last_write_gap = 0.0      # 最近一次写入与上一次写入的间隔（monotonic 秒）；用于自适应稳定窗口
        # 2026-10-19：是否曾检测到文件变化（mtime/size 相对 baseline 改变过）。
        # 用于区分「纯查看未保存就关编辑器」与「编辑过但没在退出前回传」：前者
        # 静默作废（不弹错误回传），后者保留原 abandon 错误提示。
        self.ever_dirty = False
        self.log = lambda msg: None     # 由 Daemon.handle 注入真实日志；默认无操作

    # -- 快照：原文件 + 同目录新图片文件 ------------------------------------
    # 元信息 = (mtime_ns, size, ctime_ns)。ctime_ns 是「文件身份」信号：
    #   · rename / 替换（PS「写临时文件 -> rename 覆盖目标」）→ 目标文件被新文件取代，
    #     ctime（Windows 创建时间 / Unix inode 变更时间）变化；
    #   · 原地增量覆盖（SAI2 风格）→ 同一文件对象，ctime 不变，只有 mtime/size 变。
    # 据此可在 advance() 里区分「rename 完成（可立即发布）」与「仍在原地写」。
    def snapshot(self) -> dict:
        snap = {}
        for f in self.files:
            name = os.path.basename(f)
            try:
                st = os.stat(f)
                snap[name] = (st.st_mtime_ns, st.st_size, st.st_ctime_ns)
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
                        snap[name] = (st.st_mtime_ns, st.st_size, st.st_ctime_ns)
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

    @staticmethod
    def asset_identity(orig_options: dict, files) -> tuple:
        """逻辑「源资产标识」：去重 / 窗口复用只认它（**不再含内容 sha256**）。

        2026-10-15 P0-1 返工 -> 2026-10-15 用户复核返工（关键修正）：
        原回退键 ("file_name", 主文件名, 内容 sha256) 在「我改一版发一版」
        工作流里是错的——同名不同内容的两次发送 key 不同 -> 不复用 ->
        又堆窗口（正是 P0-1 阻塞级没修到的 Blender/Maya -> PS/SAI2 分支；
        今天该分支就是真实流量，绝非理论可达）。

        现改为：**内容不进键**。UE 优先用 asset_paths（稳定、不同目录天然
        区分）；回退分支只认主文件名。于是「同名即同一窗口」：重发同名文件
        复用已有窗口（覆盖稳定路径内容 + 重新聚焦，见 Daemon.handle），不论
        内容是否变了。

        残余风险（理论，非活跃缺口）：回退分支不同目录的同名资产因无
        asset_paths 会判成同一窗口（串台）；UE 有 asset_paths 不受影响。

        TODO(2026-10-15, issue: 回退分支串台-Blender/Maya 贴图发送前置条件):
        当前 Blender/Maya 贴图发送功能尚未实现（用户确认），回退分支无真实
        流量，串台风险为零。**但这是 Blender/Maya 贴图发送功能上线的前置
        条件**：届时插件侧必须在发送的 options 里带上 asset_paths（稳定资产
        路径，见 asset_paths 分支），或把回退键改为含资产路径的键；否则不同
        目录的同名资产会被判成同一编辑会话（旧 watch 被误杀/串台）。该前置
        条件挂在 Blender/Maya 发送功能的待办上，不随本 release 交付消失。
        """
        opts = orig_options or {}
        paths = [str(p) for p in (opts.get("asset_paths") or [])
                 if str(p).strip()]
        if paths:
            return ("asset_paths", tuple(sorted(paths)))
        base = os.path.basename(files[0]).lower() if files else ""
        return ("file_name", base)

    def dedup_key(self):
        """同一编辑会话的判定键：(to_app, from_app, 资产标识)。

        2026-10-14 P0 首发：不能用绝对路径——server 按 tid 分 payload
        目录，同一资源每次发送都是新目录，按绝对路径去重永远命中不了
        （实测同一文件连发 5 次，watching 堆了 5 条，SAI2 被反复探测）。

        2026-10-15 P0-1 返工：原键 (app, 主文件名) 有误杀风险——不同
        目录的同名文件（不同资产）会被当成同一会话，旧 watch 被直接移除、
        回传目标被悄悄换掉。现在资产标识优先取 options.asset_paths
        （UE 提供，重复发送同一资产时稳定，不同目录天然区分），缺失才
        回退「主文件名 + 内容摘要」（见 asset_identity：Blender 今天
        就走回退分支，只凭文件名会误杀，必须叠内容才能区分）。from_app
        也进键：不同发送端的同名文件是不同会话（回传目标不同），不应
        互杀；from_app 为空的传输在 handle() 已被 error 收尾，watch
        层不存在 None 来源，键里不会是 (to_app, None, ...)。

        identity 在 __init__ 算一次并缓存（回退分支含 sha256，重算浪费）。
        """
        return (self.app, self.from_app, self.identity)

    def brief(self) -> dict:
        return {
            "tid": self.tid,
            "app": self.app,
            "asset_name": self.asset_name,
            "return_to": self.from_app,
            "files": [os.path.basename(f) for f in self.files],
            "opened_at": self.opened_at,
            "pending_save": bool(self.dirty_names),
            "returns": self.returns,
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

        # 2026-10-16 P1-C：settle 自适应（替代固定 SETTLE_SEC 等待）。
        # 每周期对全部监视文件采一次 (mtime,size)：
        #  - 相对上一轮采样有任一变化 -> 视为「还在写」，清空稳定计时
        #    （stable_since=None）并记录 settle 起始时刻（last_activity 同步更新）；
        #  - 停笔后「连续稳定达到 SETTLE_STABLE_SEC 时长」即判定稳定并发布
        #    （正常 ~300ms，远快于旧 1.5s；用「时长」而非「采样次数」判定，
        #    可桥接编辑器分块写之间的短暂间隔，避免半写回传）；
        #  - 若迟迟不稳定（编辑器持续写入/自动保存），以「尺寸分档兜底上限」
        #    作为发布上限，避免半写文件被无限期挂起。
        # PS（临时文件+rename）与 SAI2（增量覆盖写）两种保存模式都能正确
        # 处理（详见 release notes P1-C 前置确认）。
        cur = self.snapshot()
        prev_snap = self.last_snap
        changed = any(cur.get(name) != prev_snap.get(name)
                      for name in cur)
        self.last_snap = cur
        dirty_now = [name for name, meta in cur.items()
                     if self.baseline.get(name) != meta]
        self.dirty_names = dirty_now
        # 调试（2026-10-17 用户要求）：打印受监视主文件的 ctime_ns，确认 Windows 上
        # 原地覆盖写时 ctime 不变（故 rename 分支不触发，必须靠 ReadDirectoryChangesW）。
        if changed and self.files:
            if now_mono - self._last_ctime_dbg >= 2.0:
                self._last_ctime_dbg = now_mono
                nm = os.path.basename(self.files[0])
                cm = cur.get(nm)
                pm = prev_snap.get(nm)
                cnow = cm[2] if isinstance(cm, tuple) and len(cm) == 3 else None
                cprev = pm[2] if isinstance(pm, tuple) and len(pm) == 3 else None
                self.log("[settle-debug] tid={0} file={1} ctime_ns now={2} prev={3} "
                         "ctime_changed={4}".format(
                             self.tid, nm, cnow, cprev, cnow != cprev))

        # rename / 文件替换检测（2026-10-16 用户纠正）：目标文件被新文件取代
        # （ctime_ns 变化）即视为「保存已完成（rename 原子）」，可立即发布，跳过
        # 稳定等待 -> save->detect 压到一次采样（~0.15s，< 0.3s）。
        # 仅对受监视的原文件（self.files）判定，且要求当前文件存在（meta 非 None）。
        # 原地增量覆盖（SAI2 风格）同一文件对象 ctime 不变 -> 不会误触发，走稳定分支。
        rename_detected = False
        for f in self.files:
            name = os.path.basename(f)
            cm = cur.get(name)
            pm = prev_snap.get(name)
            if (isinstance(cm, tuple) and len(cm) == 3 and cm[0] is not None
                    and isinstance(pm, tuple) and len(pm) == 3):
                if cm[2] != pm[2]:          # ctime_ns 变化 = 文件被替换
                    rename_detected = True
                    break
        if changed:
            self.dirty_ts = now_mono
            self.last_activity_mono = now_mono
            self.stable_since_mono = None
            if self.settle_start_mono is None:
                self.settle_start_mono = now_mono
        elif dirty_now:
            if self.stable_since_mono is None:
                self.stable_since_mono = now_mono
        else:
            # 相对 baseline 无任何变化：无待发布内容，复位 settle 状态
            self.settle_start_mono = None
            self.stable_since_mono = None
            self.dirty_ts = 0.0

        if dirty_now:
            self.ever_dirty = True
            cur_size = max((m[1] for m in cur.values() if m), default=0)
            self.settle_max_wait = settle_fallback_maxsec(cur_size)
            # 2026-10-17/18 修正（用户真机反馈 + e2e 证伪修复）：Windows 上 PS/SAI2 是
            # 原地覆盖写，ctime_ns 不变 -> rename 分支永不触发，settle 走 mtime/size
            # 双采样稳定。WinDirWatcher（ReadDirectoryChangesW）只在每次写入后即时唤醒
            # 主循环（paced_wait），让下方双采样以高节拍快速采样；**绝不**直接驱动
            # finalize（单纯「最后写入距今 >= 阈值」会在块间间隔误判完成，回传半写）。
            # 稳定窗口自适应（关键修复）：用 watcher 实际写入事件流记录「最近一次写入
            # 与上一次写入的间隔 _last_write_gap」（注意：是「最后一段」而非「全程最大」
            # 间隔）。要求连续稳定时长 >= max(SETTLE_STABLE_SEC, gap*SAFETY) 才 finalize。
            # 为什么用 last_gap 而非 max_gap：分块保存过程中某次 fsync 抖动会让中间块间
            # 间隔异常大，但真正停笔后只需等「最后一次写入后的安静时长 >= 最后一段间隔」
            # 即可判定完成；用 max_gap 会把窗口撑到整段保存的最大间隔，使 save->detect
            # 无谓放大到 0.6~1.3s（用户真机实测）。改 last_gap 后窗口随尾部节奏收敛。
            last_write = 0.0
            if self.watcher is not None and self.watcher.available and self.files:
                last_write = self.watcher.get_last_change(self.files[0])
                if last_write > 0.0:
                    if self._prev_write_ts > 0.0 and last_write > self._prev_write_ts:
                        self._last_write_gap = last_write - self._prev_write_ts
                        if self.log:
                            self.log("[settle-debug] watcher-write mono={0:.3f} now={1:.3f} "
                                     "gap_since_prev={2:.3f}s".format(
                                         last_write, now_mono, self._last_write_gap))
                    self._prev_write_ts = last_write
            # 自适应稳定窗口（2026-10-18 P1 修复）：
            #   · 测得**真实正间隔**（last_write>0 且 _last_write_gap>0）-> 窗口随尾部节奏收敛，
            #     但地板 SETTLE_MIN_WINDOW 保证 > 可能的块间隔，杜绝半写；
            #   · 仅 1 次写入 / watcher 未续报（_last_write_gap==0，无头测试常见）-> 保守 0.3s。
            # 旧版「gap<0.05 即压到 0.05s」会被 stale gap=0 误触发 -> 回传半写（P1-3 抓出），已废弃。
            if last_write > 0.0 and self._last_write_gap > 0.0:
                adaptive_window = max(SETTLE_MIN_WINDOW,
                                     min(self._last_write_gap, STABLE_GAP_CAP) * STABLE_GAP_SAFETY)
            else:
                adaptive_window = SETTLE_STABLE_SEC
            if rename_detected:
                # 文件被替换（rename/原子覆盖）完成 -> 立即发布，无需等稳定窗口
                self.last_settle_reason = "rename"
                self.finalize_at = time.time()
                return "finalize"
            stable_enough = (self.stable_since_mono is not None
                             and now_mono - self.stable_since_mono >= adaptive_window)
            overdue = (self.settle_start_mono is not None
                       and now_mono - self.settle_start_mono >= self.settle_max_wait)
            if stable_enough or overdue:
                # wall clock（与文件 mtime 同基准）：2026-10-14 修复
                # monotonic/wall clock 混用导致 save->detect 出天文负数
                self.last_settle_reason = "stable" if stable_enough else "fallback"
                self.finalize_at = time.time()
                if self.log:
                    _stable_for = (now_mono - self.stable_since_mono) if self.stable_since_mono else 0.0
                    self.log("[settle-debug] FINALIZE tid={0} reason={1} adaptive_window={2:.3f}s "
                             "last_write_gap={3:.3f}s stable_for={4:.3f}s".format(
                                 self.tid, self.last_settle_reason,
                                 adaptive_window, self._last_write_gap, _stable_for))
                return "finalize"

        # 2026-10-26 P0-2 诊断：每轮打印 watch 关键状态，便于确认「关闭软件后为何不取消监听」。
        self.log("[watch-state] tid={0} proc_exited_at={1} delegated={2} "
                 "dirty_names={3} ever_dirty={4} returns={5} proc_alive={6}".format(
                     self.tid,
                     "{0:.1f}s".format(now_mono - self.proc_exited_at) if self.proc_exited_at else "None",
                     self.delegated,
                     len(self.dirty_names),
                     self.ever_dirty,
                     self.returns,
                     (self.proc is not None and self.proc.poll() is None)))

        # 2026-10-26 P0-2：移除原 `not self.dirty_names` 守卫。原守卫会令「文件改过但未回传」
        # （dirty_names 非空）的 watch 永远进不了取消分支，只能等 600s 超时兜底。现改为：
        # 进程已退出且非委托态即进入取消判定，内层按 returns/ever_dirty/EXIT_GRACE 细分处理，
        # 满足「关闭软件即取消监听」预期（脏文件仍给 EXIT_GRACE_SEC 宽限，避免关窗瞬间丢失内容）。
        if self.proc_exited_at is not None and not self.delegated:
            if self.returns > 0:
                # 已成功回传（returns>0）：保留 REUSE_GRACE_SEC 的「关窗重发」复用
                # 窗口（见 Daemon.handle 复用分支）——超期只清 watch，绝不把已 done
                # 的传输翻成 cancelled（done->cancelled 回归会让回传结果凭空消失）。
                if now_mono - self.proc_exited_at >= REUSE_GRACE_SEC:
                    return "clear"
                return None
            # 2026-10-19：用户纯查看（打开文件看一眼、未做任何修改就退出）→ 文件
            # 从未变化过（ever_dirty=False）。这种场景不应弹「未保存，传输作废」错误
            # 回传，而应**静默作废** watch。只有「曾改过但退出前没回传」（ever_dirty
            # 且 returns==0）才保留原 abandon 错误提示。
            if not self.ever_dirty:
                return "abandon_silent"
            if now_mono - self.proc_exited_at >= EXIT_GRACE_SEC:
                return "abandon"

        # 超时兜底解 watch（2026-10-14）：编辑器进程已退/没有、
        # 且长时间无保存行为时解除监视，避免 watches 无限堆积
        if self._should_expire(now_mono):
            return "expire"
        return None

    def _should_expire(self, now_mono: float) -> bool:
        """超时兜底解 watch：长时间无保存行为 + 编辑器进程已退/没有。

        2026-10-14：移除 msvcrt 文件锁探测（O_RDWR 打开会干扰 SAI2，
        且「持锁」信号对 SAI2 不可靠），改为纯「空闲时长 + 进程存活」判定。
        """
        if now_mono - self.last_activity_mono < WATCH_IDLE_SEC:
            return False
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    return False        # 编辑器还开着：继续等，不超时
            except Exception:
                return False
        return True


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
        self.watcher = WinDirWatcher()  # Windows 原生目录监听（settle 加速）
        self.watches = []
        self.zb_pending = []            # ZBrush 任务投递等待回执的列表
        self._zb_delivered = set()      # 2026-10-18 终局：已投递 tid 去重集合，
                                        #   防同一任务被反复写 inbox / 反复 append
        self._zb_rehydrated = False     # 启动后是否已从磁盘恢复上一实例遗留的 ZBrush
                                        #   投递状态（防 Bridge 重启死锁，见 _zb_rehydrate）
        self.route_focus = {}           # 流转设置逐路由 auto_focus 缓存（供 SAI2/PS 聚焦决策）
        self.zb_returned = 0            # 热文件夹回传计数
        self.handled = 0
        self.returned = 0
        self.last_event = "守护进程启动中"
        self.stopping = False
        self._rl = {}          # 日志限频表
        self._cycle_count = 0
        self._last_poll_reason = ""   # 上一轮轮询档位（用于 [poll] 切换日志）

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

    def log_timing_claim(self, tid: str, rec: dict) -> None:
        """分段计时：server 建传输 -> daemon claim 到（轮询间隔占大头）。"""
        created = str(rec.get("created_at") or "")
        if not created:
            return
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except Exception:
            try:
                ts = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
            except Exception:
                return
        gap = time.time() - ts
        if gap < 0:
            return
        self.log("[timing] tid={0} created->claim={1:.1f}s "
                 "(poll_interval={2:.1f}s)".format(tid, gap, self.interval))

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

    def req_full(self, method: str, path: str, body=None):
        """返回 (HTTP 状态码, 应答体)；网络异常时 (None, {"_exception": ...})。
        用于关键调用（回传 publish）的应答留痕。"""
        try:
            return _request_full(method, path, body, server=self.server)
        except Exception as exc:
            return None, {"_exception": repr(exc)}

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
            inbox, _, _ = zb_dirs(self.payload_dir)
            self.log("[payload] payload_dir={0} zbrush_inbox={1}".format(
                self.payload_dir, inbox))
            self._refresh_route_focus()

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

        self.log("守护进程启动 pid={0} server={1} state={2} build=20261026 fix=Issue2-leftover-keep+Issue5-utf8".format(
            os.getpid(), self.server, self.state_dir))
        self.event("守护进程已启动（{0}）".format(
            "、".join(APP_LABELS.get(a, a) for a in self.cfg["apps"])))

        self.watcher.start()             # 启动 Windows 原生目录监听线程（非 Win 平台空操作）
        self.watcher._log = self.log     # 注入调试日志回调（settle 诊断 / 唤醒失效排查）

        # 2026-10-19：安装全局异常钩子——任何未被捕获的异常（含子线程）都写进 daemon.log
        # 并带完整栈，杜绝「进程无声挂掉、日志里看不到栈」的排查黑洞（P0 真机根因）。
        self._install_excepthooks()

        code = 0
        try:
            while True:
                try:
                    if self.stop_path.exists():
                        self.event("收到停止指令，退出")
                        break
                    self.cycle()
                    self.write_status()
                    if self.once:
                        break
                    interval = self._current_poll_interval()
                    reason = self._poll_reason()
                    if reason != self._last_poll_reason:
                        self.log("[poll] interval -> {0:.2f}s (reason={1})".format(
                            interval, reason))
                        self._last_poll_reason = reason
                    # 2026-10-17：用 WinDirWatcher 的变更事件唤醒主循环——有文件写入
                    # 时立即进入下一轮 advance，无需等满 interval，配合 FS_DEBOUNCE_SEC
                    # 去抖把 save->detect 压到 < 0.3s。非 Windows 平台 watcher.wait
                    # 内部退化为普通 time.sleep。
                    # 限频（2026-10-17 补强）：相邻周期强制至少间隔 MIN_CYCLE_SEC，避免
                    # 高频写入把 `_changed` 持续置位导致 run() 忙等（每秒数千轮 cycle ->
                    # 冲刷中转服务 HTTP、拖垮 claim）。实测暴露的 e2e 抖动根因即此。
                    self.watcher.paced_wait(interval)
                except KeyboardInterrupt:
                    self.event("Ctrl+C，退出")
                    break
                except Exception as exc:
                    # 单周期任何异常（含 write_status / paced_wait / cycle 副作用）都只
                    # 记日志、绝不让进程退场——守护进程必须 7x24 存活。
                    self._log_traceback("周期致命异常（已隔离，进程继续）", exc)
        finally:
            self.cleanup()
        return code

    # -- 全局异常兜底（2026-10-19 P0：守护进程必须「打死不退出」）------------------
    def _install_excepthooks(self) -> None:
        import sys, traceback
        dmn = self

        def _hook(etype, evalue, etb):
            try:
                dmn.log_rl("fatal", "未捕获异常（excepthook）: {0}: {1}".format(
                    getattr(etype, "__name__", etype), evalue))
                dmn.log("【FATAL TRACEBACK】\n" + "".join(
                    traceback.format_exception(etype, evalue, etb)))
            except Exception:
                pass

        try:
            sys.excepthook = _hook
        except Exception:
            pass
        try:
            threading.excepthook = lambda args: _hook(
                args.exc_type, args.exc_value, args.exc_traceback)
        except Exception:
            pass

    def _log_traceback(self, label: str, exc: Exception) -> None:
        import traceback
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.log("【{0}】\n{1}".format(label, tb))
        except Exception:
            self.log("{0}: {1!r}".format(label, exc))

    def _status_fresh(self) -> bool:
        """status.json 是否近期有写入（本守护进程仍在正常服务）。"""
        try:
            sp = self.status_path
            if not sp.exists():
                return False
            return (time.time() - sp.stat().st_mtime) <= 60.0
        except Exception:
            return False

    def _acquire_pidfile(self) -> bool:
        """获取单实例锁文件，带自愈。

        2026-10-18 终局修复（真机：僵尸进程卡死，点「开启」无反应）：
          - pid 文件存在但进程**已死** -> 清理残留、直接覆盖（原逻辑已实现）。
          - pid 存在且存活但**不是本守护**（pid 被别的进程复用）-> 清理残留、覆盖。
          - pid 存在且存活、确为本守护，但**无响应**（僵尸 / GIL 饿死 / 卡死：
            status.json 长期未更新，或非刚启动）-> 强制 taskkill 回收后覆盖，
            不再「已有实例在运行」直接退出，解决点开启没反应。
          - pid 存活、本守护、status 新鲜（或刚启动 <20s 还在初始化）-> 确有实例在跑，
            拒绝启动（避免双开），但**不误杀**健康实例。
        """
        if self.pid_path.exists():
            try:
                old = int(self.pid_path.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                old = 0
            if old and old != os.getpid():
                if _pid_alive(old) and _is_our_daemon(old):
                    age = _process_start_age(old)
                    if self._status_fresh() or age < 20.0:
                        # 健康实例（或刚启动还在初始化）：拒绝启动，不误杀
                        _safe_print("[editor_daemon] 已有实例在运行（pid={0}），退出".format(old))
                        self.log("拒绝启动：已有实例 pid={0}".format(old))
                        return False
                    # 存活但确为僵尸/卡死（无响应且非刚启动）：强制回收后覆盖
                    _safe_print("[editor_daemon] pid={0} 存活但无响应，强制回收".format(old))
                    self.log("回收无响应实例 pid={0}".format(old))
                    _force_kill_pid(old)
                else:
                    self.log("清理残留 pid 文件（pid={0} 已不存在 / 非本守护）".format(old))
                # 无论哪种分支，清掉残留 pid 文件以便本次覆盖
                try:
                    self.pid_path.unlink()
                except OSError:
                    pass
            else:
                try:
                    self.pid_path.unlink()
                except OSError:
                    pass
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
        try:
            self.watcher.stop()
        except Exception:
            pass
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

        # 启动后一次性从磁盘恢复上一实例遗留的 ZBrush 投递状态（防重启死锁/丢失）。
        # 必须在 ensure_session 之后（_claim_finish 需要 session）且仅执行一次。
        if not self._zb_rehydrated:
            try:
                self._zb_rehydrate()
            except Exception as exc:
                self.log("[zb-rehydrate] 失败（不影响主流程）: {0!r}".format(exc))
            self._zb_rehydrated = True

        if self._cycle_count % HEARTBEAT_EVERY == 0:
            self.safe("POST", "/sessions/{0}/heartbeat".format(self.session_id), {})

        for app in self.cfg["apps"]:
            resp = self.safe("GET", "/inbox?app={0}&limit=20".format(app))
            if not resp:
                self.log_rl("inbox", "收件箱请求失败（{0}）".format(app))
                continue
            transfers = resp.get("transfers") or []
            if app == "zbrush":
                self.log("[zb-inbox-poll] 服务端返回 zbrush 待收任务数={0}".format(
                    len(transfers)))
            for rec in transfers:
                try:
                    self.handle(rec)
                except Exception as exc:
                    # 2026-10-18 P0：单记录异常隔离，绝不让一个坏任务（如复杂模型
                    # 处理异常）拖垮整轮轮询，否则后续所有任务进不来（ZBrush 接收
                    # 扫描 n=0）。异常即把该传输标记 error 收尾，避免它反复进入
                    # handle 卡死循环。
                    tid = (rec or {}).get("id", "?")
                    self.log("[cycle] handle {0} 抛异常（已隔离）: {1!r}".format(tid, exc))
                    try:
                        self._claim_finish(
                            tid, "error",
                            {"message": "处理异常: {0!r}".format(exc)})
                    except Exception:
                        pass

        self.advance_watches()
        self._zb_advance_pending()
        self._zb_process_outbox()

    # -- 自适应轮询间隔（2026-10-16 P1-A'）------------------------------------
    def _poll_reason(self) -> str:
        """当前轮询档位的原因（用于切换日志，也便于测试断言）。"""
        if any(w.dirty_names for w in self.watches):
            return "settling"
        if any(w.app in FAST_APPS for w in self.watches):
            return "active_watch"
        return "idle"

    def _current_poll_interval(self) -> float:
        """自适应轮询间隔：

        - 任意 watch 正在 settle（有待发布变更、尚未 finalize）-> SETTLE_SAMPLE_SEC
          （settle 子系统需要 150ms 细粒度采样，确保「停笔 ~300ms 内发布」）；
        - 否则，存在活跃 PS/SAI2 监视会话（编辑器开着、尚未稳定变更）->
          FAST_POLL_SEC=0.5（更快感知新保存 -> 回传延迟下降）；
        - 其余（空闲 / 仅 Blender 等慢节奏 app）-> self.interval（默认 2.0s，
          CPU ~0.38% 单核）。

        区间档位相对上次变化时由 run() 打一行 [poll] 日志，便于真机验收。
        """
        if any(w.dirty_names for w in self.watches):
            return SETTLE_SAMPLE_SEC
        if any(w.app in FAST_APPS for w in self.watches):
            return FAST_POLL_SEC
        return float(self.interval)

    # -- 处理一条新传输 -----------------------------------------------------
    # -- 窗口存活探测（仅 Windows）---------------------------------------------
    # 2026-10-16 三轮迭代（用户 P0 追问）：单实例编辑器助手进程秒退（delegated），
    # proc.poll() 不可靠 -> 用窗口枚举。PS 单窗口多标签：顶层标题只显示活动标签，
    # 故必须同时枚举 **子窗口**（SAI2 每文件画布 = MDI 子窗口，标题含文件名）。
    def _enumerate_editor_windows(self, exe):
        """枚举编辑器进程的全部可见窗口（顶层 + 子窗口）。

        返回 (titles, hwnds) 平行列表（titles 已 lower()）。进程不在 -> 两个
        空列表。无需管理员权限；纯 user32 枚举，每次数十毫秒级（见
        tests/bench_win_probe.py 实测），且只在「重发命中复用」时调用一次，
        不在每 2s 轮询周期里跑。
        """
        titles, hwnds = [], []
        if sys.platform != "win32":
            return titles, hwnds
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            pids = self._process_ids_by_exe(exe)
            if not pids:
                return titles, hwnds
            pid_set = set(pids)

            def _collect(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return 1
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in pid_set:
                    return 1
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    t = (buf.value or "").strip()
                    if t:
                        titles.append(t.lower())
                        hwnds.append(hwnd)
                return 1

            WNDENUM = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
            top = []
            def _top(hwnd, _):
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pid_set:
                    top.append(hwnd)
                return 1
            user32.EnumWindows(WNDENUM(_top), 0)
            for hwnd in top:
                _collect(hwnd, None)
                # 子窗口：SAI2 的每文件画布是 MDI 子窗口，EnumWindows 看不到，
                # 必须对每个顶层窗口再枚举其子窗口（PS 浮动文档窗口同理）。
                user32.EnumChildWindows(hwnd, WNDENUM(_collect), 0)
        except Exception:
            pass
        return titles, hwnds

    def _is_file_open_in_editor(self, app, exe, base):
        """三态判定（窗口开着？已关？歧义？），返回 True=视作开着。

        - 标题命中 base（顶层或子窗口）-> 开着（高置信）。
        - 编辑器进程无任何可见窗口 -> 已关（-> 走重开）。
        - 有窗口但标题不含 base -> **多标签歧义**（PS/SAI2 单窗口多标签：顶层
          标题只显示活动标签，后台标签的画布是**隐藏 MDI 子窗口**
          IsWindowVisible=False，被 _collect 跳过枚举不到）。歧义策略按 app 取
          AMBIGUOUS_DEFAULT（2026-10-16 P0 修正，分 app 保守化）：
            · ps   : 默认 **reopen**。用户实机验证 PS 对已打开文件再 Popen 只聚焦
              现有文档、不弹窗不重复打开（「重新发送没反应不弹窗，笔画也在」），
              歧义重开 benign。
            · sai2 : 默认 **keep**（不重开）。SAI2 对「打开已打开文件」会弹「是否
              放弃当前编辑重新打开」确认框（第一轮用户抱怨根因）；后台标签画布
              隐藏导致歧义，若默认重开必重新引入弹窗。故 SAI2 歧义默认 keep
              （只去重不重开、不弹窗）。窗口确实被关再发的极小概率静默丢失风险
              用户已知情接受（见 release notes 第二节）。
          editor_daemon.json 的 "reuse_ambiguous": {"<app>": "reopen"|"keep"}
          可针对单个 app 覆盖默认；但默认值已正确，用户不必手动配。
        - 非 Windows / 探测异常 -> 视作未打开（走重开，数据安全优先）。
        - 测试 seam：AB_EDITOR_OPEN_STATE=open/closed 强制窗口状态（e2e 用）。
        """
        forced = os.environ.get("AB_EDITOR_OPEN_STATE", "")
        if forced == "open":
            return True
        if forced == "closed":
            return False
        t0 = time.monotonic()
        titles, _hwnds = self._enumerate_editor_windows(exe)
        dt_ms = (time.monotonic() - t0) * 1000.0
        base_l = (base or "").lower()
        if base_l and any(base_l in t for t in titles):
            self.log_rl("win_probe",
                        "[win-probe] {0} base={1} -> open ({2:.1f}ms, {3} titles)".format(
                            app, base, dt_ms, len(titles)))
            return True
        if not titles:
            self.log_rl("win_probe",
                        "[win-probe] {0} base={1} -> closed/no-window ({2:.1f}ms)".format(
                            app, base, dt_ms))
            return False
        # 歧义：有窗口但都不是目标文件 -> 按 app 取默认策略（可被 reuse_ambiguous 覆盖）。
        # 返回语义：True=视作「窗口开着」（复用分支只聚焦去重、不重开、不弹窗）；
        #           False=视作「已关」（复用分支重新 Popen 打开）。
        # policy 与返回值的映射（2026-10-16 修正：原 `policy != "keep"` 把语义写反了——
        #   旧代码「keep」反而返回 False 导致重开、「reopen」反而返回 True 不重开，
        #   与 docstring/配置意图完全相反）：
        #   · "keep"   -> 返回 True  -> 不重开（SAI2 用，避免「放弃当前编辑」弹窗）
        #   · "reopen" -> 返回 False -> 重开（PS 用，用户实机验证良性聚焦）
        policy = (self.cfg.get("reuse_ambiguous") or {}).get(app)
        if not policy:
            policy = AMBIGUOUS_DEFAULT.get(app, "reopen")  # 未知 app 默认重开（数据安全优先）
        self.log_rl("win_probe",
                    "[win-probe] {0} base={1} -> ambiguous({2} titles) policy={3} ({4:.1f}ms)".format(
                        app, base, len(titles), policy, dt_ms))
        return policy == "keep"

    def _top_level_windows(self, exe):
        """枚举目标进程的全部**顶层**可见窗口（返回 [(title, hwnd, classname)]）。

        与 _enumerate_editor_windows 的区别：只取顶层窗口（排除 MDI 子画布等），
        并额外返回窗口类名——用于确认 SAI 的「真正主窗口」（排除启动画面 / 隐藏 /
        子窗口），聚焦主窗口才能可靠把整个应用带到前台（要求 二.4）。
        """
        out = []
        if sys.platform != "win32":
            return out
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            pids = self._process_ids_by_exe(exe)
            if not pids:
                return out
            pid_set = set(pids)

            def _top(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return 1
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in pid_set:
                    return 1
                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = (buf.value or "").strip()
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                cls = (class_buf.value or "").strip()
                # 排除无标题伪窗口（启动画面/StaticText 之类通常无标题）
                if title or cls:
                    out.append((title, hwnd, cls))
                return 1

            WNDENUM = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUM(_top), 0)
        except Exception:
            pass
        return out

    def _is_elevated(self, pid):
        """返回 True/False/None：进程是否以管理员身份运行（UIPI 需要同权限跨进程前台）。"""
        if sys.platform != "win32" or not pid:
            return None
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            advapi32 = ctypes.windll.advapi32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            h = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not h:
                return None
            try:
                t = wintypes.HANDLE(0)
                if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(t)):  # TOKEN_QUERY
                    return None
                try:
                    elev = wintypes.DWORD(0)
                    ret = wintypes.DWORD(0)
                    # 20 = TokenElevation
                    if advapi32.GetTokenInformation(t, 20, ctypes.byref(elev), 4,
                                                   ctypes.byref(ret)):
                        return bool(elev.value)
                    return None
                finally:
                    kernel32.CloseHandle(t)
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return None

    def _pid_of(self, exe):
        # TODO(2026-10-26 P1 #2, 非阻塞): 当前按进程名匹配；PS 退出慢时
        # _process_ids_by_exe 在进程已无可见窗口后仍返回非 None 达 ~16s（见
        # advance_watches 委托探测），导致委托解除滞后。可优化为「按窗口枚举判断：
        # exe 无可见主窗口即视为退出」以缩短委托解除延迟。验收不依赖此项。
        pids = self._process_ids_by_exe(exe or "")
        return pids[0] if pids else None

    def _log_stealer_if_any(self, fg, target):
        """若最终前台不是目标窗口，记录抢走焦点的进程名与标题（要求 二.2 探针）。"""
        if not fg or fg == target:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            length = user32.GetWindowTextLengthW(fg)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(fg, buf, length + 1)
                title = (buf.value or "").strip()
            _pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(fg, ctypes.byref(_pid))
            self.log(
                        "[focus] 前台被其它窗口抢走：hwnd={0} pid={1} title={2!r}".format(
                            fg, _pid.value, title[:60]))
        except Exception:
            pass

    def _focus_editor_window(self, app, exe, base):
        """把已打开该文件的编辑器窗口带到前台（尽力而为，带完整探针日志）。

        后台进程前台锁（Windows 前台锁 / UIPI）：daemon 是后台进程，裸
        SetForegroundWindow 通常被拒。回退链（每次都打探针，失败不静默吞）：
           ① SW_RESTORE -> 裸 SetForegroundWindow（立即复查 + 500ms 复查）
           ② 失败/被抢 -> AttachThreadInput（借前台线程输入权限抢焦点）
           ③ 仍失败 -> Alt 键技巧（按下 Alt 期间调用 SetForegroundWindow，绕过前台锁
              的经典手法）
           ④ 仍失败 -> 任务栏闪烁（FlashWindowEx）提示用户手动点，并明确日志「聚焦失败」。
        任一步成功即止；全部失败也不影响交付（文件已打开，只是没抢到焦点）。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            base_l = (base or "").lower()
            self.log( "[focus] 目标 exe={0} base={1}".format(exe, base))
            # ④ 枚举目标进程顶层可见窗口，确认真正主窗口（排除启动画面/隐藏/子窗口）
            tops = self._top_level_windows(exe)
            for title, hwnd, cls in tops:
                self.log(
                            "[focus] 候选主窗口 hwnd={0} class={1} title={2!r}".format(
                                hwnd, cls, title[:60]))
            candidates = [(t, h, c) for (t, h, c) in tops if base_l and base_l in t.lower()] \
                if base_l else list(tops)
            if not candidates:
                # 2026-10-26 P0：PS 多标签场景下顶层窗口标题是 'Adobe Photoshop 2023'
                # 不含文件名（base），原逻辑 candidates 为空直接 return、永不执行
                # SetForegroundWindow（用户实机测得 PS「被拉前台」只是 PS 自己激活，非
                # daemon 行为）。此处置 fallback：标题不含 base 时取第一个顶层窗口
                # （PS 主窗口即 tops[0]），使其真正被聚焦。SAI2 顶层标题含文件名、原
                # 即命中，不会走到此分支，故不受影响。
                if tops:
                    self.log("[focus] {0} 的顶层标题不含 base={1}，fallback 到第一个顶层窗口 hwnd={2} title={3!r}".format(
                        app, base, tops[0][1], tops[0][0][:60]))
                    candidates = [tops[0]]
                else:
                    self.log(
                                "[focus] 未找到 {0} 的可见主窗口（base={1}），放弃聚焦".format(app, base))
                    return
            h = candidates[0][1]
            target_title = candidates[0][0]
            # 权限一致性（UIPI）：daemon 与目标进程是否同为管理员，跨权限会被拦
            daemon_admin = self._is_elevated(os.getpid())
            target_admin = self._is_elevated(self._pid_of(exe))
            self.log(
                        "[focus] 权限 daemon={0} target={1}（跨权限会被 UIPI 拦）".format(
                            daemon_admin, target_admin))
            SW_RESTORE = 9
            try:
                user32.ShowWindow(h, SW_RESTORE)
            except Exception:
                pass
            fg_before = user32.GetForegroundWindow()
            self.log(
                        "[focus] 调用前 GetForegroundWindow={0} 目标 hwnd={1} title={2!r}".format(
                            fg_before, h, target_title[:60]))
            # ① 裸 SetForegroundWindow
            try:
                r1 = user32.SetForegroundWindow(h)
            except Exception:
                r1 = 0
            fg_after = user32.GetForegroundWindow()
            self.log(
                        "[focus] ①裸 SetForegroundWindow 返回={0} 立即 GetForegroundWindow={1}".format(
                            r1, fg_after))
            if fg_after == h:
                self.log( "[focus] ①成功：前台已是目标窗口")
                return
            self._log_stealer_if_any(fg_after, h)
            time.sleep(0.5)
            fg_500 = user32.GetForegroundWindow()
            self.log( "[focus] ①+500ms GetForegroundWindow={0}".format(fg_500))
            if fg_500 == h:
                self.log( "[focus] ①成功（延迟生效）")
                return
            # ② AttachThreadInput：借前台线程的输入权限抢焦点（经典做法）
            try:
                our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                fg = user32.GetForegroundWindow()
                fg_tid = 0
                if fg:
                    _pid = wintypes.DWORD(0)
                    fg_tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(_pid))
                _pid2 = wintypes.DWORD(0)
                tgt_tid = user32.GetWindowThreadProcessId(h, ctypes.byref(_pid2))
                attached = []
                if fg_tid and fg_tid != our_tid:
                    user32.AttachThreadInput(our_tid, fg_tid, True)
                    attached.append((our_tid, fg_tid))
                if tgt_tid and tgt_tid != our_tid and (our_tid, tgt_tid) not in attached:
                    user32.AttachThreadInput(our_tid, tgt_tid, True)
                    attached.append((our_tid, tgt_tid))
                try:
                    user32.BringWindowToTop(h)
                    user32.SetForegroundWindow(h)
                finally:
                    for a, b in attached:
                        try:
                            user32.AttachThreadInput(a, b, False)
                        except Exception:
                            pass
                if user32.GetForegroundWindow() == h:
                    self.log( "[focus] ②AttachThreadInput 抢焦点成功")
                    return
                self._log_stealer_if_any(user32.GetForegroundWindow(), h)
            except Exception as exc:
                self.log( "[focus] ②AttachThreadInput 异常: {0!r}".format(exc))
            # ③ Alt 键技巧：按下 Alt 期间调用 SetForegroundWindow（绕过前台锁经典手法）
            try:
                VK_MENU = 0x12
                user32.keybd_event(VK_MENU, 0, 0, 0)        # Alt down
                try:
                    user32.SetForegroundWindow(h)
                finally:
                    user32.keybd_event(VK_MENU, 0, 0x0002, 0)  # Alt up
                if user32.GetForegroundWindow() == h:
                    self.log( "[focus] ③Alt 键技巧成功")
                    return
                self._log_stealer_if_any(user32.GetForegroundWindow(), h)
            except Exception as exc:
                self.log( "[focus] ③Alt 键技巧异常: {0!r}".format(exc))
            # ④ 兜底：任务栏闪烁 + 明确「聚焦失败」日志（禁止静默吞）
            try:
                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.UINT),
                                ("hwnd", wintypes.HWND),
                                ("dwFlags", wintypes.DWORD),
                                ("uCount", wintypes.UINT),
                                ("dwTimeout", wintypes.DWORD)]
                FLASHW_ALL = 3
                f = FLASHWINFO(ctypes.sizeof(FLASHWINFO), h, FLASHW_ALL, 5, 0)
                user32.FlashWindowEx(ctypes.byref(f))
            except Exception:
                pass
            self.log(
                        "[focus] 聚焦失败：①裸SetForegroundWindow ②AttachThreadInput "
                        "③Alt键 均无效，已任务栏闪烁提示手动点击（app={0}）".format(app))
        except Exception as exc:
            self.log( "[focus] _focus_editor_window 异常: {0!r}".format(exc))

    def _refresh_route_focus(self) -> None:
        """读取流转设置 flow_settings.json 的逐路由 auto_focus，缓存到 self.route_focus。

        根因（2026-10-26 P0-2）：此前用 encoding="utf-8" 读，文件带 BOM（EF BB BF）时
        json.loads 抛 JSONDecodeError: Unexpected UTF-8 BOM，被 except 吞掉 -> 统一打
        「未找到」，路由 auto_focus 永远空 -> 用户勾了自动聚焦对守护无效。现改用
        encoding="utf-8-sig"（自动去 BOM），并区分「文件不存在 / 解析失败 / 权限不足」
        三类错误分别打日志，且每次尝试都打印「路径 / exists / 读取结果 / 命中路由数」，
        避免再靠推测。
        flow_settings.json 与 payload_dir 同目录（<runtime>/flow_settings.json）。
        """
        if not self.payload_dir:
            return
        # 多候选路径探测并打日志，把真实加载路径与命中路由打印出来。
        base = self.payload_dir
        candidates = [
            os.path.join(os.path.dirname(base), "flow_settings.json"),
            os.path.join(base, "flow_settings.json"),
            os.path.join(os.path.dirname(os.path.dirname(base)), "config",
                         "flow_settings.json"),
        ]
        # 2026-10-26 P1：flow_settings 可能每轮被查询（handle 路径）。mtime 未变则跳过重读
        # 与「解析成功」日志，避免每 0.5s 刷爆 daemon.log（实测已 7.3MB）。仅在内容变化时重读重打。
        probe = next((c for c in candidates if os.path.isfile(c)), None)
        if probe:
            try:
                _mt = os.path.getmtime(probe)
            except OSError:
                _mt = None
            if (probe == getattr(self, "_rf_path", None)
                    and _mt == getattr(self, "_rf_mtime", None)
                    and getattr(self, "_rf_loaded", False)):
                return
        data = None
        used = ""
        for c in candidates:
            exists = os.path.isfile(c)
            if not exists:
                self.log("[focus] flow_settings 候选不存在: {0}".format(c))
                continue
            # 2026-10-26 P0-2：utf-8-sig 自动去 BOM；分错误类型打日志，不再笼统「未找到」。
            try:
                raw = Path(c).read_text(encoding="utf-8-sig")
            except PermissionError:
                self.log("[focus] flow_settings 权限不足（跳过）: {0}".format(c))
                continue
            except Exception as exc:
                self.log("[focus] flow_settings 读取异常({0}): {1}".format(type(exc).__name__, c))
                continue
            try:
                data = json.loads(raw)
                used = c
                self.log("[focus] flow_settings 解析成功: {0}".format(c))
                break
            except json.JSONDecodeError as exc:
                # BOM 已被 utf-8-sig 处理；此处多为内容损坏，明确报错避免静默误判。
                self.log("[focus] flow_settings 解析失败(JSONDecodeError): {0} ({1})".format(c, exc))
                continue
            except Exception as exc:
                self.log("[focus] flow_settings 解析异常({0}): {1}".format(type(exc).__name__, c))
                continue
        if data is None:
            self._rf_loaded = False
            self._rf_path = None
            self._rf_mtime = None
            self.log("[focus] flow_settings.json 未找到/全部解析失败（候选: {0}），路由 auto_focus 留空".format(
                "; ".join(candidates)))
            return
        rf = {}
        routes = data.get("routes") or {}
        if not isinstance(routes, dict):
            # 兼容 routes 为列表（每项含 key/from/to）的写法
            for rv in (routes or []):
                if isinstance(rv, dict) and rv.get("auto_focus"):
                    k = rv.get("key") or "{0}>{1}".format(rv.get("from", ""), rv.get("to", ""))
                    rf[k] = True
                    if ">" in k:
                        rf[">" + k.split(">", 1)[1].strip()] = True
            self.log("[focus] flow_settings.json 已加载（{0}），但 routes 非字典，按列表解析得 {1} 条".format(
                used, len(rf)))
            self.route_focus = rf
            self._rf_loaded = True
            self._rf_path = used
            try:
                self._rf_mtime = os.path.getmtime(used)
            except OSError:
                self._rf_mtime = None
            return
        for key, rv in routes.items():
            if isinstance(rv, dict) and rv.get("auto_focus"):
                rf[key] = True
                if ">" in key:               # 同时登记 ">{dst}" 维度，匹配任意来源
                    rf[">" + key.split(">", 1)[1].strip()] = True
        self.log("[focus] flow_settings.json 已加载（{0}），路由 auto_focus 命中 {1} 条: {2}".format(
            used, len(rf), ", ".join(sorted(rf.keys()))))
        self.route_focus = rf
        self._rf_loaded = True
        self._rf_path = used
        try:
            self._rf_mtime = os.path.getmtime(used)
        except OSError:
            self._rf_mtime = None

    def _route_focus_for(self, from_app: str, to_app: str) -> bool:
        # 2026-10-26 P1：改用 _rf_loaded 标志判定是否已加载，而非 `not self.route_focus`。
        # 否则当 flow_settings 里没有任何 auto_focus 路由时 rf 为空字典，route_focus 仍是
        # falsy，导致每轮都重读重打「解析成功」（实测 daemon.log 每 0.5s 一条、已 7.3MB）。
        if not getattr(self, "_rf_loaded", False) and self.payload_dir:
            self._refresh_route_focus()
        key = "{0}>{1}".format(from_app, to_app)
        return bool(self.route_focus.get(key) or self.route_focus.get(">" + to_app))

    def _reuse_focus_if_allowed(self, app, exe, base, want_focus=False) -> bool:
        """复用「窗口已开着」分支时，按 app 决定是否把窗口带到前台。

        返回 True=已聚焦，False=未碰窗口（noop）。

        - focus_on_open（2026-10-17 新增全局开关）：开启时**无视**下方 per-app 策略，
          一律强制聚焦（用户已接受 SAI2 可能弹「放弃当前编辑重新打开」确认框的代价）。
        - ps  ：保持既有 SetForegroundWindow 聚焦逻辑（用户实测 PS 多标签正常）。
        - sai2：noop。SAI2 把「外部进程聚焦到已打开文件」当成重开请求并弹
          「放弃当前编辑重新打开」确认框（2026-10-16 实机实证：复用+仅聚焦也弹窗，
          Popen 不是唯一触发器）。无实测证据表明 SetForegroundWindow /
          AttachThreadInput / FlashWindowEx 哪个在 SAI2 里不触发弹窗前，一律不碰窗口。
          只更新 last_event 反馈，由用户手动切窗口。
        """
        if want_focus or self.cfg.get("focus_on_open"):
            self._focus_editor_window(app, exe, base)
            return True
        policy = REUSE_FOCUS_ON_OPEN.get(app, "focus")
        if policy == "noop":
            self.log(
                        "[focus] {0} 复用分支不碰窗口（noop，避免外部聚焦触发 SAI2 确认框）".format(app))
            return False
        self._focus_editor_window(app, exe, base)
        return True

    def _focus_after_open(self, app, exe, base, want_focus=False):
        """首开编辑器后，若开启 focus_on_open，则在后台短重试把窗口拉前台。

        首开瞬间窗口尚未就绪，故在后台线程里重试几次（~1.5s）等窗口出现再聚焦，
        不阻塞 handle() 主流程。仅 focus_on_open 开启时生效。
        """
        if not (want_focus or self.cfg.get("focus_on_open")):
            return
        if sys.platform != "win32":
            return

        def _run():
            for _ in range(6):
                try:
                    self._focus_editor_window(app, exe, base)
                except Exception:
                    pass
                time.sleep(0.25)
        threading.Thread(target=_run, daemon=True).start()

    def _process_ids_by_exe(self, exe):
        name = os.path.basename(exe).lower()
        pids = []
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            TH32CS_SNAPPROCESS = 0x00000002
            h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            INVALID = wintypes.HANDLE(-1).value
            if h_snap == INVALID:
                return pids
            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.CHAR * 260),
                ]
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(h_snap, ctypes.byref(entry)):
                while True:
                    try:
                        exe_name = entry.szExeFile.decode("mbcs")
                    except Exception:
                        exe_name = ""
                    if exe_name.lower() == name:
                        pids.append(entry.th32ProcessID)
                    if not kernel32.Process32Next(h_snap, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(h_snap)
        except Exception:
            pass
        return pids

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
        from_app = _norm_app(str(frm.get("app", "")).lower())
        if not from_app:
            self._claim_finish(tid, "error", {
                "message": "发送端未标注 from.app，无法回传"})
            return
        if from_app == app:
            self._claim_finish(tid, "error", {
                "message": "回环：编辑器不接受来源为自己的传输"})
            return
        # 2026-10-20 P1：流转设置「自动聚焦」写的是 per-route auto_focus；与全局
        # focus_on_open 取或，任一为真即触发聚焦（含 SAI2 强制 SetForegroundWindow）。
        fo = bool(self.cfg.get("focus_on_open"))
        route_focus = self._route_focus_for(from_app, app)
        want_focus = fo or route_focus
        # 2026-10-26 真机排查（用户质询 SAI2 无聚焦日志）：聚焦决策入口无条件打点，
        # 区分「根本没触发（want_focus=False）」与「触发了但 noop（SAI2 复用分支）」。
        self.log(
                    "[focus] decision want_focus={0} focus_on_open={1} "
                    "route_auto_focus={2} from={3} to={4}".format(
                        want_focus, fo, route_focus, from_app, app))

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

        # 分段计时（2026-10-14）：server 建传输 -> daemon 轮询并 claim 的间隔
        # （主要受 poll_interval 与周期耗时影响；SAI2 回传慢定位用）
        self.log_timing_claim(tid, rec)

        # P0 2026-10-14 / P0-1 2026-10-15（再返工，覆盖回退分支）/ P1 2026-10-15：
        # 「同一逻辑文件只开一个窗口 + 内容更新即聚焦」的统一方案。
        # 根因：server 按 tid 分 payload 目录，同一资源每次发送都是新目录、新物理
        # 路径；若直接 Popen 每次的 per-tid 路径，单实例编辑器（PS/SAI2 把它当
        # 不同文件）会新开同名标签页——用户实测「堆 5 窗口」，且 daemon 只监听
        # 最新 watch -> 旧窗口回传永久丢失。
        # 修复：把传入 payload 拷到「稳定打开路径」state_dir/open/<app>/<基名>
        # （与 tid 无关，只认逻辑文件名），Popen 这个稳定路径。单实例编辑器对
        # 同一路径会聚焦已有标签页（不新开窗口）。去重键 =
        # (to_app, from_app, 资产标识)，资产标识优先 asset_paths（UE，稳定且不同
        # 目录天然区分），回退只认主文件名（Blender/Maya->PS/SAI2 的潜在形态，
        # 但当前 Blender/Maya 贴图发送尚未实现，故回退分支暂无真实流量）。**回退键
        # 不再含内容 sha256**：「我改一版发一版」= 同名
        # 不同内容，若键含 sha256 则每次都不同键 -> 不复用 -> 又堆窗口，正是
        # P0-1 阻塞级没修到的分支。改为「同名即同一窗口」。
        # 复用语义（2026-10-15 e2e 实机回归 + 实机复验三轮迭代）：命中已有同名窗口时，
        # 先判断文件窗口是否还真的开着（见 _is_file_open_in_editor：Windows 窗口
        # 枚举 + 标题匹配，SAI2/PS 都把文件名写进窗口标题；单实例助手进程秒退使
        # proc.poll() 不可靠，故不用进程判定）：
        #   - 开着 -> 去重 superseded_by=首窗口，绝不重开；是否把窗口带到前台按 app
        #     分策略（见 REUSE_FOCUS_ON_OPEN）：PS 聚焦、SAI2 不碰窗口——因为 SAI2
        #     把「外部进程聚焦到已打开文件」当成重开请求并弹「放弃当前编辑重新打开」
        #     确认框（2026-10-16 实机实证：复用+仅聚焦也弹窗，Popen 不是唯一触发器）。
        #     稳定文件由编辑器独占，daemon 不反向写回、不动检测状态，旧窗口回传不丢。
        #   - 已关（用户关了文件窗口但 app 还开着）-> 重新 Popen 打开（此时 SAI2
        #     不弹确认框，因为文件确实没开），不丢发送；复用 existing watch 并重置
        #     检测基线到新打开内容，避免把刚拷入文件误判成一次保存。
        # 回退分支（无 asset_paths，Blender/Maya 发 PS/SAI2 的潜在形态）目前无真实
        # 流量（Blender/Maya 贴图发送尚未实现）。**前置条件**：Blender/Maya 贴图
        # 发送功能上线前，插件侧必须在发送的 options 带上 asset_paths（或改回退键），
        # 否则不同目录同名资产会被判成同一窗口（串台）——见代码 TODO 与 release
        # notes 第二节。UE 发送端带 asset_paths -> 不受影响。
        dup_key = (app, from_app,
                   Watch.asset_identity(rec.get("options") or {}, files))

        # ① 同名逻辑文件已（曾）被打开 -> 复用窗口：先探测窗口是否还开着，
        #    开着只聚焦去重、已关则重新打开（详见下方复用分支内注释）。
        existing = None
        for w in self.watches:
            if w.dedup_key() == dup_key:
                existing = w
                break
        if existing is not None:
            # 复用分支（同名逻辑文件已打开过）：必须区分「窗口还开着」与「已关」，
            # 否则「只去重不重开」会在「用户关了文件窗口但 app 还开着」时静默丢发送。
            # 2026-10-15 实机复验迭代（用户 P0 追问）：之前「复用完全不 Popen」在
            # 600s 空闲窗口内（_should_expire 仅靠空闲时长 + 进程存活判定；单实例
            # 助手进程已秒退 delegated，故窗口关了仍不超时）会把「关窗再发」判成复用
            # -> 不重开 -> 发送静默丢失。修复：用 Windows 窗口枚举判断文件窗口是否
            # 真的开着（SAI2/PS 都把文件名放进窗口标题，可靠、无需管理员）：
            #   - 开着 -> 去重 superseded，绝不重开；但「把窗口带到前台」按 app 分策略
            #     （REUSE_FOCUS_ON_OPEN）：SAI2 聚焦会触发「放弃当前编辑重新打开」确认框
            #     （实机实证），故 SAI2 复用分支完全不碰窗口（不聚焦、不 FlashWindow），
            #     只更新 last_event 反馈，用户手动切窗口；PS 保持聚焦（实测多标签正常）。
            #   - 已关 -> 重新 Popen 打开（此时 SAI2 不会弹确认框，因为文件确实没开），
            #     不丢发送；
            #   - 探测失败（非 Windows / API 异常）默认「未打开」-> 走重新打开（数据
            #     安全优先，最坏偶发弹窗；绝不让发送静默丢失）。
            base = os.path.basename(existing.files[0]) if existing.files else ""
            if self._is_file_open_in_editor(app, exe, base):
                # 窗口还开着：去重 + 反馈；是否把窗口带到前台按 app 分策略
                # （SAI2 聚焦会触发确认框，故 noop；PS 保持聚焦）。返回是否真的聚焦了。
                focused = self._reuse_focus_if_allowed(app, exe, base, want_focus)
                self.log("[watch-dedup] reuse keep-open tid={0} keep={1} key={2} focus={3}".format(
                    tid, existing.tid, dup_key[2], focused))
                # 仅刷新轻量元数据，不动检测关键字段
                existing.asset_name = rec.get("asset_name", "")
                existing.orig_options = rec.get("options") or {}
                existing.identity = Watch.asset_identity(existing.orig_options, existing.files)
                if focused:
                    fb = "已聚焦窗口，未重开、未覆盖编辑"
                else:
                    fb = "未碰窗口（避免弹窗，请手动切换到该编辑器）"
                self.event("已在 {0} 复用已打开的 {1}（{2}）".format(
                    APP_LABELS.get(app, app), base, fb))
                self._claim_finish(tid, "done", {
                    "message": "同一文件已在该编辑器打开，复用窗口（{0}）".format(fb),
                    "superseded_by": existing.tid,
                })
                return
            # 窗口已关（文件窗口被用户关掉，app 可能还开着）：重新打开，不丢发送
            staged = self._stage_for_open(app, files, dup_key)
            if not staged:
                self._claim_finish(tid, "error", {
                    "message": "稳定打开路径拷贝失败（无可用文件）"})
                return
            try:
                proc = subprocess.Popen([exe] + staged, close_fds=True)
            except Exception as exc:
                self._claim_finish(tid, "error", {
                    "message": "启动 {0} 失败: {1}".format(APP_LABELS.get(app, app), exc)})
                return
            # 复用 existing watch（不追加新 watch）：重置检测基线到「新打开内容」，
            # 避免把刚拷入的稳定路径当成一次保存而误回传；保留 tid / 回传目标。
            snap = existing.snapshot()
            existing.baseline = snap
            existing.last_snap = snap
            existing.dirty_names = []
            existing.dirty_ts = 0.0
            existing._prev_write_ts = 0.0
            existing._last_write_gap = 0.0
            existing.proc = proc
            existing.app_exe = exe
            existing.opened_at = now_iso()
            existing.opened_mono = time.monotonic()
            existing.proc_exited_at = None
            existing.delegated = False
            existing.last_activity_mono = time.monotonic()
            existing.asset_name = rec.get("asset_name", "")
            existing.orig_options = rec.get("options") or {}
            existing.identity = Watch.asset_identity(existing.orig_options, existing.files)
            self.log("[watch-reopen] reuse reopen tid={0} keep={1} key={2}".format(
                tid, existing.tid, dup_key[2]))
            self.event("已在 {0} 重新打开 {1}（上次窗口已关闭）".format(
                APP_LABELS.get(app, app), base))
            # 2026-10-26 P0-3：reopen 路径此前直接 return，漏掉 _focus_after_open，
            # 导致「关窗再发」场景下窗口重新打开后不被拉前台（PS 实测零 [focus] 日志）。
            # 现在与首开路径（L2342-2346）一致：reopen 后若 want_focus，后台把窗口拉前台。
            # want_focus 来自本方法 L2174（fo or route_focus），reopen 分支前已算出，可直接用。
            try:
                reopen_base = os.path.basename(files[0]) if files else ""
                self._focus_after_open(app, exe, reopen_base, want_focus)
            except Exception:
                pass
            self._claim_finish(tid, "done", {
                "message": "同一文件窗口已关闭，已在编辑器重新打开"})
            return

        # ② 首次打开：拷到稳定路径并 Popen
        staged = self._stage_for_open(app, files, dup_key)
        if not staged:
            self._claim_finish(tid, "error", {
                "message": "稳定打开路径拷贝失败（无可用文件）"})
            return
        try:
            proc = subprocess.Popen([exe] + staged, close_fds=True)
        except Exception as exc:
            self._claim_finish(tid, "error", {
                "message": "启动 {0} 失败: {1}".format(APP_LABELS.get(app, app), exc)})
            return

        w = Watch(rec, staged, proc, self.watcher, app_exe=exe)
        w.log = self.log
        self.watches.append(w)
        if w.dir:
            self.watcher.add_dir(w.dir)
        # 2026-10-17 新增（用户需求 focus_on_open）：首开编辑器后，若开启开关，
        # 后台把窗口拉前台（仅该开关开启时生效；默认不碰窗口，保护 SAI2 不弹确认框）。
        try:
            base = os.path.basename(staged[0]) if staged else ""
            self._focus_after_open(app, exe, base, want_focus)
        except Exception:
            pass
        self.handled += 1
        # 诊断（2026-10-13 夜）：回传链路排查——打开会话的完整映射关系
        self.log("[watch-open] tid={0} editor={1} return_to={2} dir={3} files={4} "
                 "orig_options={5}".format(
                     tid, app, from_app, os.path.dirname(staged[0]) if staged else "",
                     [os.path.basename(f) for f in staged],
                     {k: v for k, v in (rec.get("options") or {}).items()
                      if k in ("asset_paths", "source_files", "source_file")}))
        self.event("已在 {0} 打开 {1}（等待保存回传 {2}）".format(
            APP_LABELS.get(app, app),
            "、".join(os.path.basename(f) for f in staged),
            APP_LABELS.get(from_app, from_app)))

    # -- 稳定打开路径：把 per-tid payload 拷到与 tid 无关的「逻辑文件名」路径 ----
    @staticmethod
    def _file_sha256(path: str) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except Exception:
            return ""
        return h.hexdigest()

    def _stage_for_open(self, app: str, files, dup_key, force: bool = False) -> list:
        """把传入 payload 拷到「稳定打开路径」state_dir/open/<app>/<slot>/<基名>。

        与 server 的 per-tid 目录解耦：只认逻辑文件名，保证同名文件永远落到
        同一个物理路径，单实例编辑器（PS/SAI2）据此聚焦已有窗口而非新开同名
        标签页（P0-1 阻塞级根因：每次 payload 目录不同 -> 对 app 是 N 个不同
        路径的同名文件 -> 堆窗口；daemon 只监听最新 watch -> 旧窗口回传丢失）。
        重发同名文件时覆盖同一稳定路径 + 重新 Popen -> 聚焦/重载，不新开窗口。

        slot 由 dedup_key 决定：同逻辑文件 -> 同 slot -> 同稳定路径（复用窗口）；
        不同逻辑文件（如 UE 同文件名不同 asset_paths）-> 不同 slot -> 不同路径
        （不串台、不碰撞）。回退分支无 asset_paths 时只认主文件名，slot 随文件名，
        故「我改一版发一版」（同名不同内容）也复用同一窗口。

        注意：本方法只在「首次打开」分支调用（把 payload 落盘到稳定路径）。
        复用分支不再调用本方法——命中已有窗口时只 Popen 同稳定路径聚焦、绝不
        把传入 payload 拷回，避免回写破坏编辑器正在保存的内容（见 handle 复用
        分支注释）。dst 已存在且内容一致时跳过拷贝是首开幂等优化（同一 slot
        被新传输重新首开时不会覆盖在途文件）。
        """
        slot = hashlib.md5(repr(dup_key).encode("utf-8")).hexdigest()[:16]
        d = self.state_dir / "open" / app / slot
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        staged = []
        for f in files:
            dst = d / os.path.basename(f)
            if not force and dst.is_file():
                try:
                    if (os.path.getsize(dst) == os.path.getsize(f)
                            and self._file_sha256(str(dst)) == self._file_sha256(f)):
                        # 内容一致：跳过拷贝，只复用已开窗口（避免 mtime 抖动）
                        staged.append(str(dst))
                        continue
                except Exception:
                    pass
            try:
                shutil.copy2(f, str(dst))
                staged.append(str(dst))
            except Exception as exc:
                self.log("拷到稳定路径失败 {0} -> {1}: {2}".format(f, dst, exc))
        return staged

    # -- ZBrush：任务文件握手 / 一次性 -script 兜底 / 热文件夹回传 -----------
    def handle_zbrush(self, record: dict, kind: str) -> None:
        tid = record.get("id", "")
        self.log("[handle_zbrush] ENTER tid={0} kind={1} payload_dir={2}".format(
            tid, kind, self.payload_dir))
        # 去重（2026-10-18 终局修复）：同一 tid 绝不允许重复投递 inbox。claim 虽把
        # 服务端状态置 claimed（/inbox 不再返回），但防御性再加内存去重，杜绝任何
        # 路径下的重复写文件 / 重复 append zb_pending（真机曾 1 秒内同任务多条投递记录）。
        if tid and tid in self._zb_delivered:
            self.log_rl("zb-dedup",
                        "handle_zbrush 去重跳过已投递 tid={0}".format(tid))
            return
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
        from_app = _norm_app(str(frm.get("app", "")).lower())
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
        self.log("[handle_zbrush] claim 成功 tid={0} files={1}".format(
            tid, len(rec.get("_files_abs") or rec.get("files") or [])))
        claim_files = [f for f in (rec.get("_files_abs") or []) if os.path.isfile(f)]
        if claim_files:
            files = claim_files

        if not self.payload_dir:
            self._claim_finish(tid, "error", {
                "message": "拿不到负载目录，无法投递任务"})
            return

        # 投递决策（2026-10-16 用户实机纠正）：ZBrush 是**单实例**，已运行时
        # **绝不**再 spawn 新实例（会超时/冲突，见 zbrush_process_running 注释与
        # 用户实机铁证：一次性 -script 拉起超时，因为本机 ZBrush 已在跑）。统一走
        # 「文件握手 / IPC」——把 <tid>.json 任务文件放进 inbox，用户在 ZBrush 里
        # 点「导入待收模型（inbox 里有任务时点）」即可消费。桥接心跳（GIL 饿死导致
        # 常不新鲜）不再作为投递门槛，**进程是否在运行**才是可靠信号。
        running = zbrush_process_running()
        if running:
            self.event("检测到 ZBrush 已在运行（进程扫描，非心跳）：走 inbox 文件握手，"
                       "不拉起新实例")
        else:
            self.event("未检测到 ZBrush 进程：模型先投递到 inbox（启动 ZBrush 后点"
                       "「导入待收模型」即可接收）")
        try:
            inbox_dir = zb_dirs(self.payload_dir)[0]
            self.log("[handle_zbrush] 准备投递 tid={0} 文件数={1} inbox_dir={2}".format(
                tid, len(files), inbox_dir))
            task = zb_write_task(self.payload_dir, rec, files)
            if task:
                self.log("[handle_zbrush] 已写任务文件 path={0} exists={1}".format(
                    task, os.path.isfile(task)))
                self.zb_pending.append({
                    "tid": tid, "files": files, "mode": "handshake",
                    "deadline": time.monotonic() + ZB_IMPORT_TIMEOUT_SEC,
                    "task": task, "from_app": from_app,
                    "asset_name": rec.get("asset_name", ""),
                })
                self._zb_delivered.add(tid)   # 去重：标记已投递，杜绝重复写 inbox
                self.handled += 1
                self.event("已投递模型任务给 ZBrush（inbox 握手）：{0}（请在 ZBrush 点"
                           "「导入待收模型」接收）".format(
                               "、".join(os.path.basename(f) for f in files)))
                return

            # 兜底：inbox 不可写 -> 落盘 + D3 托盘通知（用户手动 Tool:Load）
            notify_path = zb_write_notify(self.state_dir, rec, files)
            self.zb_pending.append({
                "tid": tid, "files": files, "mode": "manual",
                "deadline": time.monotonic() + ZB_IMPORT_TIMEOUT_SEC,
                "task": notify_path, "from_app": from_app,
                "asset_name": rec.get("asset_name", ""),
            })
            self._zb_delivered.add(tid)   # 去重：标记已投递，杜绝重复落盘
            self.handled += 1
            self.event("ZBrush inbox 不可用，模型已落盘：{0}（托盘已提示，"
                       "可在 ZBrush 里 Tool:Load 手动加载）".format(
                           "、".join(os.path.basename(f) for f in files)))
        except Exception as exc:
            # 2026-10-18 P0：投递阶段异常（如 zb_write_task 失败）必须兜底清理，
            # 否则已 claim 的传输会滞留、且反复进入 handle 卡死整轮轮询。
            self.log("[handle_zbrush] tid={0} 投递异常（兜底清理）: {1!r}".format(tid, exc))
            try:
                inbox, _, _ = zb_dirs(self.payload_dir)
                if inbox:
                    try:
                        os.remove(os.path.join(inbox, tid + ".json"))
                    except OSError:
                        pass
                self._claim_finish(
                    tid, "error",
                    {"message": "ZBrush 投递异常: {0!r}".format(exc)})
            except Exception:
                pass
            self._zb_delivered.discard(tid)   # 投递失败：允许下次轮询重试
            self.zb_pending[:] = [p for p in self.zb_pending if p.get("tid") != tid]

    def _zb_rehydrate(self) -> None:
        """启动/接管时从磁盘恢复上一实例遗留的 ZBrush 投递状态（防重启死锁/丢失）。

        真机场景（用户 2026-10-18 复现）：Bridge（本守护）完整重启，但 ZBrush 没重启
        -> 上一实例已写进 inbox 的任务文件（<tid>.json）或 ZBrush 已导入但回执还没被
        读走的证据（done/<tid>.done.json）都还在磁盘上，而内存里的 zb_pending /
        _zb_delivered 已随进程销毁。若不恢复：
          ① 已投递但 ZBrush 还没点的任务文件 -> 本守护再也不会读它（zb_pending 空），
             只能赌 ZBrush 自己 import_pending 扫盘兜底（不可靠）；
          ② ZBrush 已在上一实例崩溃前导入、但回执没被读走的 -> 中转服务侧该传输永久
             卡在 claimed（server.py 持久化状态，见 _save），新实例 /inbox 不再返回它
             -> 既不会重投也不会收尾，陷入「claimed 永不终结」死锁，对应真机「无法接收」。
        故启动时必须（仅一次）：
          1. 重放 done/*.done.json 孤儿回执：凡是 tid 不在当前 zb_pending 的，直接
             _claim_finish 收尾（ZBrush 已导入的证据），并清理回执，让 server 侧
             claimed 传输正常终结，不再卡死；
          2. 重登记 inbox/*.json 孤儿任务文件：凡是还没对应回执/已消费文件（done/ 无同名）
             且不在当前 zb_pending 的，重新登记进 zb_pending（handshake 模式），让
             _zb_advance_pending 继续等回执——即便上一实例崩在「已写任务文件、ZBrush
             还没点」的窗口，新实例也能接着等、等到了就收尾，绝不丢失。
        """
        if not self.payload_dir:
            return
        inbox, _, _ = zb_dirs(self.payload_dir)
        done = zb_done_dir(self.payload_dir)
        if not inbox or not os.path.isdir(inbox):
            return
        pending_tids = {p.get("tid") for p in self.zb_pending}
        # 2026-10-26 修复（用户铁证 813dc7e 被删）：rehydrate 必须区分「守护自己写的
        # 任务文件」与「用户手动放入、服务端根本不认识的 json」。只接管 tid ∈ 服务端
        # claimed 集合的文件；用户 json 一律留给 ZBrush 插件 scan_inbox 自行消费，守护
        # 绝不认领、绝不超时删除。拉取失败时 claimed_tids 为空 -> adopt_all 退化为旧行为。
        my_apps = set(str(a).lower() for a in (self.cfg.get("apps") or []))
        claimed_tids = set()
        try:
            rsp = self.safe("GET", "/transfers?status=claimed&limit=1000")
            for rec in (rsp or {}).get("transfers") or []:
                to_a = _norm_app(str((rec.get("to") or {}).get("app", "")).lower())
                if to_a and (to_a in my_apps or to_a == "*"):
                    claimed_tids.add(str(rec.get("id", "")))
        except Exception:
            claimed_tids = set()
        adopt_all = (len(claimed_tids) == 0)
        if not adopt_all:
            self.log("[zb-rehydrate] 仅接管服务端 claimed 的任务文件（共 {0} 条），"
                     "其余视作用户手动 json 留给插件扫描".format(len(claimed_tids)))

        # 1) 孤儿回执重放：done/*.done.json 中不属当前 zb_pending 的 -> 收尾
        if done and os.path.isdir(done):
            for name in sorted(os.listdir(done)):
                if not name.endswith(".done.json"):
                    continue
                tid = name[:-len(".done.json")]
                if tid in pending_tids:
                    continue
                rp = os.path.join(done, name)
                try:
                    receipt = json.loads(Path(rp).read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                status = str(receipt.get("status", "error"))
                sstatus = "done" if status == "ok" else "error"
                self._claim_finish(tid, sstatus, {
                    "message": "ZBrush 已导入（守护重启后回执重放）: " +
                               "、".join(receipt.get("imported") or [])})
                try:
                    os.remove(rp)
                except OSError:
                    pass
                self._zb_delivered.discard(tid)
                self.event("守护重启：重放 ZBrush 回执 {0} -> {1}".format(tid, sstatus))

        # 2) 孤儿任务文件重登记：inbox/*.json（非回执）且尚未被消费（done/ 无同名）的
        consumed = set()
        if done and os.path.isdir(done):
            for name in os.listdir(done):
                if name.endswith(".json"):
                    consumed.add(name[:-len(".json")])
        for name in sorted(os.listdir(inbox)):
            if not name.endswith(".json") or name.endswith(".done.json"):
                continue
            tid = name[:-len(".json")]
            if tid in consumed or tid in pending_tids:
                continue
            if not adopt_all and tid not in claimed_tids:
                # 用户手动放入的 json：服务端无此 claimed 传输 -> 守护不接管，
                # 留给 ZBrush 插件 scan_inbox 自行消费（绝不超时删除）。
                continue
            path = os.path.join(inbox, name)
            try:
                rec = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            files = [f for f in (rec.get("files") or []) if f]
            self.zb_pending.append({
                "tid": tid, "files": files, "mode": "handshake",
                "deadline": time.monotonic() + ZB_IMPORT_TIMEOUT_SEC,
                "task": path, "from_app": str(rec.get("from_app", "")),
                "asset_name": rec.get("asset_name", ""),
                "renews": 0,
            })
            self._zb_delivered.add(tid)
            self.handled += 1
            self.event("守护重启：从磁盘恢复 ZBrush 待收任务 {0}（请在 ZBrush 点"
                       "「导入待收模型」接收）".format(tid))

        # 3) 孤儿 claimed 传输复活：最隐蔽的「完整重启后仍收不到」根因——上一实例
        # 在「claim 成功（服务端已置 claimed 并持久化）」与「写 inbox 任务文件」之间
        # 崩溃：磁盘上既无任务文件也无回执，步骤 1/2 都抓不到。重启后服务端永久卡
        # claimed，/inbox 不返回它，ZBrush 永收不到。这里主动把这类孤儿 claimed
        # （本守护负责、且无任何磁盘产物）reset 回 queued，后续 cycle 正常重投。
        try:
            rsp = self.safe("GET", "/transfers?status=claimed&limit=1000")
            claimed = (rsp or {}).get("transfers") or []
        except Exception:
            claimed = []
        my_apps = set(str(a).lower() for a in (self.cfg.get("apps") or []))
        # 汇齐所有磁盘产物对应的 tid（inbox 任务文件 + done/ 下回执或已消费任务文件）
        artifact_tids = set()
        if inbox and os.path.isdir(inbox):
            for name in os.listdir(inbox):
                if name.endswith(".json"):
                    artifact_tids.add(name[:-len(".json")])
        if done and os.path.isdir(done):
            for name in os.listdir(done):
                if name.endswith(".json"):
                    artifact_tids.add(name[:-len(".json")])
        revived = 0
        for rec in claimed:
            to_app = _norm_app(str((rec.get("to") or {}).get("app", "")).lower())
            if not to_app or (to_app not in my_apps and to_app != "*"):
                continue   # 不属于本守护负责的 app -> 留给对应守护复活
            tid = rec.get("id", "")
            if not tid or tid in artifact_tids:
                continue   # 有磁盘产物 -> 步骤 1/2 已接管，不抢
            r = self.safe("POST", "/transfers/{0}/retry".format(tid), {})
            if r is not None:
                revived += 1
                self._zb_delivered.discard(tid)
                self.event("守护重启：复活孤儿 claimed 传输 {0} -> queued（重新投递）".format(tid))
        if revived:
            self.log("[zb-rehydrate] 复活 {0} 条孤儿 claimed 传输（claim 后崩溃、无磁盘产物）".format(revived))

    def _zb_advance_pending(self) -> None:
        now = time.monotonic()
        inbox, _, _ = zb_dirs(self.payload_dir)
        done = zb_done_dir(self.payload_dir)
        pending_tids = {p["tid"] for p in self.zb_pending}
        # 清理遗留：旧版把 *.done.json 写在 inbox 顶层，这里直接删（守护不再从顶层读）。
        if inbox and os.path.isdir(inbox):
            try:
                for name in os.listdir(inbox):
                    if name.endswith(".done.json"):
                        _p = os.path.join(inbox, name)
                        self.log("[zb-cleanup] 删除 inbox 顶层遗留回执 {0}".format(_p))
                        try:
                            os.remove(_p)
                        except OSError:
                            pass
            except Exception:
                pass
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
                self._zb_delivered.discard(tid)
                self.zb_pending.remove(p)
                continue
            if now >= p["deadline"]:
                # handshake 模式：ZBrush 可能还没启动 / 用户还没点「接收」/ 或被
                # import_max_per_click 上限留下次再收。只要任务文件还在 inbox，就续期
                # 继续等——绝不删用户的待收任务（要求 二：剩余任务留 inbox 下次再收）。
                # 2026-10-18 P0：续期设上限 ZB_IMPORT_MAX_RENEWALS，超出即彻底清理，
                # 杜绝「任务文件永不被消费」导致的永久 claimed 滞留（卡死后续任务）。
                task_file = p.get("task") or ""
                task_file_exists = bool(task_file) and os.path.isfile(task_file)
                if (p["mode"] == "handshake" and task_file_exists
                        and p.get("renews", 0) < ZB_IMPORT_MAX_RENEWALS):
                    p["renews"] = p.get("renews", 0) + 1
                    p["deadline"] = now + ZB_IMPORT_TIMEOUT_SEC
                    self.event("ZBrush 任务 {0} 仍在等待（请在 ZBrush 点"
                               "「导入待收模型」接收）".format(tid))
                    continue
                # 任务文件仍在 inbox（未消费 / 被上限留下）：不删、不 cancel，重置续期计数器
                # 后继续等待。靠「文件存在」本身防泄漏——用户删文件或插件消费后文件消失，
                # 下一轮走下方孤儿清理分支，不会永久 claimed。
                if task_file_exists:
                    p["renews"] = 0
                    p["deadline"] = now + ZB_IMPORT_TIMEOUT_SEC
                    self.event("ZBrush 任务 {0} 仍在 inbox 未消费（续等下次接收）".format(tid))
                    continue
                if p["mode"] != "manual" and task_file:
                    self.log("[zb-cleanup] 超时删除任务文件 tid={0} path={1} "
                             "(文件已不在 inbox，视为已消费或丢失)".format(tid, task_file))
                    try:
                        os.remove(task_file)
                    except OSError:
                        pass
                self._claim_finish(tid, "cancelled", {
                    "message": "等待 ZBrush 导入回执超时（文件仍在负载目录，"
                               "可用 ZBrush 的 Tool:Load 手动加载）"})
                self.event("ZBrush 任务 {0} 等待超时".format(tid))
                self._zb_delivered.discard(tid)
                self.zb_pending.remove(p)
        # 孤儿/陈旧清理：done/ 里 tid 不在 zb_pending 且超过 5 分钟的文件（回执或已消费
        # 任务文件）直接删，防无限堆积（历史垃圾 / 孤儿回执）。注意：文件 age 用墙上
        # 时钟 time.time()，与上面的 now=monotonic() 区分开（两者不可混算）。
        if done and os.path.isdir(done):
            now_wall = time.time()
            try:
                for name in os.listdir(done):
                    if not (name.endswith(".done.json") or name.endswith(".json")):
                        continue
                    tid = name[:-len(".done.json")] if name.endswith(".done.json") \
                        else name[:-len(".json")]
                    if tid in pending_tids:
                        continue
                    try:
                        st = os.stat(os.path.join(done, name))
                        if now_wall - st.st_mtime > 300:
                            _dp = os.path.join(done, name)
                            self.log("[zb-cleanup] 删除 done/ 陈旧孤儿 {0} (age={1:.0f}s)".format(
                                _dp, now_wall - st.st_mtime))
                            os.remove(_dp)
                    except OSError:
                        pass
            except Exception:
                pass

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
    def _drop_watch(self, w: "Watch") -> None:
        """移除监视会话并注销其目录监听（WinDirWatcher 引用计数）。"""
        if w.dir:
            try:
                self.watcher.remove_dir(w.dir)
            except Exception:
                pass
        if w in self.watches:
            self.watches.remove(w)

    def advance_watches(self) -> None:
        now = time.monotonic()
        for w in list(self.watches):
            try:
                # 2026-10-26 P0-2：委托（编辑器已运行，二次启动秒退）场景下，Watch.proc
                # 是秒退的壳进程，真正的编辑器是已运行实例。LAUNCH_DELEGATE_SEC 过后若
                # 真实编辑器 exe 已不在（用户关掉了软件），则解除委托态并标记退出，让
                # 下方 advance() 进入取消分支，避免 watch 无限期挂着（实测关 PS 后 10s 无日志）。
                if w.delegated and getattr(w, "app_exe", ""):
                    if self._pid_of(w.app_exe) is None:
                        w.delegated = False
                        # 2026-10-26 P1 #1：解除委托态时把 proc_exited_at 重置为**真实
                        # 退出时间 now**（原逻辑保留壳进程秒退时刻 -> 用户关 PS 后 0 秒宽限
                        # 即被吞，ever_dirty 的 EXIT_GRACE 形同虚设）。重置后宽限从「真实
                        # 关闭 PS」起算，符合「关窗后给 N 秒宽限」语义。
                        w.proc_exited_at = now
                action = w.advance(now)
            except Exception as exc:
                self.log_rl("watch", "监视 {0} 异常: {1!r}".format(w.tid, exc))
                continue
            if action is None:
                continue
            # 2026-10-19：每个动作的副作用（_claim_finish / _drop_watch / event）都包进
            # try——任一动作抛异常只隔离该 watch，绝不让 advance_watches 冒泡到 cycle
            # 顶层把整个守护进程带崩（P0 真机根因：abandon_silent 处理中异常静默带崩进程）。
            try:
                if action == "finalize":
                    self.finalize(w)
                elif action == "abandon":
                    dirty = bool(w.dirty_names)
                    self._claim_finish(w.tid, "cancelled", {
                        "message": ("编辑器已关闭且文件有未保存修改，传输作废"
                                    if dirty else "编辑器已关闭且未检测到保存")})
                    self.event("{0} 关闭了 {1}，{2}".format(
                        APP_LABELS.get(w.app, w.app), w.asset_name,
                        "未保存修改，传输作废" if dirty else "未保存，传输作废"))
                    self._drop_watch(w)
                elif action == "abandon_silent":
                    # 2026-10-19：纯查看（未改文件就退出）的静默作废——只翻状态、不弹
                    # 错误回传事件（避免「看了眼没改」也弹「传输作废」的错误提示）。
                    self._claim_finish(w.tid, "cancelled", {
                        "message": "编辑器已关闭且未修改，传输静默作废"})
                    self._drop_watch(w)
                elif action == "clear":
                    # 已成功回传（returns>0）的 watch 超复用宽限后清理：只移除监视，
                    # 不翻写传输状态（传输早已 done，翻成 cancelled 会让回传结果凭空消失）。
                    # 实测间隔（now - proc_exited_at）一并打印，证明 REUSE_GRACE_SEC 真实。
                    measured = (now - w.proc_exited_at) if w.proc_exited_at else -1.0
                    self.log("[watch-clear] reuse 宽限到期，移除已回传 watch tid={0} "
                             "returns={1} (reuse_grace_measured={2:.1f}s, REUSE_GRACE_SEC={3})".format(
                                 w.tid, w.returns, measured, REUSE_GRACE_SEC))
                    self._drop_watch(w)
                elif action == "expire":
                    # 超时兜底：进程已退/没有、文件未被占用、长时间无保存行为
                    self._claim_finish(w.tid, "cancelled", {
                        "message": "超过 {0:.0f}s 无保存行为，监视解除（文件仍在负载目录）".format(
                            WATCH_IDLE_SEC)})
                    self.event("{0} 长时间未保存 {1}，监视解除".format(
                        APP_LABELS.get(w.app, w.app), w.asset_name))
                    self._drop_watch(w)
            except Exception as exc:
                self.log_rl("watch", "处理 watch {0} 动作 {1} 异常（已隔离，移除监视）: {2!r}".format(
                    w.tid, action, exc))
                try:
                    self._drop_watch(w)
                except Exception:
                    pass

    # -- 保存后回传 ---------------------------------------------------------
    def finalize(self, w: Watch) -> None:
        names = w.dirty_names or w.changed_files()
        if not names:
            self._drop_watch(w)
            return
        srcs = [os.path.join(w.dir, n) for n in names]
        fmt = os.path.splitext(names[0])[1].lstrip(".").lower() or "png"
        # 诊断（2026-10-13 夜）：保存回传三要素对照——保存了什么 / 原始 payload 在哪 /
        # 回传给谁。三个 payload 目录（原传输、回传传输）不同是设计使然（回传=新传输），
        # 关键是发送端能否按 source_files 把新目录的文件映射回原资产。
        self.log("[finalize] tid={0} editor={1} saved={2} src_paths={3}".format(
            w.tid, w.app, names, srcs))

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
        # 诊断（2026-10-13 夜）：回传选项全量——发送端 reload 按 source_files 名字映射
        self.log("[finalize] return_of={0} to_app={1} source_files={2} "
                 "asset_paths={3} format={4}".format(
                     w.tid, w.from_app, opts.get("source_files"),
                     opts.get("asset_paths"), fmt))
        rt = (created or {}).get("transfer") or {}
        rtid = rt.get("id", "")
        if not rtid:
            self._claim_finish(w.tid, "done", {
                "message": "检测到保存（{0}），但回传传输创建失败，文件仍在: {1}".format(
                    "、".join(names), w.dir),
                "saved_files": names,
            })
            self._drop_watch(w)
            return

        dest = os.path.join(self.payload_dir, rtid) if self.payload_dir else ""
        # 诊断（2026-10-13 夜）：回传传输的 payload 目录（与原传输目录不同是设计使然）
        self.log("[finalize] return transfer rtid={0} dest={1} copy_from={2}".format(
            rtid, dest, w.dir))
        if not dest:
            self._claim_finish(w.tid, "done", {
                "message": "检测到保存（{0}），但拿不到负载目录，回传取消，文件仍在: {1}".format(
                    "、".join(names), w.dir),
                    "saved_files": names,
                })
            self._drop_watch(w)
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
            self._drop_watch(w)
            return

        # 回传 publish 的 server 应答留痕（2026-10-14）：daemon 侧「已回传」
        # 只代表本地拷贝完成，发送端（UE 等）是否处理失败在 daemon 侧原本不可见，
        # 这里把 publish 的 HTTP 状态码 + 应答体摘要写进日志
        pcode, pbody = self.req_full("POST", "/transfers/{0}/publish".format(rtid), {})
        try:
            resp_summary = json.dumps(pbody, ensure_ascii=False)
        except Exception:
            resp_summary = repr(pbody)
        self.log("[finalize] publish rtid={0} http={1} resp={2}".format(
            rtid, pcode if pcode is not None else "网络失败",
            resp_summary[:200]))
        self._claim_finish(w.tid, "done", {
            "message": "{0} 已保存，回传 {1} 个文件 -> {2}".format(
                APP_LABELS.get(w.app, w.app), len(names),
                APP_LABELS.get(w.from_app, w.from_app)),
            "returned": True,
            "return_transfer": rtid,
            "saved_files": names,
        })
        # 分段计时（2026-10-14）：定位「SAI2 回传慢」——
        # 保存->daemon 检测（轮询间隔+settle）、检测->publish 完成。
        # 两侧都必须用 wall clock（time.time）：此前 finalize_at 存的是
        # monotonic 而文件 mtime 是 epoch，相减出 -1.7e9 的天文负数
        # （实测 save->detect=-1791725802.3s）。
        detect_at = w.finalize_at or time.time()
        done_at = time.time()
        try:
            newest_mtime = max(os.path.getmtime(s) for s in srcs)
            save_to_detect = detect_at - newest_mtime
        except Exception:
            save_to_detect = -1.0
        self.log("[timing] tid={0} save->detect={1:.1f}s detect->published={2:.1f}s "
                 "settle_reason={3} (poll_interval={4:.1f}s "
                 "settle_fallback_max={5:.1f}s)".format(
                     w.tid, save_to_detect, done_at - detect_at,
                     getattr(w, "last_settle_reason", ""),
                     self._current_poll_interval(), w.settle_max_wait))
        self.returned += 1
        self.event("已回传 {0} -> {1}（保存后自动重载，第 {2} 次）".format(
            "、".join(names), APP_LABELS.get(w.from_app, w.from_app), w.returns + 1))

        # 回传成功 ≠ 编辑结束（2026-10-14 P0：同一文件二次编辑不回传）。
        # 复位基线与 dirty：baseline=刚回传的内容，dirty 清空让下一次修改
        # 重新走 SETTLE_SEC 计时；watch 是否继续由 _watch_keep 判定。
        # 2026-10-15 P1-3：baseline 与上一轮快照（last_snap）必须用同一次
        # 采样复位——只复位 baseline 的话，残留的 mid-write last_snap 会让
        # 下一轮 advance 误判「仍在写」，把稳定计时白重置一轮。
        snap = w.snapshot()
        w.baseline = snap
        w.last_snap = snap
        w.dirty_names = []
        w.dirty_ts = 0.0
        w.stable_since_mono = None
        w.settle_start_mono = None
        w.settle_max_wait = 0.0
        w._prev_write_ts = 0.0
        w._last_write_gap = 0.0
        w.last_activity_mono = time.monotonic()
        w.returns += 1
        if self._watch_keep(w):
            self.log("[finalize] watch 保留 tid={0} returns={1}"
                     "（编辑器可能仍开着同一文件，可再次保存回传）".format(
                         w.tid, w.returns))
        else:
            self._drop_watch(w)
            self.log("[finalize] watch 解除 tid={0} returns={1}".format(
                w.tid, w.returns))

    def _watch_keep(self, w: "Watch") -> bool:
        """回传后是否继续监视同一 watch（三信号，任一为真即保留）：
        1. 我们启动的编辑器进程还活着；
        2. PS 委托场景（进程秒退但用户还在已有实例里编辑）；
        3. 距上次保存/回传不足 WATCH_IDLE_SEC（近期还在动）。

        2026-10-14：原第 4 信号「目标文件仍被持有（msvcrt O_RDWR 锁探测）」
        已移除——实测该探测的 O_RDWR 打开会被 SAI2 的文件变更监听捕获导致
        操作卡顿，且「持锁」对 SAI2 不可靠。详见文件顶部移除说明。
        """
        if w.delegated:
            return True
        if w.proc is not None:
            try:
                if w.proc.poll() is None:
                    return True
            except Exception:
                pass
        return (time.monotonic() - w.last_activity_mono) < WATCH_IDLE_SEC

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
            "zbrush_running": zbrush_process_running(),
            "handled": self.handled,
            "returned": self.returned,
            "last_event": self.last_event,
            "updated_at": now_iso(),
        }
        try:
            # 重试 3 次 × 100ms：规避杀软锁文件 / 托盘并发读导致的瞬时权限拒绝
            # （用户实机 daemon.log 出现 PermissionError(13, '拒绝访问。')）。
            # 仍失败则降级为日志，绝不抛异常影响主循环。
            _atomic_write_json(self.status_path, st, retries=3, retry_delay=0.1)
        except Exception as exc:
            self.log_rl("status",
                        "写 status.json 失败（重试 3 次仍失败，已降级为仅日志，不影响主循环）: {0!r}".format(exc))


# ---------------------------------------------------------------------------
# 子命令：--check / --stop
# ---------------------------------------------------------------------------

def _read_pidfile(state_dir: Path):
    p = state_dir / "daemon.pid"
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8-sig").strip() or "0")
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
            st = json.loads(status_file.read_text(encoding="utf-8-sig"))
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
