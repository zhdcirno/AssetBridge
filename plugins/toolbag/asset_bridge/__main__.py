# -*- coding: utf-8 -*-
"""
Asset Bridge :: Marmoset Toolbag 5 适配器
============================================================================
安装：整个 asset_bridge 文件夹拷到
      D:/DCC/Toolbag 5/data/plugin/asset_bridge/
      （Toolbag 启动时自动加载 data/plugin 下带 __main__.py 的文件夹）

作用（与其他适配器一致：轮询中转服务收件箱，认领后本地执行）：
  1) 烘焙模式（options.mode = "bake"，默认）
     - 找到场景里的 BakerObject（没有就新建一个）
     - 用 baker.importModel() 的快速加载器导入随资产发来的模型文件
       （高低模由 Toolbag 自动归类；名册见 bake group）
     - baker.addGroup() 建烘焙组，配置输出路径/分辨率/贴图类型
     - baker.bake() 开始烘焙，轮询输出文件落盘
     - 烘焙完成后自动回传一条 kind=textures 给 Substance Painter
  2) 渲染模式（options.mode = "render"）
     - 仅导入模型 / 材质，不做任何烘焙

设计约束（与仓库其他适配器相同）：
  * 只用标准库（Toolbag 内嵌 Python 3.12，不保证有第三方包）
  * 网络 IO 全部在 mset.callbacks.onPeriodicUpdate 里同步做，但每次都有
    时间闸门（>=2s）+ 短超时（1.5s）+ 指数退避 + 熔断，服务没开时不会
    把 Toolbag 拖死（本地连接失败是瞬间返回的，不会挂起）
  * 烘焙是异步的：bake() 之后靠「输出目录出现新文件且稳定」判断完成，
    不依赖任何轮询进度的 API（Toolbag 未暴露 bake 进度）

配置：同目录下的 config.json 可覆盖服务地址，例如
      {"server_url": "http://127.0.0.1:27631"}
      也可用环境变量 ASSET_BRIDGE_URL。
============================================================================
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.request

import mset

APP_NAME = "toolbag"
DEFAULT_SERVER = "http://127.0.0.1:27631"

POLL_INTERVAL = 2.0        # 正常轮询间隔（秒）
HTTP_TIMEOUT = 1.5         # 单次 HTTP 超时
HEARTBEAT_EVERY = 15       # 每 N 次轮询打一次心跳
BAKE_SETTLE_SEC = 2.5      # 输出文件稳定多久算烘焙完成
BAKE_TIMEOUT_SEC = 600.0   # 烘焙等待上限（10 分钟）
MAX_LOG_EVERY = 30.0       # 失败日志限频（秒）

# 默认烘焙输出的贴图类型（getMap 的候选名，逐个尝试直到命中）
DEFAULT_BAKE_MAPS = [
    ["Normals", "Normal", "Normal Map"],
    ["Ambient Occlusion", "AO", "Occlusion"],
]

MODEL_EXTS = (".fbx", ".obj", ".usd", ".usda", ".usdc", ".abc", ".ply", ".stl")
MATERIAL_EXTS = (".tmb", ".mtl", ".sbsar")  # Toolbag 材质 / MTL


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _server_url():
    env = os.environ.get("ASSET_BRIDGE_URL", "").strip()
    if env:
        return env
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            url = str(cfg.get("server_url", "")).strip()
            if url:
                return url
    except Exception:
        pass
    return DEFAULT_SERVER


SERVER_URL = _server_url()


# ---------------------------------------------------------------------------
# 极简 HTTP 客户端（标准库，短超时，绝不让异常逃出回调）
# ---------------------------------------------------------------------------

# 本机回环必须绕过代理：这台机器的系统 HTTP 代理会把 127.0.0.1 也劫走，
# 表现是 502 upstream，极难查（curl 侧同样要 --noproxy '*'）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(method, path, body=None, timeout=HTTP_TIMEOUT):
    url = SERVER_URL + "/api/v1" + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _safe(method, path, body=None, timeout=HTTP_TIMEOUT):
    """请求的容错包装：任何网络异常都返回 None（由调用方走退避逻辑）。"""
    try:
        return _request(method, path, body, timeout)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 适配器状态
# ---------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.session_id = ""
        self.next_poll_ts = 0.0
        self.fail_streak = 0
        self.circuit_open = False
        self.last_probe_ts = 0.0
        self.last_fail_log_ts = 0.0
        self.poll_count = 0
        self.job = None            # 进行中的烘焙任务（跨多次回调的状态机）
        self.last_event = "适配器已加载"
        self.busy = False          # bake() 同步执行中（避免重入）


_S = _State()


def _log(msg):
    # Toolbag 控制台 + mset 日志双写，方便排查
    try:
        print("[bridge] " + msg)
    except Exception:
        pass
    try:
        mset.log("[bridge] " + msg + "\n")
    except Exception:
        pass


def _set_status(msg):
    _S.last_event = msg
    _refresh_ui()


# ---------------------------------------------------------------------------
# UI（一个可隐藏的小窗口；UITextField.value 可写，用来显示最近事件）
# ---------------------------------------------------------------------------

_ui = {"win": None, "status": None}


def _refresh_ui():
    try:
        if _ui["status"] is not None:
            _ui["status"].value = _S.last_event
    except Exception:
        pass


def _add_return(win):
    """Toolbag UI 是流式布局：addReturn() 切到下一行，否则所有控件挤在一行
    （表现为「Pull Now 左侧一大片空位」）。方法名在 Toolbag 5 文档里叫
    addReturn；个别版本若没有则跳过（不阻断建面板）。"""
    try:
        fn = getattr(win, "addReturn", None)
        if callable(fn):
            fn()
    except Exception:
        pass


def _pull_now():
    """「Pull Now」按钮：先给即时反馈，再强制轮询一次（绕过时间闸门）。"""
    _set_status("Pulling now…")
    _poll_once(force=True)


def _build_ui():
    try:
        win = mset.UIWindow("Asset Bridge (Toolbag)")
        win.addElement(mset.UILabel("Asset Bridge Adapter (Toolbag)"))
        _add_return(win)
        status = mset.UITextField()
        status.value = _S.last_event
        win.addElement(status)
        _ui["status"] = status
        _add_return(win)

        pull_btn = mset.UIButton("Pull Now")
        pull_btn.onClick = lambda: _pull_now()
        win.addElement(pull_btn)
        _add_return(win)

        hide_btn = mset.UIButton("Hide (keep polling)")
        hide_btn.onClick = lambda: setattr(win, "visible", False)
        win.addElement(hide_btn)

        win.visible = True
        _ui["win"] = win
    except Exception as exc:
        _log("UI creation failed (polling continues): {0}".format(exc))


# ---------------------------------------------------------------------------
# 会话 / 收件箱
# ---------------------------------------------------------------------------

def _register():
    r = _safe("POST", "/sessions", {
        "app": APP_NAME,
        "version": str(_toolbag_version()),
        "capabilities": {"receive": True, "send": False, "modes": ["bake", "render"]},
    })
    if r and r.get("session"):
        _S.session_id = r["session"]["session_id"]
        _set_status("session registered: " + _S.session_id)
        return True
    return False


def _toolbag_version():
    try:
        return mset.getToolbagVersion()
    except Exception:
        return ""


def _heartbeat():
    if not _S.session_id:
        return
    _safe("POST", "/sessions/{0}/heartbeat".format(_S.session_id), {})


def _inbox():
    r = _safe("GET", "/inbox?app={0}&limit=5".format(APP_NAME))
    if r is None:
        return None
    return r.get("transfers") or []


def _claim(tid):
    return _safe("POST", "/transfers/{0}/claim".format(tid),
                 {"session_id": _S.session_id})


def _finish(tid, status, result):
    return _safe("POST", "/transfers/{0}/finish".format(tid),
                 {"status": status, "result": result})


def _payload_dir():
    r = _safe("GET", "/config")
    if not r:
        return ""
    return r.get("payload_dir") or ""


# ---------------------------------------------------------------------------
# 退避 / 熔断（与 Blender/SP 适配器同策略）
# ---------------------------------------------------------------------------

def _on_poll_failure(what):
    _S.fail_streak += 1
    backoff = min(1000 * (2 ** (_S.fail_streak - 1)), 30000) / 1000.0
    _S.next_poll_ts = time.time() + backoff
    now = time.time()
    if now - _S.last_fail_log_ts >= MAX_LOG_EVERY:
        _S.last_fail_log_ts = now
        _log("server comm failure({0}): {1}".format(_S.fail_streak, what))
    if _S.fail_streak >= 5 and not _S.circuit_open:
        _S.circuit_open = True
        _S.last_probe_ts = now
        _set_status("server unreachable, polling paused")


def _on_poll_success():
    _S.fail_streak = 0
    _S.circuit_open = False
    _S.next_poll_ts = time.time() + POLL_INTERVAL


# ---------------------------------------------------------------------------
# 场景操作
# ---------------------------------------------------------------------------

def _find_baker():
    try:
        for obj in mset.getAllObjects():
            if isinstance(obj, mset.BakerObject):
                return obj
    except Exception as exc:
        _log("scene scan failed: {0}".format(exc))
    return None


def _get_baker(create=True):
    """优先用场景里已有的 Baker；没有就尝试新建（失败返回 None）。"""
    baker = _find_baker()
    if baker is not None:
        return baker
    if not create:
        return None
    try:
        baker = mset.BakerObject()
        _log("no Baker in scene, created one")
        return baker
    except Exception as exc:
        _log("cannot create BakerObject ({0}): add a Bake object to the scene".format(exc))
        return None


def _model_files(record):
    out = []
    for f in record.get("_files_abs") or []:
        if str(f).lower().endswith(MODEL_EXTS):
            out.append(str(f))
    return out


def _material_files(record):
    out = []
    for f in record.get("_files_abs") or []:
        if str(f).lower().endswith(MATERIAL_EXTS):
            out.append(str(f))
    return out


def _enable_map(baker, candidates):
    """按候选名逐个尝试 getMap，命中就 enabled=True，返回命中的名字或 None。"""
    for name in candidates:
        try:
            m = baker.getMap(name)
            m.enabled = True
            return name
        except Exception:
            continue
    return None


def _configure_baker(baker, record, out_path):
    opts = record.get("options") or {}
    try:
        baker.outputPath = out_path
    except Exception as exc:
        _log("set outputPath failed: {0}".format(exc))
    for attr, val in (("outputWidth", 2048), ("outputHeight", 2048),
                      ("outputBits", 8), ("outputSamples", 16)):
        try:
            setattr(baker, attr, val)
        except Exception:
            pass
    # 多文件输出（每张贴图一个文件）对回传 SP 更友好；
    # Toolbag 不支持所选格式的多文件时会自行改成单 PSD，以实际落盘为准。
    try:
        baker.outputSinglePsd = False
    except Exception:
        pass

    maps = opts.get("maps") or DEFAULT_BAKE_MAPS
    enabled = []
    for entry in maps:
        candidates = entry if isinstance(entry, (list, tuple)) else [entry]
        hit = _enable_map(baker, candidates)
        if hit:
            enabled.append(hit)
        else:
            _log("skipping unsupported map type: {0}".format(entry))
    return enabled


# ---------------------------------------------------------------------------
# 烘焙模式
# ---------------------------------------------------------------------------

def _start_bake(record):
    """第一次进入 job 时调用：搭场景 + 开始烘焙。返回 False 表示直接失败。"""
    tid = record["id"]
    name = record.get("asset_name") or "Bake"
    models = _model_files(record)
    if not models:
        _finish(tid, "error", {"message": "没有随资产发来的模型文件（fbx/obj/usd/abc/ply/stl）"})
        _set_status("bake failed: no model files")
        return False

    payload = _payload_dir()
    if not payload:
        _finish(tid, "error", {"message": "拿不到中转服务负载目录"})
        _set_status("bake failed: no payload dir")
        return False

    baker = _get_baker(create=True)
    if baker is None:
        _finish(tid, "error", {"message": "场景里没有 Baker 且无法新建，请手动添加 Bake 对象后重试"})
        _set_status("bake failed: no Baker")
        return False

    out_dir = os.path.join(payload, tid)
    try:
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
    except Exception as exc:
        _finish(tid, "error", {"message": "创建输出目录失败: {0}".format(exc)})
        return False

    # 导入模型：既要让网格进主场景（可见），也要登记进 Baker 作为烘焙输入。
    # - mset.importModel 把网格加进主场景（render 模式同款调用，已验证可见）；
    # - baker.importModel 走 Baker 快速加载器自动建高低模槽位。
    # 两者任一成功即视为导入可用；render 模式只走 mset.importModel，烘焙模式
    # 额外登记 Baker（2026-10-16 修复：原烘焙模式只调 baker.importModel 导致
    # 网格未进主场景、用户看到「只建了 bake 组、模型没进来」）。
    imported = []
    for path in models:
        ok_scene = False
        try:
            obj = mset.importModel(path)          # 进主场景（可见）
            ok_scene = obj is not None
            if ok_scene:
                try:
                    mset.frameObject(obj)
                except Exception:
                    pass
        except Exception as exc:
            _log("scene model import failed {0}: {1}".format(path, exc))
        ok_baker = False
        try:
            baker.importModel(path)              # 登记进 Baker 槽位
            ok_baker = True
        except Exception as exc:
            _log("baker model import failed {0}: {1}".format(path, exc))
        if ok_scene or ok_baker:
            imported.append(os.path.basename(path))
        else:
            _log("both scene & baker import failed {0}".format(path))
    if not imported:
        _finish(tid, "error", {"message": "所有模型文件都导入失败"})
        _set_status("bake failed: model import failed")
        return False

    # 建烘焙组（用户明确要求：烘焙模式要创建烘焙组）
    try:
        grp = baker.addGroup(name)
        _log("bake group created: {0}".format(getattr(grp, "name", name)))
    except Exception as exc:
        _log("bake group failed (continuing): {0}".format(exc))

    out_path = os.path.join(out_dir, name + ".png")
    enabled_maps = _configure_baker(baker, record, out_path)

    before = _snapshot(out_dir)
    _S.job = {
        "tid": tid,
        "name": name,
        "out_dir": out_dir,
        "before": before,
        "stage": "baking",
        "since": 0.0,
        "deadline": time.time() + BAKE_TIMEOUT_SEC,
        "models": imported,
        "maps": enabled_maps,
    }
    _set_status("baking {0} ({1} models, maps: {2})".format(
        name, len(imported), "/".join(enabled_maps) or "默认"))
    return True


def _snapshot(directory):
    try:
        return set(os.listdir(directory))
    except Exception:
        return set()


def _advance_bake():
    """烘焙状态机：bake() -> 等文件 -> 回传 SP -> finish。"""
    job = _S.job
    if job is None:
        return
    tid = job["tid"]

    if job["stage"] == "baking":
        baker = _get_baker(create=False)
        if baker is None:
            _finish(tid, "error", {"message": "烘焙过程中 Baker 消失了"})
            _S.job = None
            return
        _S.busy = True
        try:
            baker.bake()
        except Exception as exc:
            _S.busy = False
            _finish(tid, "error", {"message": "bake() 失败: {0}".format(exc)})
            _set_status("bake failed: {0}".format(exc))
            _S.job = None
            return
        _S.busy = False
        job["stage"] = "await"
        job["since"] = time.time()
        _set_status("baking... {0}".format(job["name"]))
        return

    if job["stage"] == "await":
        now = time.time()
        current = _snapshot(job["out_dir"])
        new_files = sorted(current - job["before"])
        if new_files:
            if job["since"] == 0.0:
                job["since"] = now
            if now - job["since"] >= BAKE_SETTLE_SEC:
                _finish_bake(job, new_files)
            return
        # 还没有新文件：重置稳定计时，防超时
        job["since"] = 0.0
        if now > job["deadline"]:
            _finish(tid, "error", {"message": "等待烘焙输出超时（{0:.0f}s）".format(BAKE_TIMEOUT_SEC)})
            _set_status("bake timed out")
            _S.job = None
        return


def _finish_bake(job, new_files):
    tid = job["tid"]
    out_dir = job["out_dir"]
    _set_status("bake done, returning textures to SP...")

    # 回传一条 kind=textures 给 SP（SP 侧作为工程资源导入）
    target_app = "substance_painter"
    created = _safe("POST", "/transfers", {
        "asset_name": job["name"],
        "kind": "textures",
        "format": "png",
        "to": {"app": target_app, "project": "", "content_path": ""},
        "options": {"mode": "update", "from_toolbag": tid},
        "note": "Toolbag baked maps ({0})".format(", ".join(job.get("maps") or [])),
    })
    return_tid = ""
    if created and created.get("transfer"):
        return_tid = created["transfer"]["id"]
        dest = os.path.join(_payload_dir() or "", return_tid)
        try:
            if not os.path.isdir(dest):
                os.makedirs(dest)
            for fn in new_files:
                src = os.path.join(out_dir, fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dest, fn))
            _safe("POST", "/transfers/{0}/publish".format(return_tid), {})
        except Exception as exc:
            _log("texture copy failed: {0}".format(exc))
            _safe("POST", "/transfers/{0}/finish".format(return_tid),
                  {"status": "error", "result": {"message": "拷贝失败: {0}".format(exc)}})
            return_tid = ""

    if return_tid:
        _finish(tid, "done", {
            "message": "烘焙完成并已回传 {0} 张贴图 -> SP".format(len(new_files)),
            "baked_files": new_files,
            "return_transfer": return_tid,
        })
        _set_status("returned {0} textures -> SP".format(len(new_files)))
    else:
        _finish(tid, "done", {
            "message": "烘焙完成（{0} 个文件），但回传 SP 失败，文件在负载目录".format(len(new_files)),
            "baked_files": new_files,
        })
        _set_status("bake done, SP return failed")
    _S.job = None


# ---------------------------------------------------------------------------
# 渲染模式
# ---------------------------------------------------------------------------

def _handle_render(record):
    tid = record["id"]
    name = record.get("asset_name") or "Scene"
    models = _model_files(record)
    if not models:
        _finish(tid, "error", {"message": "没有随资产发来的模型文件"})
        _set_status("render failed: no model files")
        return
    imported = []
    for path in models:
        try:
            obj = mset.importModel(path)
            imported.append(os.path.basename(path))
            try:
                if obj is not None:
                    mset.frameObject(obj)
            except Exception:
                pass
        except Exception as exc:
            _log("model import failed {0}: {1}".format(path, exc))
    mats = _material_files(record)
    for path in mats:
        try:
            mset.importMaterial(path)
        except Exception as exc:
            _log("material import failed {0}: {1}".format(path, exc))
    if not imported:
        _finish(tid, "error", {"message": "所有模型文件都导入失败"})
        _set_status("render failed: import failed")
        return
    _finish(tid, "done", {
        "message": "已导入 {0} 个模型{1}".format(
            len(imported), "、{0} 个材质".format(len(mats)) if mats else ""),
        "objects": imported,
    })
    _set_status("imported {0} (render mode)".format(name))


# ---------------------------------------------------------------------------
# 轮询主循环
# ---------------------------------------------------------------------------

def _dispatch(record):
    opts = record.get("options") or {}
    mode = str(opts.get("mode", "bake")).lower()
    kind = str(record.get("kind", "static_mesh")).lower()

    if kind == "textures":
        # Toolbag 侧不接收贴图（只发不收），直接回执，避免记录卡在队列里
        _finish(record["id"], "error",
                {"message": "Toolbag 适配器只接收模型（bake/render），不收贴图"})
        return

    if mode == "render":
        _handle_render(record)
    else:
        # bake：进入跨回调的状态机（bake() 可能耗时较长）
        _start_bake(record)


def _poll_once(force=False):
    now = time.time()
    if not force:
        if now < _S.next_poll_ts:
            return
        if _S.circuit_open:
            if now - _S.last_probe_ts < 30.0:
                return
            _S.last_probe_ts = now
            if not _S.session_id and not _register():
                return
            if _S.session_id:
                _S.circuit_open = False
                _S.fail_streak = 0
                _set_status("server back online")
            else:
                return

    if _S.busy:
        return

    # 有进行中的烘焙任务时，优先推进状态机
    if _S.job is not None:
        _advance_bake()
        return

    if not _S.session_id:
        if not _register():
            _on_poll_failure("register failed")
            return

    _S.poll_count += 1
    if _S.poll_count % HEARTBEAT_EVERY == 1:
        _heartbeat()

    items = _inbox()
    if items is None:
        _on_poll_failure("inbox request failed")
        return
    _on_poll_success()

    for rec in items:
        full = _claim(rec["id"])
        if not full or not full.get("transfer"):
            continue
        record = full["transfer"]
        _log("received {0} (from {1}, rev {2}, mode={3})".format(
            record.get("asset_name"),
            (record.get("from") or {}).get("app", "?"),
            record.get("revision"),
            (record.get("options") or {}).get("mode", "bake")))
        try:
            _dispatch(record)
        except Exception as exc:
            _finish(record["id"], "error",
                    {"message": "{0}: {1}".format(type(exc).__name__, exc)})
            _set_status("handle failed: {0}".format(exc))
        break  # 一次只处理一条，剩下的下个轮询周期


def _on_periodic_update():
    """mset.callbacks.onPeriodicUpdate：几秒一次，保持轻量。"""
    try:
        _poll_once()
    except Exception as exc:
        _log("poll exception: {0}".format(exc))


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def _install_callbacks():
    try:
        cbs = mset.callbacks
        cbs.onPeriodicUpdate = _on_periodic_update
        try:
            cbs.onSceneLoaded = lambda: _set_status("scene loaded")
        except Exception:
            pass
        return True
    except Exception as exc:
        _log("callback registration failed: {0}".format(exc))
        return False


_build_ui()
if _install_callbacks():
    _log("Toolbag adapter loaded (server {0})".format(SERVER_URL))
    _set_status("adapter loaded, server " + SERVER_URL)
    # 立即尝试注册一次，让面板状态尽快亮起来
    _register()
else:
    _set_status("callback registration failed, adapter inactive")
