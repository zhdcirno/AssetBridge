# -*- coding: utf-8 -*-
"""
Asset Bridge :: Maya 适配器
============================================================================
安装：把 asset_bridge_maya 放到 MAYA_SCRIPT_PATH 或
      ~/Documents/maya/<ver>/scripts 下，在 userSetup.py 里加：
          import maya.utils
          maya.utils.executeDeferred(
              lambda: __import__('asset_bridge_maya').start())

线程模型（Bug3 修复后）：
    **所有网络 IO 都在后台 daemon 线程里做**，绝不阻塞 Maya 主线程。
    scriptJob 的 idle 回调（_on_idle）只从队列里取出记录、调用 Maya API
    （cmds），不做任何 HTTP 请求。这样即使中转服务挂了，Maya 的 UI
    也完全不会被拖死（旧实现在主线程串行发 heartbeat + inbox，服务不可达
    时 UI 几乎全程冻结，卡在启动界面）。

----------------------------------------------------------------------------
兼容性说明
----------------------------------------------------------------------------
Maya 2018 及更早版本内置 **Python 2.7**，Maya 2020+ 才是 Python 3。
本模块同时兼容两代解释器，做法是：

  * 顶部 `from __future__ import print_function`；
  * 不使用 f-string，统一 `.format()`；
  * 不使用 `pathlib`（py2.7 没有），统一 `os.path`；
  * HTTP 层走 `_urlreq` / `_urlerror` 两个别名：
      py3 -> urllib.request / urllib.error
      py2 -> urllib2（HTTPError、URLError 也都挂在 urllib2 上）
  * 统一走无代理 opener，避免公司网络下 HTTP_PROXY 劫持 127.0.0.1 报 502；
  * 不用 `tempfile.TemporaryDirectory`（py3 才有），改用 mkdtemp + rmtree；
  * 后台线程里**绝不调用 cmds / mel**（Maya API 只能在主线程），只做网络
    与 print 日志；Maya API 调用全部留在 idle 回调（主线程）里。
============================================================================
"""
from __future__ import print_function

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid

import maya.cmds as cmds
import maya.mel as mel

# py2.7 没有 queue 模块（叫 Queue），做个兼容别名
try:
    import queue  # py3
except ImportError:  # py2.7
    import Queue as queue  # noqa: N811

# ---------------------------------------------------------------------------
# Python 2 / 3 兼容层
# ---------------------------------------------------------------------------
# py2.7（Maya 2018 及更早）与 py3（Maya 2020+）的 urllib 布局不同，
# 这里统一收敛成两个别名，业务代码只认 _urlreq / _urlerror。
_PY2 = sys.version_info[0] == 2

if _PY2:
    import urllib2 as _urlreq        # py2: Request / ProxyHandler / build_opener
    import urllib2 as _urlerror      # py2: HTTPError / URLError 同在 urllib2
else:
    import urllib.request as _urlreq
    import urllib.error as _urlerror

API = "/api/v1"

# 名称归一化（中文 -> 拼音 ASCII 驼峰）：与本文件同目录的 bridge_names.py。
# 模块内部只用 os/ord，py2.7 也能用；表文件缺失时退化为旧的 `isalnum` 过滤。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bridge_names import to_ascii as _to_ascii
except Exception:  # noqa: BLE001
    _to_ascii = None

# 绕过代理：公司网络下 urllib 会拿 HTTP_PROXY 去连 127.0.0.1，报 502。
# py3 -> urllib.request.ProxyHandler({})   py2 -> urllib2.ProxyHandler({})
_OPENER = _urlreq.build_opener(_urlreq.ProxyHandler({}))

CONFIG = {
    "server_url": "http://127.0.0.1:27631",
    "project": "",
    "poll_interval": 1.0,
    "auto_receive": True,
    # 默认不在启动时开 commandPort（安全默认）。
    # 原因：Maya 的 `commandPort -close` 会**去连接**目标端口，若该端口被另一个
    # Maya 实例占用且其解释器已卡死，本调用会**永久阻塞**调用线程 —— 这正是
    # 「Maya 卡在启动界面(正在加载插件)」的根因。
    # 需要被外部脚本远程驱动时，显式调用 start(open_command_port=True)。
    "open_command_port": False,
    "command_port": 7001,
}
_job_id = None
_session_id = None
_payload_dir = None
_tick = 0
_last_poll = 0.0

# ---------------------------------------------------------------------------
# 后台轮询线程（所有网络 IO 都在这，绝不阻塞主线程）
# ---------------------------------------------------------------------------
_commandport_name = None  # 本实例自己开过的 commandPort 名（只关自己开的那个）
_worker = None           # 后台 daemon 线程
_stop_event = None       # 本次运行独立的停止事件（杜绝 clear() 复活旧线程）
_recv_queue = None       # worker -> 主线程：已认领的完整记录
_result_queue = None     # 主线程 -> worker：导入结果，由 worker 负责 finish
_maya_version = ""       # 主线程缓存的 Maya 版本（线程里不调 cmds.about）

# 退避 / 熔断 / 日志限频（与 SP / UE 同策略）
_fail_streak = 0
_next_poll_ts = 0.0
_last_fail_log_ts = 0.0
_circuit_open = False
_last_probe_ts = 0.0
_last_status_ts = 0.0      # 面板状态灯刷新节流（_on_idle 用）
_last_status_ui_ts = 0.0   # UI 刷新节流（与 worker 探测解耦）
_last_status_probe_ts = 0.0  # worker 状态探测节流
_STATUS_CACHE = {"text": "● 检测中…", "rgb": (0.4, 0.4, 0.4), "ok": False}
_panel_job = None          # 面板专用 idle scriptJob（与桥接启停解耦，保证灯实时刷新）
_panel_status_ts = 0.0


def _reset_runtime_state():
    """重置模块级运行态。仅供启动/重启用，业务代码不需要调用。"""
    global _session_id, _payload_dir, _tick, _last_poll
    global _fail_streak, _next_poll_ts, _last_fail_log_ts, _circuit_open, _last_probe_ts
    global _last_status_probe_ts, _last_status_ui_ts, _STATUS_CACHE
    _session_id = None
    _payload_dir = None
    _tick = 0
    _last_poll = 0.0
    _fail_streak = 0
    _next_poll_ts = 0.0
    _last_fail_log_ts = 0.0
    _circuit_open = False
    _last_probe_ts = 0.0
    _last_status_probe_ts = 0.0
    _last_status_ui_ts = 0.0
    _STATUS_CACHE = {"text": "● 检测中…", "rgb": (0.4, 0.4, 0.4), "ok": False}


# ---------------------------------------------------------------------------
# 极简客户端
# ---------------------------------------------------------------------------


class BridgeError(Exception):
    pass


def _urlopen(req, timeout=3.0):
    """跨版本 urlopen 适配：统一走无代理 opener。

    py3 传的是 urllib.request.Request，py2 传的是 urllib2.Request，
    两边 opener.open 的签名一致，所以这里只做一层薄封装。

    :param req: 由 _mk_request 构造好的 Request 对象
    :param timeout: 超时秒数
    :returns: 响应对象（调用方负责 close）
    """
    return _OPENER.open(req, timeout=timeout)


def _mk_request(url, method, data=None):
    """跨版本构造 Request 对象。

    py2 的 urllib2.Request 没有 `method=` 关键字参数，
    需要覆盖实例方法 get_method 才能发出 PUT / DELETE。

    :param url: 完整 URL
    :param method: HTTP 方法，如 GET / POST / PUT
    :param data: 请求体字节串，可为 None
    :returns: Request 对象
    """
    if _PY2:
        req = _urlreq.Request(url, data=data)
        req.get_method = lambda: method
    else:
        req = _urlreq.Request(url=url, method=method, data=data)
    return req


def _req(method, path, body=None, data=None, timeout=3.0):
    url = "{0}{1}{2}".format(CONFIG["server_url"], API, path)
    req = _mk_request(url, method)
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
        req.data = data
    elif body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        resp = _urlopen(req, timeout=timeout)
        try:
            raw = resp.read().decode("utf-8")
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return json.loads(raw) if raw else {}
    except _urlerror.HTTPError as exc:
        raise BridgeError("HTTP {0}: {1}".format(
            exc.code, exc.read().decode("utf-8", "ignore")))
    except _urlerror.URLError as exc:
        raise BridgeError("连不上中转服务: {0}".format(exc.reason))


def _safe_version():
    """主线程之外取 Maya 版本会崩，故用缓存；兜底才在异常里调 cmds。"""
    if _maya_version:
        return _maya_version
    try:
        return cmds.about(version=True)
    except Exception:
        return ""


def _post_transfer(meta):
    """发送前统一注入 from.app/version，修复服务端回传阶段报「发送端未标注 from.app」。"""
    meta.setdefault("from", {})
    meta["from"].update({"app": "maya", "version": _safe_version()})
    return _req("POST", "/transfers", meta)["transfer"]


def register():
    global _session_id
    v = _safe_version()
    s = _req("POST", "/sessions", {
        "app": "maya", "version": v, "project": CONFIG["project"],
        "host": os.environ.get("COMPUTERNAME", ""), "pid": os.getpid(),
        "capabilities": {"formats": ["fbx", "ma", "obj", "abc"], "send": True, "receive": True},
    })["session"]
    _session_id = s["session_id"]
    return s


def payload_dir():
    global _payload_dir
    if _payload_dir and os.path.isdir(_payload_dir):
        return _payload_dir
    try:
        p = _req("GET", "/config", timeout=2.0)["payload_dir"]
        if os.path.isdir(p):
            _payload_dir = p
            return _payload_dir
    except Exception:
        pass
    return None


def _bridge_running():
    """本适配器是否在运行：有有效的 idle scriptJob 且后台 worker 线程存活。"""
    if _job_id is None or _worker is None:
        return False
    try:
        if not _worker.is_alive():
            return False
    except Exception:
        return False
    try:
        if not cmds.scriptJob(exists=_job_id):
            return False
    except Exception:
        pass
    return True


def is_connected():
    """连接状态 = 本适配器在运行 且 中转服务可达。

    可达性由后台 worker 线程探测并写入 _STATUS_CACHE，这里只在主线程读缓存，
    不发起网络请求，避免没开服务器时主线程卡在 /health 超时上。
    """
    if not _bridge_running():
        return False
    return _STATUS_CACHE.get("ok", False)


def connection_state():
    """返回 (状态文本, (r,g,b))，供面板状态灯显示三态。主线程只读缓存。"""
    if not _bridge_running():
        return "● 桥接未启动", (0.55, 0.32, 0.08)
    return _STATUS_CACHE["text"], _STATUS_CACHE["rgb"]


def _update_status_cache():
    """后台 worker 线程调用：短超时探测连通性，更新 _STATUS_CACHE（主线程只读）。

    绝不调 _bridge_running（里面会碰 cmds，子线程不安全），worker 存活即代表桥接在跑。
    """
    global _STATUS_CACHE
    try:
        _req("GET", "/health", timeout=0.3)
        _STATUS_CACHE = {"text": "● 已连接 桥接服务器", "rgb": (0.16, 0.55, 0.18), "ok": True}
    except Exception:
        _STATUS_CACHE = {"text": "● 服务器不可达", "rgb": (0.65, 0.12, 0.12), "ok": False}


def route_profile(src):
    """读取插件自带的 profiles.json，取 `<src>>maya` 这条路由配置。"""
    try:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
        with io.open(fp, "r", encoding="utf-8") as fh:
            j = json.load(fh)
        return j.get("routes", {}).get("{0}>maya".format(src), {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 导入 / 导出
# ---------------------------------------------------------------------------


def _ensure_fbx_plugin():
    try:
        loaded = cmds.pluginInfo("fbxmaya", query=True, loaded=True)
    except Exception:
        loaded = False
    if not loaded:
        cmds.loadPlugin("fbxmaya", quiet=True)


def _try_mel(*commands):
    """按顺序尝试多条等价的 MEL 命令，返回第一条执行成功的。

    FBX 插件的 MEL 命令在不同版本里参数写法不一致
    （例如 FBXExportScaleFactor 有的版本认 `-v 1`，有的只认位置参数 `1`），
    用「依次尝试」代替「一次写死」，避免某个参数写法变了就整段导出配置失效。
    全部失败只警告，不抛（导出本身还能继续）。
    """
    last = None
    for cmd in commands:
        try:
            mel.eval(cmd)
            return cmd
        except Exception as exc:   # noqa: BLE001 - MEL 层异常类型不可控
            last = exc
    if last is not None:
        cmds.warning("[bridge] MEL 全部失败 {0}: {1}".format(list(commands), last))
    return None


def import_fbx(path, profile, material_names=None):
    _ensure_fbx_plugin()
    # 先清掉已存在的同名材质节点，迫使 FBX 重建带新属性的材质。
    # 否则 Maya 会按名复用旧材质 -> 编辑材质/材质槽后再次接收不更新。
    _purge_materials(material_names)
    up = str((profile.get("import") or {}).get("up_axis", "y")).lower()
    _try_mel('FBXImportUpAxis "{0}"'.format(up))
    before = set(cmds.ls(assemblies=True) or [])
    cmds.file(path, i=True, type="FBX", ignoreVersion=True, mergeNamespacesOnClash=True,
              namespace=":", returnNewNodes=True, options="fbx")
    after = set(cmds.ls(assemblies=True) or [])
    return sorted(after - before)


def import_obj(path):
    """导入 OBJ（ZBrush 发来的模型都是 OBJ；走 FBX 导入器会直接报错）。

    依赖 Maya 自带 objExport 插件（同时注册 OBJ 导入翻译器）。
    """
    try:
        if not cmds.pluginInfo("objExport", query=True, loaded=True):
            cmds.loadPlugin("objExport", quiet=True)
    except Exception:
        pass
    before = set(cmds.ls(assemblies=True) or [])
    # OBJ 导入翻译器名在版本间可能是 "OBJ" / "OBJimport"，逐个尝试
    last_exc = None
    for typ in ("OBJ", "OBJimport"):
        try:
            cmds.file(path, i=True, type=typ, returnNewNodes=True,
                      options="groups=1;ptgroups=1;materials=0;smoothing=1;normals=1")
            after = set(cmds.ls(assemblies=True) or [])
            return sorted(after - before)
        except Exception as exc:
            last_exc = exc
    raise RuntimeError("OBJ 导入失败: {0}".format(last_exc))


# 要清掉的材质节点类型（覆盖 Maya 内置 + Arnold）。只删这些，绝不碰 mesh 等。
_MATERIAL_NODE_TYPES = (
    "lambert", "blinn", "phong", "phongE", "anisotropic",
    "standardSurface", "surfaceShader", "useShader", "materialInfo",
    "aiStandardSurface", "aiFlat", "aiMatte", "aiSkin",
)


def _purge_materials(names):
    """导入前清掉已存在的同名材质节点 + 其 shadingEngine，迫使 FBX 重建。

    不抛异常：任一环节失败都不影响后续导入。只删与传入材质名匹配的节点，
    不碰其它资产，安全。
    """
    if not names:
        return
    for nm in names:
        for node in (cmds.ls(nm, r=True) or []):
            try:
                if cmds.nodeType(node) not in _MATERIAL_NODE_TYPES:
                    continue
            except Exception:
                continue
            # 先删连到这个材质的 shadingEngine（残留的孤儿 SG 会让 mesh 连到空材质）
            try:
                for sg in (cmds.listConnections(node + ".outColor",
                                                type="shadingEngine") or []):
                    cmds.delete(sg)
            except Exception:
                pass
            try:
                cmds.delete(node)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Groom（毛发）：XGen / 交互式 Groom -> Alembic(.abc) -> 桥接到目标软件
# ---------------------------------------------------------------------------

def _ensure_abc_plugin():
    """确保 Alembic 导出/导入插件已加载（AbcExport 依赖它）。"""
    for p in ("AbcExport", "AbcImport"):
        try:
            if not cmds.pluginInfo(p, query=True, loaded=True):
                cmds.loadPlugin(p, quiet=True)
        except Exception:
            pass


def _resolve_groom_roots(sel):
    """把选择里的节点解析成一组 transform 根（作为 Alembic 的 -root）。"""
    roots = []
    for s in sel:
        if cmds.nodeType(s) == "transform":
            roots.append(s)
        else:
            parents = cmds.listRelatives(s, parent=True, fullPath=True) or []
            roots.append(parents[0] if parents else s)
    seen = set()
    out = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _convert_xgen_if_needed(sel, roots):
    """若选择里含 XGen Description，先尝试转成交互式 Groom 再导出。

    转换命令 xgmInteractiveGroomConvert 作用于当前选择；失败或拿不到结果时
    给出「手动转换」提示，绝不强行继续（避免产生孤儿节点 / 重复转换）。
    """
    xgen_types = ("xgmDescription", "xgenDescription", "xgen")
    xgen_nodes = [s for s in sel if cmds.nodeType(s) in xgen_types]
    if not xgen_nodes:
        return roots
    for xg in xgen_nodes:
        try:
            cmds.select(xg, replace=True)
            mel.eval("xgmInteractiveGroomConvert")
        except Exception as exc:
            raise BridgeError(
                "XGen 转交互式 Groom 失败 ({0})。请手动在 Modeling 菜单集下选中该 XGen "
                "Description，点击 Generate > Convert to Interactive Groom，再选中生成的 "
                "交互式 Groom 节点发送。".format(exc))
    # 转换后从当前选择重新解析根；拿不到再退回场景里的 xgmGroom
    new_sel = cmds.ls(selection=True, long=True) or []
    new_roots = _resolve_groom_roots(new_sel)
    if new_roots:
        return new_roots
    fallback = cmds.ls(type="xgmGroom", long=True) or []
    if fallback:
        return _resolve_groom_roots(fallback)
    raise BridgeError("XGen 已转换，但没找到生成的交互式 Groom 节点，请手动选中后发送。")


def export_groom(roots, path):
    """把一组根节点导出为 Alembic 缓存（当前帧 + 写入最终宽度）。

    对应 UE Groom 导入所需的曲线桥梁：
      -frameRange cf cf  : 仅当前帧（XGen 缓存导出里「Current Frame」）
      -writeFinalWidth    : 写入毛发最终宽度（保留粗细，对应「Write Final Width」）
      -root <node>        : 每个 Groom 根一个 -root（即不勾选「Multiple Transforms」）
    """
    _ensure_abc_plugin()
    cf = cmds.currentTime(query=True)
    # 注意：AbcExport 的 -j 参数本身是一段被双引号包裹的作业串，
    # 串内不能再出现双引号（否则会提前闭合 -j）。本机 payload 路径不含空格，
    # 节点名也不含空格，因此 -root / -file 的值都不加引号，由 Alembic 作业解析器按空格分词。
    job = "-frameRange {0} {0} -writeFinalWidth -dataFormat ogawa -uvWrite -stripNamespaces".format(cf)
    for r in roots:
        job += ' -root {0}'.format(r)
    job += ' -file {0}'.format(path.replace("\\", "/"))
    mel.eval('AbcExport -j "{0}"'.format(job))
    return path if os.path.exists(path) else None


def send_groom(target_app="unreal", note=""):
    """把选中的 XGen / 交互式 Groom 毛发转成 Alembic 并发送到目标软件。

    自动处理：XGen Description -> 转交互式 Groom -> 导出 Alembic ->
    通过桥接服务传输（kind=groom, format=abc），由目标软件端自动导入为 Groom 资产。
    """
    sel = cmds.ls(selection=True, long=True) or []
    if not sel:
        cmds.warning("[bridge] 没有选中任何毛发节点（请选中 XGen Description 或交互式 Groom 节点）")
        return 0
    roots = _resolve_groom_roots(sel)
    if not roots:
        cmds.warning("[bridge] 选中的对象里找不到可导出的 Groom 根节点")
        return 0

    roots = _convert_xgen_if_needed(sel, roots)

    name = roots[0].split("|")[-1].split(":")[-1]
    if _to_ascii is not None:
        name = _to_ascii(name)
    else:
        name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)

    meta = {
        "asset_name": name,
        "kind": "groom",
        "format": "abc",
        "to": {"app": target_app, "project": CONFIG["project"],
               "content_path": "/Game/Bridge/Groom"},
        "unit": {"scene_unit": "cm", "up_axis": "Y", "forward_axis": "Z", "unit_scale": 1.0},
        "options": {"mode": "update", "import_materials": True, "bind_to_character": True},
        "objects": [{"name": r.split("|")[-1], "type": cmds.nodeType(r)} for r in roots],
        "note": note,
    }
    rec = _post_transfer(meta)
    tid = rec["id"]
    # 服务端可能把中文名归一化成了拼音，以它为准
    name = rec.get("asset_name") or name

    pd = payload_dir()
    abc_name = name + ".abc"
    if pd:
        out = os.path.join(pd, tid)
        if not os.path.isdir(out):
            os.makedirs(out)
        abc = export_groom(roots, os.path.join(out, abc_name))
        if not abc:
            raise BridgeError("Alembic 导出失败，请检查 Script Editor")
    else:
        tmp = tempfile.mkdtemp(prefix="ab_groom_")
        try:
            abc = export_groom(roots, os.path.join(tmp, abc_name))
            if not abc:
                raise BridgeError("Alembic 导出失败，请检查 Script Editor")
            with open(abc, "rb") as fh:
                blob = fh.read()
            _req("PUT", "/transfers/{0}/files/{1}".format(tid, abc_name),
                 data=blob, timeout=60.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    rec = _req("POST", "/transfers/{0}/publish".format(tid), {})["transfer"]
    cmds.warning("[bridge] 已发送 Groom {0} -> {1} (rev {2})".format(
        name, target_app, rec["revision"]))
    return 1


def export_selection(path, profile):
    """导出当前选择为 FBX。

    单位：Maya 场景单位默认 cm，与 UE 一致，FBX 里也是 cm，
    所以缩放系数保持 1.0 —— 乘以 100 会让模型在 UE 里放大 100 倍。
    """
    _ensure_fbx_plugin()
    exp = profile.get("export") or {}
    up = str(exp.get("up_axis", "y")).lower()
    scale = exp.get("scale_factor", 1.0)

    _try_mel('FBXExportUpAxis "{0}"'.format(up))
    _try_mel("FBXExportScaleFactor -v {0}".format(scale),
             "FBXExportScaleFactor {0}".format(scale))
    _try_mel("FBXExportSmoothingGroups -v true")
    _try_mel("FBXExportHardEdges -v false")
    _try_mel("FBXExportTangents -v true")
    _try_mel("FBXExportInstances -v false")
    _try_mel("FBXExportBakeComplexAnimation -v false")
    _try_mel("FBXExportInputConnections -v false")

    # MEL 字符串里反斜杠是转义符，统一换成正斜杠再交给 FBXExport
    mel.eval('FBXExport -f "{0}" -s'.format(path.replace("\\", "/")))  # -s = export selected
    return path if os.path.exists(path) else None


def export_selection_obj(path):
    """导出当前选择为 OBJ（ZBrush 目标专用：ZBrush 免对话框导入只认 OBJ）。

    依赖 Maya 自带 objExport 插件（2023 内置，按需加载）。
    返回路径；失败返回 None。
    """
    try:
        if not cmds.pluginInfo("objExport", query=True, loaded=True):
            cmds.loadPlugin("objExport", quiet=True)
    except Exception:
        pass  # 已加载时 pluginInfo 也可能抛异常，继续尝试导出
    try:
        cmds.file(path, force=True,
                  options="groups=1;ptgroups=1;materials=0;smoothing=1;normals=1",
                  typ="OBJexport", es=True, pr=True)
    except Exception as exc:
        cmds.warning("[bridge] OBJ 导出失败: {0}".format(exc))
        return None
    return path if os.path.exists(path) else None


# ---------------------------------------------------------------------------
# 收发
# ---------------------------------------------------------------------------


def send_selection(target_app="unreal", note="", mode="update"):
    sel = cmds.ls(selection=True, long=True) or []
    if not sel:
        cmds.warning("[bridge] 没有选中对象")
        return 0
    name = sel[0].split("|")[-1].split(":")[-1]
    if _to_ascii is not None:
        # 中文 -> 拼音驼峰（旧实现用 isalnum 判定，中文的 isalnum 是 True，
        # 中文会被原样留下 -> 服务端 400 / 目标软件乱码）
        name = _to_ascii(name)
    else:
        name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)

    to_zbrush = (target_app == "zbrush")
    ext = "obj" if to_zbrush else "fbx"
    meta = {
        "asset_name": name,
        "kind": "models" if to_zbrush else ("skeletal_mesh" if any(
            cmds.nodeType(s) == "joint" or cmds.listRelatives(s, type="joint")
            for s in sel) else "static_mesh"),
        "format": ext,
        "to": {"app": target_app, "project": CONFIG["project"], "content_path": "/Game/Bridge"},
        "unit": {"scene_unit": "cm", "up_axis": "Y", "forward_axis": "Z", "unit_scale": 1.0},
        # Toolbag 目标时 mode = bake/render（Toolbag 适配器据此分流）；
        # 其他目标保持 update（覆盖更新）
        "options": {"mode": mode, "import_materials": True, "import_textures": True},
        "objects": [{"name": s.split("|")[-1], "type": cmds.nodeType(s)} for s in sel],
        "note": note,
    }
    rec = _post_transfer(meta)
    tid = rec["id"]
    # 服务端可能把中文名归一化成了拼音，以它为准，保证文件名与记录名一致
    name = rec.get("asset_name") or name

    def _export_model(out_path):
        if to_zbrush:
            return export_selection_obj(out_path)
        return export_selection(out_path, route_profile("maya"))

    pd = payload_dir()
    if pd:
        out = os.path.join(pd, tid)
        if not os.path.isdir(out):
            os.makedirs(out)
        fbx = _export_model(os.path.join(out, "{0}.{1}".format(name, ext)))
        if not fbx:
            raise BridgeError("模型导出失败，检查 Script Editor")
    else:
        # py2.7 没有 tempfile.TemporaryDirectory，用 mkdtemp + rmtree 代替
        tmp = tempfile.mkdtemp(prefix="ab_")
        try:
            fbx = _export_model(os.path.join(tmp, "{0}.{1}".format(name, ext)))
            if not fbx:
                raise BridgeError("模型导出失败，检查 Script Editor")
            with open(fbx, "rb") as fh:
                blob = fh.read()
            _req("PUT", "/transfers/{0}/files/{1}".format(tid, os.path.basename(fbx)),
                 data=blob, timeout=30.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    rec = _req("POST", "/transfers/{0}/publish".format(tid), {})["transfer"]
    cmds.warning("[bridge] 已发送 {0} -> {1} (rev {2})".format(
        name, target_app, rec["revision"]))
    return 1


def _texture_files_from_selection():
    """收集选中对象用到的贴图文件（file 节点的 fileTextureName）。

    优先选中对象 shadingEngine -> shader 上游的 file 节点；
    没有选中/取不到时退化为场景里所有 file 节点。
    返回去重后的 [(显示名, 绝对路径)]。
    """
    file_nodes = []
    sel = cmds.ls(selection=True, long=True) or []
    for s in sel:
        shapes = cmds.listRelatives(s, shapes=True, fullPath=True) or []
        if cmds.nodeType(s) == "mesh":
            shapes.append(s)
        for sh in shapes:
            try:
                sgs = cmds.listConnections(sh, type="shadingEngine") or []
            except Exception:
                sgs = []
            for sg in sgs:
                try:
                    shaders = cmds.listConnections(sg + ".surfaceShader",
                                                   source=True, destination=False) or []
                except Exception:
                    shaders = []
                for shd in shaders:
                    try:
                        ups = cmds.listConnections(shd, source=True,
                                                   destination=False,
                                                   type="file") or []
                    except Exception:
                        ups = []
                    for n in ups:
                        if n not in file_nodes:
                            file_nodes.append(n)
    if not file_nodes:
        try:
            file_nodes = list(cmds.ls(type="file") or [])
        except Exception:
            file_nodes = []

    out, seen = [], set()
    for node in file_nodes:
        try:
            fp = cmds.getAttr(node + ".fileTextureName")
        except Exception:
            continue
        if not fp:
            continue
        fp = os.path.abspath(fp)
        base = os.path.basename(fp)
        if base in seen or not os.path.isfile(fp):
            continue
        seen.add(base)
        out.append((node, fp))
    return out


def send_textures(target_app="photoshop", note=""):
    """把选中对象用到的贴图发给 Photoshop / SAI2（只能发贴图）。

    保存后由编辑器守护进程回传，Maya 侧自动改 fileTextureName 重载。
    """
    if target_app not in ("photoshop", "sai2"):
        cmds.warning("[bridge] 贴图发送只支持 photoshop / sai2")
        return 0
    items = _texture_files_from_selection()
    if not items:
        cmds.warning("[bridge] 没有找到可发送的贴图文件"
                     "（选中带贴图材质的对象，或场景里先有 file 节点）")
        return 0

    first = items[0][1]
    name = os.path.splitext(os.path.basename(first))[0]
    if _to_ascii is not None:
        name = _to_ascii(name)
    ext = os.path.splitext(first)[1].lstrip(".").lower() or "png"

    meta = {
        "asset_name": name,
        "kind": "textures",
        "format": ext,
        "to": {"app": target_app, "project": CONFIG["project"], "content_path": ""},
        "options": {"editor": target_app},
        "objects": [{"name": n, "type": "file"} for n, _ in items],
        "note": note,
    }
    rec = _post_transfer(meta)
    tid = rec["id"]
    name = rec.get("asset_name") or name

    pd = payload_dir()
    if pd:
        out = os.path.join(pd, tid)
        if not os.path.isdir(out):
            os.makedirs(out)
        for _n, fp in items:
            shutil.copy2(fp, os.path.join(out, os.path.basename(fp)))
    else:
        for _n, fp in items:
            with open(fp, "rb") as fh:
                blob = fh.read()
            _req("PUT", "/transfers/{0}/files/{1}".format(
                tid, os.path.basename(fp)), data=blob, timeout=30.0)

    rec = _req("POST", "/transfers/{0}/publish".format(tid), {})["transfer"]
    cmds.warning("[bridge] 已发送 {0} 张贴图 -> {1} (rev {2})，保存后自动重载".format(
        len(items), target_app, rec["revision"]))
    return len(items)


def _report(tid, status, result):
    """把导入结果交给后台线程去上报 finish（主线程只碰 Maya API）。"""
    if _result_queue is not None:
        _result_queue.put((tid, status, result))


def _find_file_node_by_source(source_file):
    """按贴图文件名找 Maya 场景里的 file 节点（保存回传重载用）。"""
    if not source_file:
        return None
    stem = os.path.splitext(source_file)[0]
    for node in (cmds.ls(type="file") or []):
        try:
            fp = cmds.getAttr(node + ".fileTextureName")
        except Exception:
            continue
        if not fp:
            continue
        base = os.path.basename(fp)
        if base == source_file or base == stem:
            return node
    return None


def _handle_textures(record):
    """主线程：处理贴图传输。

    - options.return_of（PS/SAI2 保存回传）：找到原 file 节点，
      改 fileTextureName 指向新文件（Maya 自动重载，节点连接不变）
    - 普通贴图：新建 file + place2dTexture 节点
    """
    files = [f for f in (record.get("_files_abs") or [])
             if f.lower().endswith((".png", ".psd", ".tga", ".jpg", ".jpeg",
                                    ".bmp", ".exr", ".tif", ".tiff"))]
    if not files:
        raise RuntimeError("没有可处理的图片文件")

    opts = record.get("options") or {}
    return_of = opts.get("return_of")
    source_file = str(opts.get("source_file") or "")
    editor = str(opts.get("editor") or (record.get("from") or {}).get("app") or "")

    reloaded, created = [], []
    for f in files:
        name = os.path.basename(f)
        node = _find_file_node_by_source(source_file) if return_of else None
        if node is not None:
            cmds.setAttr(node + ".fileTextureName", f, type="string")
            reloaded.append(node)
            continue
        node = cmds.shadingNode("file", asTexture=True, name="ab_bridge_{0}".format(
            os.path.splitext(name)[0]))
        cmds.setAttr(node + ".fileTextureName", f, type="string")
        try:
            p2d = cmds.shadingNode("place2dTexture", asUtility=True)
            cmds.connectAttr(p2d + ".outUV", node + ".uv")
            cmds.connectAttr(p2d + ".outUvFilterSize", node + ".uvFilterSize")
        except Exception:
            pass
        created.append(node)

    if reloaded and not created:
        msg = "{0} 保存后已重载: {1}".format(editor or "编辑器", ", ".join(reloaded))
    elif created and not reloaded:
        msg = "已创建贴图节点: {0}".format(", ".join(created))
    else:
        msg = "重载 {0}；新建 {1}".format(", ".join(reloaded), ", ".join(created))
    _report(record["id"], "done", {"message": msg, "nodes": reloaded + created})


def _handle(record):
    """在主线程执行真正的 Maya 导入（只碰 cmds），结果交后台线程上报。"""
    try:
        if str(record.get("kind", "")).lower() == "textures":
            _handle_textures(record)
            return
        files = record.get("_files_abs") or []
        fbx = [f for f in files if f.lower().endswith((".fbx", ".obj"))]
        if not fbx:
            raise RuntimeError("没有可导入的模型文件")
        src = (record.get("from") or {}).get("app", "unreal")
        # 收集记录里带的材质名（发送方 gather_objects 已采集），用于导入前清旧材质
        mat_names = []
        for _o in (record.get("objects") or []):
            for _m in (_o.get("materials") or []):
                if _m and _m not in mat_names:
                    mat_names.append(_m)
        # ZBrush 发来的都是 OBJ；按扩展名选导入器
        if fbx[0].lower().endswith(".obj"):
            new = import_obj(fbx[0])
        else:
            new = import_fbx(fbx[0], route_profile(src), material_names=mat_names)
        cmds.warning("[bridge] 收到 {0} (rev {1})".format(
            record["asset_name"], record["revision"]))
        _report(record["id"], "done",
                {"objects": new, "message": "导入 {0} 个层级".format(len(new))})
    except Exception as exc:
        cmds.warning("[bridge] 导入失败: {0}".format(exc))
        _report(record["id"], "error",
                {"message": "{0}: {1}".format(type(exc).__name__, exc)})


# ---------------------------------------------------------------------------
# 退避 / 熔断 / 日志限频（与 SP / UE 同策略）
# ---------------------------------------------------------------------------


def _on_net_success():
    """一次网络成功：复位失败计数与熔断，恢复正常轮询间隔。"""
    global _fail_streak, _circuit_open, _next_poll_ts
    _fail_streak = 0
    _circuit_open = False
    _next_poll_ts = time.time() + max(0.2, CONFIG["poll_interval"])


def _on_net_failure(what):
    """一次网络失败：指数退避 + 日志限频 + 达到阈值熔断。

    注意：本函数在后台线程调用，不能碰 cmds，只用 print。
    """
    global _fail_streak, _next_poll_ts, _last_fail_log_ts, _circuit_open, _last_probe_ts
    _fail_streak += 1
    # 指数退避：第 n 次失败的间隔 = min(1000 * 2^(n-1), 30000) ms（转秒）
    backoff = min(1000 * (2 ** (_fail_streak - 1)), 30000) / 1000.0
    _next_poll_ts = time.time() + backoff
    now = time.time()
    if now - _last_fail_log_ts >= 30.0:
        _last_fail_log_ts = now
        print("[bridge] 中转服务通信失败({0}): {1}".format(_fail_streak, what))
    if _fail_streak >= 5 and not _circuit_open:
        _circuit_open = True
        _last_probe_ts = time.time()
        print("[bridge] 中转服务不可达，已暂停轮询。请先启动 start_bridge_server.bat")


def _drain_results():
    """后台线程：把主线程回传的导入结果通过 HTTP 上报 finish（网络 IO）。"""
    if _result_queue is None:
        return
    try:
        while True:
            tid, status, result = _result_queue.get_nowait()
            try:
                _req("POST", "/transfers/{0}/finish".format(tid),
                     {"status": status, "result": result}, timeout=5.0)
            except Exception as exc:
                print("[bridge] finish 上报失败 {0}: {1}".format(tid, exc))
            _result_queue.task_done()
    except queue.Empty:
        pass


# ---------------------------------------------------------------------------
# 后台轮询线程
# ---------------------------------------------------------------------------


def _worker_loop():
    """后台 daemon 线程：所有网络 IO 都在这里，绝不阻塞 Maya 主线程。

    逻辑：
      1) 时间闸门 / 退避：距下次允许轮询未到则 ev.wait() 让出，可被 stop 唤醒；
      2) 注册重试：未拿到 sessionId 前只 register，绝不 claim；
      3) 心跳（每 30 个 tick 一次）+ 收件箱轮询 + claim，记录入队；
      4) 成功复位退避，失败指数退避 + 熔断；
      5) 顺手把主线程回传的导入结果上报 finish。
    """
    global _fail_streak, _next_poll_ts, _last_fail_log_ts, _circuit_open
    global _last_probe_ts, _session_id, _tick, _last_status_probe_ts
    ev = _stop_event
    while not ev.is_set():
        now = time.time()
        # 状态探测（与收件箱退避解耦，每 ~1s 一次，短超时，绝不在主线程）
        if now - _last_status_probe_ts > 1.0:
            _last_status_probe_ts = now
            _update_status_cache()
        wait = _next_poll_ts - now
        if wait > 0:
            ev.wait(wait)          # 退避 / 时间闸门；stop() 时立即返回
            continue

        # 注册重试（未拿 sessionId 前不 claim）
        if not _session_id:
            try:
                register()
            except Exception:
                pass
        if not _session_id:
            _on_net_failure("注册失败")
            continue

        try:
            _tick += 1
            if _tick % 30 == 1:
                _req("POST", "/sessions/{0}/heartbeat".format(_session_id), {}, timeout=1.5)
            if CONFIG["auto_receive"]:
                recs = _req("GET", "/inbox?app=maya&limit=3", timeout=2.0).get("transfers", [])
                for rec in recs:
                    full = _req("POST", "/transfers/{0}/claim".format(rec["id"]),
                                {"session_id": _session_id})["transfer"]
                    if _recv_queue is not None:
                        _recv_queue.put(full)
            _on_net_success()
        except BridgeError:
            _on_net_failure("服务不可达")
        except Exception as exc:
            _on_net_failure("轮询异常: {0}".format(exc))

        # 把主线程算好的导入结果上报（也是网络 IO，留在后台）
        _drain_results()


# ---------------------------------------------------------------------------
# 主线程：idle 回调只消费队列
# ---------------------------------------------------------------------------


def _on_idle():
    """scriptJob 的 idle 回调。

    **只做队列消费 + Maya API 调用**，绝不做任何网络请求（网络 IO 全在
    后台 worker 线程）。回调里抛异常会让 scriptJob 被 Maya 摘掉（适配器
    静默失效），所以整段包在 try/except 里，绝不外抛。
    """
    if _recv_queue is None:
        return
    try:
        while True:
            rec = _recv_queue.get_nowait()
            try:
                _handle(rec)
            except Exception as exc:
                cmds.warning("[bridge] 处理异常: {0}".format(exc))
            _recv_queue.task_done()
    except queue.Empty:
        pass

    # 桥接状态灯：每 1.5 秒刷新一次（只读缓存，不联网，面板开着才有效）
    global _last_status_ui_ts
    _now = time.time()
    if _now - _last_status_ui_ts > 1.5:
        _last_status_ui_ts = _now
        try:
            _refresh_panel_status()
        except Exception:
            pass


def start_commandport(port=7001, source_type="python"):
    """开一个 TCP commandPort，让外部脚本可以远程驱动 Maya。

    为什么需要它：Maya 2023 在这台机器上 mayapy / mayabatch 全部起不来
    （standalone.pyd 报 DLL load failed，mayabatch 退出码 0xC0000142，
    清环境变量、绕沙箱都一样）——我的进程跑在没有桌面的会话里，
    Maya 的 GUI 相关库初始化不了。交互式 Maya 本身是好的，
    所以「GUI Maya + commandPort」是唯一可行的自动化通道，
    而且它本来也是生产环境该用的接法（外部工具不必和 Maya 同进程）。

    关键：**先关后开**。Maya 的 commandPort 在「收到半句被截断的代码」
    时会卡在「未完成的语句」上（解释器一直等续行），此后所有命令都不
    再执行也不回显；而且重复执行 userSetup 时旧端口仍在占用 7001。
    所以每次启动都先按两种名称把可能存在的旧端口关掉，再开一个干净端口，
    拿到全新的解释器，彻底避免「卡死端口 / 旧端口残留」问题。
    """
    global _commandport_name
    name = "127.0.0.1:{0}".format(port)
    # ⚠️ 只关「本实例自己开过」的端口。
    # 绝不能无脑去 close 一个属于别的 Maya 实例的端口：Maya 的 `commandPort -close`
    # 会去连接那个端口，若对方解释器已卡死，本调用会**永久阻塞**（历史上正是
    # 它让新启动的 Maya 卡死在 splash 界面）。找不到自己开过的记录就什么都不关。
    if _commandport_name:
        try:
            cmds.commandPort(name=_commandport_name, close=True)
        except Exception:
            pass
        _commandport_name = None
    try:
        cmds.commandPort(name=name, sourceType=source_type, echoOutput=True)
        _commandport_name = name
        cmds.warning("[bridge] commandPort 已开启: {0} ({1})".format(name, source_type))
        return name
    except Exception as exc:
        cmds.warning("[bridge] commandPort 开启失败: {0}".format(exc))
        return ""


def _teardown():
    """彻底停止旧实例：置停止事件 + 等 worker 退出 + 杀 scriptJob。"""
    global _job_id, _worker, _stop_event, _recv_queue, _result_queue
    if _stop_event is not None:
        _stop_event.set()
    if _worker is not None and _worker.is_alive():
        _worker.join(timeout=2.0)
    _worker = None
    if _job_id is not None:
        try:
            cmds.scriptJob(kill=_job_id, force=True)
        except Exception:
            pass
        _job_id = None
    _stop_event = None
    _recv_queue = None
    _result_queue = None


def start(**overrides):
    """启动适配器：注册会话 + 后台轮询线程 + 挂 idle 消费 + 开 commandPort。

    幂等：已在运行（job 有效且 worker 存活）则直接返回，不重复注册
    scriptJob / 不重复起线程。任何一步失败都不能让 Maya 起不来。
    """
    global _job_id, _worker, _stop_event, _recv_queue, _result_queue, _maya_version
    CONFIG.update({k: v for k, v in overrides.items() if k in CONFIG})

    # 幂等守卫：已运行则直接返回（不重复注册 scriptJob / 不重复起线程）
    if _job_id is not None and _worker is not None and _worker.is_alive():
        try:
            if cmds.scriptJob(exists=_job_id):
                print("[bridge] 适配器已在运行，跳过重复启动")
                return _job_id
        except Exception:
            pass

    # 先彻底停掉旧实例，避免线程 / scriptJob 泄漏
    _teardown()
    _reset_runtime_state()

    try:
        _maya_version = cmds.about(version=True)
    except Exception:
        _maya_version = ""

    try:
        register()
    except Exception as exc:
        # 中转服务没开也要能让 Maya 正常启动，只是暂时收不到东西
        print("[bridge] 连接中转服务失败: {0}".format(exc))

    _stop_event = threading.Event()      # 本次运行独立的停止事件
    _recv_queue = queue.Queue()
    _result_queue = queue.Queue()
    _worker = threading.Thread(
        target=_worker_loop, name="asset-bridge-maya-poll", daemon=True)
    _worker.start()

    # -protected 标志在部分 Maya 版本上没有，降级后再试一次
    try:
        _job_id = cmds.scriptJob(event=["idle", _on_idle], protected=False)
    except Exception:
        _job_id = cmds.scriptJob(event=["idle", _on_idle])

    if CONFIG.get("open_command_port", False):
        start_commandport(CONFIG.get("command_port", 7001))

    # 在 AssetBridge shelf 页签补一个「面板」按钮（幂等，失败不影响桥接）
    try:
        ensure_shelf()
    except Exception:
        pass

    print("[bridge] Maya 适配器已启动")
    return _job_id


def stop():
    """停止适配器（幂等，可重复调用）。"""
    _teardown()


# ---------------------------------------------------------------------------
# 手动接收（供面板「立即接收」按钮调用，在主线程执行网络 IO）
# ---------------------------------------------------------------------------


def pull_now():
    """手动从收件箱拉取并导入（在主线程执行，供面板按钮调用）。

    把拉到的记录塞进 recv 队列，由 idle 回调在下一帧真正执行 Maya API 导入，
    所以本函数本身不碰 cmds 之外的导入逻辑，安全。
    """
    global _session_id
    if not _session_id:
        try:
            register()
        except Exception as exc:
            cmds.warning("[bridge] 连接中转服务失败: {0}".format(exc))
            return 0
    if not _session_id:
        cmds.warning("[bridge] 未连接中转服务")
        return 0
    if _recv_queue is None:
        cmds.warning("[bridge] 适配器未运行，请先点「启动桥接」")
        return 0
    n = 0
    try:
        recs = _req("GET", "/inbox?app=maya&limit=5", timeout=3.0).get("transfers", [])
        for rec in recs:
            try:
                full = _req("POST", "/transfers/{0}/claim".format(rec["id"]),
                            {"session_id": _session_id})["transfer"]
            except Exception as exc:
                cmds.warning("[bridge] claim 失败 {0}: {1}".format(rec["id"], exc))
                continue
            _recv_queue.put(full)
            n += 1
    except BridgeError as exc:
        cmds.warning("[bridge] 接收失败: {0}".format(exc))
        return 0
    except Exception as exc:
        cmds.warning("[bridge] 接收异常: {0}".format(exc))
        return 0
    cmds.warning("[bridge] 已拉取 {0} 条待导入记录（将自动导入）".format(n))
    return n


# ---------------------------------------------------------------------------
# 控制面板（窗口）+ shelf 按钮
# ---------------------------------------------------------------------------


def _ui_start():
    start()
    cmds.warning("[bridge] 桥接已启动")


def _ui_stop():
    stop()
    cmds.warning("[bridge] 桥接已停止")


def _ui_send(tgt_menu):
    tgt = cmds.optionMenu(tgt_menu, query=True, value=True) or "unreal"
    note = cmds.textFieldGrp("abNoteField", query=True, text=True) or ""
    mode = "update"
    if tgt == "toolbag" and cmds.optionMenu("abToolbagModeMenu", exists=True):
        mode = cmds.optionMenu("abToolbagModeMenu", query=True, value=True) or "bake"
    try:
        n = send_selection(target_app=tgt, note=note, mode=mode)
    except Exception as exc:
        cmds.warning("[bridge] 发送失败: {0}".format(exc))
        return
    cmds.warning("[bridge] 已发送 {0} 个对象 -> {1}".format(n, tgt))


def _ui_send_textures(tgt_menu):
    tgt = cmds.optionMenu(tgt_menu, query=True, value=True) or "photoshop"
    note = cmds.textFieldGrp("abNoteField", query=True, text=True) or ""
    try:
        n = send_textures(target_app=tgt, note=note)
    except Exception as exc:
        cmds.warning("[bridge] 贴图发送失败: {0}".format(exc))
        return
    if n:
        cmds.warning("[bridge] 已发送 {0} 张贴图 -> {1}（保存后自动重载）".format(n, tgt))


def _ui_send_groom(tgt_menu):
    tgt = cmds.optionMenu(tgt_menu, query=True, value=True) or "unreal"
    try:
        n = send_groom(target_app=tgt)
    except Exception as exc:
        cmds.warning("[bridge] 发送毛发失败: {0}".format(exc))
        return
    cmds.warning("[bridge] 已发送 {0} 个毛发资产 -> {1}".format(n, tgt))


def _ui_pull():
    try:
        n = pull_now()
    except Exception as exc:
        cmds.warning("[bridge] 接收失败: {0}".format(exc))
        return
    cmds.warning("[bridge] 拉取 {0} 条".format(n))


def _refresh_panel_status():
    """刷新面板上的连接状态灯（文字 + 背景色）。面板关着时静默跳过。

    运行态由主线程判断（_bridge_running 会碰 cmds，必须主线程）；服务器可达性
    读后台 worker 写入的 _STATUS_CACHE（绝不在此发起网络请求，否则没开服务器时
    主线程会卡在 /health 超时上）。
    """
    if not cmds.text("abStatusText", exists=True):
        return
    try:
        if not _bridge_running():
            label, rgb = "● 桥接未启动", (0.55, 0.32, 0.08)
        else:
            label, rgb = _STATUS_CACHE["text"], _STATUS_CACHE["rgb"]
    except Exception:
        label, rgb = "● 状态未知", (0.4, 0.4, 0.4)
    try:
        cmds.text("abStatusText", edit=True, label=label, backgroundColor=rgb)
    except Exception:
        pass


def _panel_idle():
    """面板专用 idle 回调：与桥接启停**解耦**，保证状态灯实时（含停止后变红）。

    旧实现把刷新挂在适配器的 idle scriptJob 上，一旦「停止桥接」job 被杀，
    面板就再也刷新不了，灯永远停在绿色。
    """
    global _panel_status_ts
    now = time.time()
    if now - _panel_status_ts < 1.5:
        return
    _panel_status_ts = now
    try:
        _refresh_panel_status()
    except Exception:
        pass


def show_panel():
    """打开 AssetBridge 控制面板：启动/停止、发送(目标+备注)、接收，Separator 拆分。

    点开面板时若桥接还没跑，会**自动启动一次**（免去每次手动点「启动桥接」）。
    这是「默认开启」的落地方式 —— 刻意不写进 userSetup.py，避免影响 Maya 启动。
    """
    try:
        if not _bridge_running():
            start()
            print("[bridge] Maya 桥接已自动开启")
    except Exception as exc:
        cmds.warning("[bridge] 自动启动桥接失败: {0}".format(exc))

    name = "assetBridgePanel"
    if cmds.window(name, exists=True):
        cmds.deleteUI(name)
    w = cmds.window(name, title="Asset Bridge 资产桥", widthHeight=(280, 250),
                    retain=False)

    # 面板专用 idle 刷新器：parent=窗口，窗口关闭时自动清理；
    # 与桥接的启停解耦，所以「停止桥接」后状态灯也能立刻变红。
    global _panel_job
    try:
        if _panel_job is not None:
            try:
                cmds.scriptJob(kill=_panel_job, force=True)
            except Exception:
                pass
            _panel_job = None
        _panel_job = cmds.scriptJob(event=["idle", _panel_idle], parent=w)
    except Exception:
        _panel_job = None

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4, columnAttach=("both", 8))
    cmds.text(label="Asset Bridge 资产桥", align="left", font="boldLabelFont")
    cmds.separator(style="none", height=4)

    # 桥接服务器连接状态（文字 + 背景色）
    # 注意 1：Maya 的 button 控件没有 backgroundColor/bgColor，状态灯必须用 text 实现。
    # 注意 2：rowLayout 的 columnAttach 只接受单个 (int,str,int)，这里干脆不用 rowLayout，
    #         直接竖向堆叠，避免各种 flag 兼容问题。
    cmds.text(label="桥接服务器连接状态:", align="left")
    cmds.text("abStatusText", label="● 检测中…", backgroundColor=(0.4, 0.4, 0.4),
              height=22, align="center")
    cmds.button(label="刷新连接状态", command=lambda *a: _refresh_panel_status())

    cmds.frameLayout(label="桥接", collapsable=False, marginWidth=6, marginHeight=6)
    cmds.button(label="启动桥接", command=lambda *a: _ui_start())
    cmds.button(label="停止桥接", command=lambda *a: _ui_stop())
    cmds.setParent("..")

    cmds.separator(style="single", height=8)  # Separator 拆分

    cmds.frameLayout(label="发送（选中对象）", collapsable=False,
                    marginWidth=6, marginHeight=6)
    tgt = cmds.optionMenu("abTargetMenu", label="目标软件")
    for _app in ("unreal", "blender", "substance_painter", "maya",
                 "toolbag", "photoshop", "sai2", "zbrush"):
        cmds.menuItem(label=_app)
    # Toolbag 模式（仅目标为 toolbag 时生效）：烘焙=建烘焙组+烘焙回传SP；渲染=只导入
    cmds.optionMenu("abToolbagModeMenu", label="Toolbag 模式")
    cmds.menuItem(label="bake")
    cmds.menuItem(label="render")
    cmds.textFieldGrp("abNoteField", label="备注", text="")
    cmds.button(label="发送选中对象", command=lambda *a: _ui_send(tgt))
    cmds.text(label="备注随资产发给对方，仅作说明(给人看)", align="left")
    cmds.setParent("..")

    cmds.separator(style="single", height=8)

    cmds.frameLayout(label="发送贴图（Photoshop / SAI2）", collapsable=False,
                    marginWidth=6, marginHeight=6)
    cmds.text(label="用上面的目标菜单选 photoshop / sai2；"
                    "在编辑器里保存后 Maya 自动重载", align="left")
    cmds.button(label="发送贴图到编辑器", command=lambda *a: _ui_send_textures(tgt))
    cmds.setParent("..")

    cmds.separator(style="single", height=8)

    cmds.frameLayout(label="发送毛发 (Groom / XGen)", collapsable=False,
                    marginWidth=6, marginHeight=6)
    cmds.text(label="选中 XGen Description 或交互式 Groom 节点，导出 Alembic 发往 UE",
              align="left")
    gtgt = cmds.optionMenu("abGroomTargetMenu", label="目标软件")
    for _app in ("unreal", "blender"):
        cmds.menuItem(label=_app)
    cmds.button(label="发送毛发到UE", command=lambda *a: _ui_send_groom(gtgt))
    cmds.setParent("..")

    cmds.separator(style="single", height=8)

    cmds.frameLayout(label="接收", collapsable=False, marginWidth=6, marginHeight=6)
    cmds.button(label="立即接收", command=lambda *a: _ui_pull())
    cmds.setParent("..")

    _refresh_panel_status()
    cmds.showWindow(w)


_ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assetbridge.png")


def _icon_arg():
    """shelfButton 的 image 参数。

    优先返回随插件分发的 PNG **绝对路径**（正斜杠）——这样即使 Maya 的图标
    查找路径（MAYA_ICONS / prefs/icons）里没有它，也能正常显示，不再报
    「找不到像素图文件 assetbridge.png」。
    """
    try:
        if os.path.isfile(_ICON_FILE):
            return _ICON_FILE.replace("\\", "/")
    except Exception:
        pass
    return "assetbridge.png"


def ensure_shelf(shelf=None):
    """在 Maya shelf 上加一个「打开面板」按钮（幂等）。

    :param shelf: 目标页签名。None = 加到**当前正在显示的那个 shelf** 上，
                  保证按钮出现在用户看得见的地方；也可显式传名字，例如
                  ``ensure_shelf("AssetBridge")``。

    取不到当前 shelf 时，退而找含「启动桥接/停止桥接」的 shelf；再找不到才新建
    AssetBridge 页签。不会重复你已有的启动/停止按钮。
    """
    # 1) 优先：当前正在显示的 shelf 页签
    if not shelf:
        try:
            shelf = cmds.shelfTabLayout("ShelfLayout", query=True, selectTab=True)
        except Exception:
            shelf = None

    # 2) 退而求其次：找含 启动桥接/停止桥接 的 shelf
    if not shelf:
        try:
            shelves = cmds.shelfTabLayout("ShelfLayout", query=True, childArray=True) or []
        except Exception:
            try:
                shelves = cmds.layout("ShelfLayout", query=True, childArray=True) or []
            except Exception:
                shelves = []
        for sh in shelves:
            try:
                kids = cmds.shelfLayout(sh, query=True, childArray=True) or []
            except Exception:
                continue
            for k in kids:
                try:
                    lbl = cmds.control(k, query=True, label=True) or ""
                except Exception:
                    lbl = ""
                if lbl in ("启动桥接", "停止桥接"):
                    shelf = sh
                    break
            if shelf:
                break

    # 3) 还找不到就新建 AssetBridge 页签
    target = shelf or "AssetBridge"
    if not cmds.shelfLayout(target, exists=True):
        try:
            cmds.shelfLayout(target, parent="ShelfLayout")
        except Exception as exc:
            cmds.warning("[bridge] 无法创建 shelf 页签: {0}".format(exc))
            return None

    cmds.setParent(target)
    try:
        existing = set(cmds.shelfLayout(target, query=True, childArray=True) or [])
    except Exception:
        existing = set()

    def _has(label):
        for _c in existing:
            try:
                if cmds.control(_c, query=True, label=True) == label:
                    return True
            except Exception:
                pass
        return False

    # 关键：shelf 按钮的 command 必须是**字符串**（MEL 或 Python 源码），
    # 绝不能传 lambda / 可调用对象 —— Maya 会把它序列化成
    # "<function ... at 0x...>" 写进 shelf 文件，点击时按 MEL 执行就报
    # "invalid syntax"（实际踩过的坑）。
    need_create = True
    for _c in list(existing):
        try:
            if cmds.shelfButton(_c, query=True, label=True) != "打开面板":
                continue
        except Exception:
            continue
        # 已存在：命令退化成函数 repr 的话，删掉重建
        try:
            _cmd = cmds.shelfButton(_c, query=True, command=True) or ""
        except Exception:
            _cmd = ""
        if (not _cmd.strip()) or ("<function" in _cmd) or ("lambda" in _cmd):
            try:
                cmds.deleteUI(_c)
            except Exception:
                pass
        else:
            need_create = False

    if need_create:
        _cmd_py = "import asset_bridge_maya; asset_bridge_maya.show_panel()"
        _icon = _icon_arg()
        _anno = "打开 AssetBridge 控制面板（启动/停止/发送/接收）"
        try:
            # 本机 shelf 样式是 iconOnly：用 imageOverlayLabel 把字叠在图标上才看得到
            cmds.shelfButton(label="打开面板", annotation=_anno, sourceType="python",
                             command=_cmd_py, image=_icon, image1=_icon,
                             imageOverlayLabel="面板")
        except Exception:
            try:
                cmds.shelfButton(label="打开面板", annotation=_anno, sourceType="python",
                                 command=_cmd_py, image=_icon)
            except Exception:
                # 老版本 Maya 没有 sourceType：退回用 MEL 的 python() 包一层
                cmds.shelfButton(label="打开面板", annotation=_anno,
                                 command='python("{0}")'.format(_cmd_py),
                                 image=_icon)

    # 让新按钮立刻可见：把 shelf 切到自身，触发 Shelf 重绘
    try:
        cmds.shelfTabLayout("ShelfLayout", edit=True, selectTab=target)
    except Exception:
        pass
    # 持久化到 shelf 文件，这样重启 Maya 后按钮依然在
    try:
        cmds.saveShelf(target)
    except Exception:
        pass
    cmds.warning("[bridge] 已在 shelf「{0}」上放置「打开面板」按钮".format(target))
    return target
