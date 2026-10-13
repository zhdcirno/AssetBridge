#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Toolbag 5 适配器无头测试。

不启动 Toolbag（GUI 进程在无显示会话里起不来），而是：
  1) 起一个真的中转服务（独立端口 + 临时数据根）
  2) 往 sys.modules 注入一个假 mset 模块（复刻 Toolbag 5 已确认的 API 面：
     BakerObject / getMap / addGroup / importModel / bake / importMaterial /
     frameObject / callbacks.onPeriodicUpdate / UI*）
  3) 用 importlib 从文件加载 plugins/toolbag/asset_bridge/__main__.py
  4) 灌入 bake / render 两种 transfer，驱动轮询状态机直到完成，断言：
     - 原 transfer 被 claim 并 finish(done)
     - bake 模式回传了一条 kind=textures 给 substance_painter 且带烘焙文件
     - render 模式只导入不回传
     - 服务不可达时退避不炸（轮询函数可安全重复调用）

用法： python tests/test_toolbag_adapter.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import server as bridge_server  # noqa: E402

PORT = 27699
BASE = "http://127.0.0.1:{0}".format(PORT)
PLUGIN = ROOT / "plugins" / "toolbag" / "asset_bridge" / "__main__.py"

PASSED, FAILED = [], []


# ---------------------------------------------------------------------------
# 假 mset（按 Toolbag 5 真实 API 形状复刻）
# ---------------------------------------------------------------------------

class _FakeBakerMap:
    def __init__(self, name):
        self.name = name
        self.enabled = False
        self.suffix = name.lower().replace(" ", "")


class _FakeBakerObject:
    """同时充当「场景里已有的 Baker」和「新建的 Baker」。"""

    _instances = []

    def __init__(self):
        self.outputPath = ""
        self.outputWidth = 0
        self.outputHeight = 0
        self.outputBits = 0
        self.outputSamples = 0
        self.outputSinglePsd = True
        self.imported = []
        self.groups = []
        self.bake_calls = 0
        self._maps = {}
        for n in ("Normals", "Ambient Occlusion", "Curvature", "Thickness",
                  "Position", "Bent Normal", "UV Island", "Wireframe", "Height"):
            self._maps[n] = _FakeBakerMap(n)
        _FakeBakerObject._instances.append(self)

    # -- baker API --
    def getMap(self, name):
        if name not in self._maps:
            raise RuntimeError("Map could not be found in BakerObject!")
        return self._maps[name]

    def addGroup(self, name):
        grp = _FakeGroup(name)
        self.groups.append(grp)
        return grp

    def importModel(self, path):
        self.imported.append(path)
        return _FakeSceneObject(os.path.basename(path))

    def bake(self):
        self.bake_calls += 1
        # 模拟异步烘焙：把输出文件写到 outputPath 所在目录
        if not self.outputPath:
            raise RuntimeError("Bake failed - check output path to see if it's valid.")
        d = os.path.dirname(self.outputPath)
        stem = os.path.splitext(os.path.basename(self.outputPath))[0]
        for suffix in ("norms", "ao"):
            with open(os.path.join(d, "{0}_{1}.png".format(stem, suffix)), "wb") as fh:
                fh.write(b"fakepng")


class _FakeGroup:
    def __init__(self, name):
        self.name = name


class _FakeSceneObject:
    def __init__(self, name):
        self.name = name


class _FakeCallbacks:
    def __init__(self):
        self.onPeriodicUpdate = None
        self.onSceneLoaded = None
        self.onSceneChanged = None
        self.onRegainFocus = None
        self.onShutdownPlugin = None


class _FakeMsetModule:
    """假 mset：属性按 Toolbag 5 实际存在的名字给。"""

    def __init__(self):
        self.BakerObject = _FakeBakerObject
        self.MeshObject = _FakeSceneObject
        self.callbacks = _FakeCallbacks()
        self.imported_models = []
        self.imported_materials = []

    # 模块级函数 --
    def getAllObjects(self):
        return list(_FakeBakerObject._instances)

    def getToolbagVersion(self):
        return 5033

    def importModel(self, path):
        self.imported_models.append(path)
        return _FakeSceneObject(os.path.basename(path))

    def importMaterial(self, path, name=""):
        self.imported_materials.append(path)
        return object()

    def frameObject(self, obj):
        return None

    def log(self, msg):
        pass

    # UI（全部空转，不在无头环境渲染） --
    def UIWindow(self, title):
        class _W:
            def __init__(self):
                self.visible = False
                self.elements = []

            def addElement(self, el):
                self.elements.append(el)

            def addReturn(self):
                pass
        return _W()

    def UILabel(self, text):
        return {"type": "label", "text": text}

    def UITextField(self):
        class _T:
            def __init__(self):
                self.value = ""
        return _T()

    def UIButton(self, text):
        class _B:
            def __init__(self):
                self.onClick = None
                self.text = text
        return _B()


# ---------------------------------------------------------------------------
# HTTP 辅助（绕过系统代理）
# ---------------------------------------------------------------------------

import urllib.request  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=10):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(BASE + "/api/v1" + path, data=data,
                               headers=headers, method=method)
    with _OPENER.open(r, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def check(name, fn):
    try:
        info = fn()
        PASSED.append(name)
        print("  [OK]   {0}{1}".format(name, "  -- " + str(info) if info else ""))
        return True
    except Exception as exc:
        FAILED.append((name, exc))
        print("  [FAIL] {0}  -- {1}: {2}".format(name, type(exc).__name__, exc))
        return False


def start_server(root: Path):
    bridge_server.run_server(root, "127.0.0.1", PORT)
    # run_server 里有 serve_forever，放线程里跑


def wait_port(timeout=10.0):
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.1)
    return False


def load_plugin():
    """把假 mset 塞进 sys.modules，再从文件加载适配器。"""
    fake = _FakeMsetModule()
    sys.modules["mset"] = fake
    spec = importlib.util.spec_from_file_location("ab_toolbag_plugin", str(PLUGIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, fake


def make_fbx(path: Path, name: str = "SM_TestCube.fbx"):
    fp = path / name
    fp.write_bytes(b"FakeFBX-binary-content")
    return fp


def publish_model(tmp: Path, mode: str, name: str = "SM_TestCube"):
    """建一条发往 toolbag 的模型 transfer 并发布，返回 (tid, fbx_path)。"""
    rec = req("POST", "/transfers", {
        "asset_name": name,
        "kind": "static_mesh",
        "format": "fbx",
        "to": {"app": "toolbag", "project": "", "content_path": ""},
        "options": {"mode": mode},
        "note": "test",
    })
    tid = rec["transfer"]["id"]
    fbx = make_fbx(tmp)
    # 直接落盘到服务端负载目录（同机直写，和 Blender direct_write 一致）
    dest = Path(rec_root_payload()) / tid
    dest.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(str(fbx), str(dest / fbx.name))
    req("POST", "/transfers/{0}/publish".format(tid), {})
    return tid


_REC_ROOT = {"payload": ""}


def rec_root_payload():
    if not _REC_ROOT["payload"]:
        cfg = req("GET", "/config")
        _REC_ROOT["payload"] = cfg["payload_dir"]
    return _REC_ROOT["payload"]


def drive(mod, until, timeout=30.0):
    """反复调用轮询 + 推进烘焙状态机，直到 until() 为真或超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        mod._poll_once(force=True)
        if mod._S.job is not None:
            mod._advance_bake()
        if until():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    print("\n=== Toolbag 适配器无头测试 ({0}) ===\n".format(PLUGIN))

    tmp = Path(tempfile.mkdtemp(prefix="ab_toolbag_test_"))
    t = threading.Thread(target=start_server, args=(tmp,), daemon=True)
    t.start()
    if not wait_port():
        print("服务没能起来")
        return 1

    os.environ["ASSET_BRIDGE_URL"] = BASE
    mod, fake = load_plugin()

    # 加快测试节奏
    mod.BAKE_SETTLE_SEC = 0.05

    def transfer(tid):
        return req("GET", "/transfers/{0}".format(tid))

    # -- bake 模式 ---------------------------------------------------------
    tid_bake = publish_model(tmp, "bake", "SM_BakeCase")
    ok = drive(mod, lambda: transfer(tid_bake)["status"] in ("done", "error"))
    rec_bake = transfer(tid_bake)

    def _bake_ok():
        assert ok, "bake transfer 未在超时内完成"
        assert rec_bake["status"] == "done", "状态应为 done，实际 {0}: {1}".format(
            rec_bake["status"], rec_bake.get("result"))
        # 回传给 SP 的 textures transfer
        trs = req("GET", "/transfers?limit=50")["transfers"]
        sp = [x for x in trs if x["to"]["app"] == "substance_painter"
              and x["kind"] == "textures"]
        assert sp, "没有回传给 SP 的 textures transfer"
        r = sp[0]
        assert r["status"] == "queued", "回传 transfer 应已发布，实际 {0}".format(r["status"])
        assert any(f.endswith(".png") for f in r["files"]), "回传缺少烘焙贴图: {0}".format(r["files"])
        bakers = _FakeBakerObject._instances
        assert bakers, "假 Baker 没被创建/使用"
        b = bakers[-1]
        assert b.imported, "baker.importModel 没被调用"
        assert b.groups, "baker.addGroup 没被调用（烘焙组）"
        assert b.bake_calls >= 1, "baker.bake() 没被调用"
        return "回传 {0} 张贴图 -> SP，bake_calls={1}, groups={2}".format(
            len([f for f in r["files"] if f.endswith(".png")]), b.bake_calls, len(b.groups))

    check("bake 模式：导入+烘焙组+烘焙+回传SP", _bake_ok)

    # -- render 模式 -------------------------------------------------------
    tid_render = publish_model(tmp, "render", "SM_RenderCase")
    ok2 = drive(mod, lambda: transfer(tid_render)["status"] in ("done", "error"))
    rec_render = transfer(tid_render)

    def _render_ok():
        assert ok2, "render transfer 未在超时内完成"
        assert rec_render["status"] == "done", "状态应为 done: {0}".format(rec_render)
        assert fake.imported_models, "mset.importModel 没被调用"
        # render 模式不应产生任何回传
        trs = req("GET", "/transfers?limit=50")["transfers"]
        sp = [x for x in trs if x["to"]["app"] == "substance_painter"
              and x["asset_name"] == "SM_RenderCase"]
        assert not sp, "render 模式不应回传 SP"
        return "importModel x{0}".format(len(fake.imported_models))

    check("render 模式：仅导入不回传", _render_ok)

    # -- 幂等/安全：重复轮询不炸 -------------------------------------------
    def _idempotent():
        for _ in range(5):
            mod._poll_once(force=True)
            if mod._S.job is not None:
                mod._advance_bake()
        return "5 次空轮询无异常"

    check("空转轮询安全（无任务时）", _idempotent)

    # -- textures 类型应被拒绝而非卡死 --------------------------------------
    rec = req("POST", "/transfers", {
        "asset_name": "T_WrongKind",
        "kind": "textures",
        "format": "png",
        "to": {"app": "toolbag", "project": "", "content_path": ""},
        "options": {},
    })
    tid_tex = rec["transfer"]["id"]
    req("POST", "/transfers/{0}/publish".format(tid_tex), {})

    def _tex_reject():
        ok3 = drive(mod, lambda: transfer(tid_tex)["status"] in ("done", "error"))
        assert ok3
        r = transfer(tid_tex)
        assert r["status"] == "error", "textures 应被拒绝，实际 {0}".format(r["status"])
        return r.get("result", {}).get("message", "")

    check("收到贴图类型：明确报错不卡死", _tex_reject)

    # -- 服务不可达：退避不炸 ----------------------------------------------
    def _down_safe():
        saved = mod.SERVER_URL
        mod.SERVER_URL = "http://127.0.0.1:1"   # 必然连不上
        try:
            for _ in range(3):
                mod._poll_once(force=True)
            assert mod._S.fail_streak > 0, "失败计数没涨"
        finally:
            mod.SERVER_URL = saved
        return "fail_streak={0}".format(mod._S.fail_streak)

    check("服务不可达：退避计数且不抛异常", _down_safe)

    print("\n=== 结果: {0} 通过 / {1} 失败 ===\n".format(len(PASSED), len(FAILED)))
    for name, exc in FAILED:
        print("  ✗ {0}: {1}".format(name, exc))
    try:
        req("POST", "/shutdown", {})
    except Exception:
        pass
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
