#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blender 适配器「贴图收发」无头测试（不启动 Blender）。

Blender 自带 Python 不在沙箱里，所以注入一个最小假 bpy（只实现被测函数
用到的 API 面），把 plugins/blender/asset_bridge_blender 当包加载，验证：

  1. _import_textures：PS/SAI2 保存回传（options.return_of）时，
     找到原图 datablock -> 改 filepath -> reload()（材质节点不用重连）
  2. _import_textures：普通贴图传输 -> 载入为新图片
  3. _import_textures：没有图片文件 -> error 不抛异常
  4. _find_image_by_source：按 basename / 名字匹配原图
  5. _gather_images：优先选中对象材质里的 TEX_IMAGE 节点，
     无选中时退化到场景 FILE 来源图片
  6. _resolve_image_file：文件来源直接用；打包图走 save_reload；
     都没有 -> None（调用方跳过）
  7. create_transfer 会带 from.project（编辑器回传要靠它定位工程）

用法： python tests/test_blender_textures.py
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugins" / "blender" / "asset_bridge_blender"

PASSED, FAILED = [], []


# ---------------------------------------------------------------------------
# 最小假 bpy
# ---------------------------------------------------------------------------

class _FakeImage:
    def __init__(self, name, filepath="", source="FILE", packed=False):
        self.name = name
        self.filepath = filepath
        self.source = source
        self.packed_file = object() if packed else None
        self.reload_calls = 0
        self.save_reload_target = None

    def reload(self):
        self.reload_calls += 1

    def save_reload(self, filepath):
        self.save_reload_target = filepath
        Path(filepath).write_bytes(b"unpacked-png")


class _FakeImages:
    def __init__(self):
        self.items = []
        self.loaded = []

    def __iter__(self):
        return iter(self.items)

    def load(self, filepath, check_existing=True):
        img = _FakeImage(Path(filepath).stem, filepath=str(filepath))
        self.items.append(img)
        self.loaded.append(str(filepath))
        return img


class _FakeData:
    def __init__(self):
        self.images = _FakeImages()


def _make_bpy():
    bpy = types.ModuleType("bpy")

    class _AnyTypes:
        """任意 bpy.types.X 都返回一个空类（够注释/基类用）。"""

        def __getattr__(self, name):
            return type(name, (), {})

    class _Operator:
        pass

    def _prop(*args, **kwargs):
        return None

    props = types.SimpleNamespace(
        StringProperty=_prop, EnumProperty=_prop, BoolProperty=_prop,
        FloatProperty=_prop, IntProperty=_prop,
    )
    data = _FakeData()
    path = types.SimpleNamespace(abspath=lambda p: os.path.abspath(p) if p else "")
    bpy.types = _AnyTypes()
    bpy.props = props
    bpy.data = data
    bpy.path = path
    bpy.app = types.SimpleNamespace(version=(5, 0, 1))
    bpy.context = types.SimpleNamespace()
    return bpy, data


def load_plugin(bpy):
    """把假 bpy 装进 sys.modules，按包路径加载插件子模块。"""
    sys.modules["bpy"] = bpy
    pkg_name = "ab_blender_plugin"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[pkg_name] = pkg
    operators = importlib.import_module(pkg_name + ".operators")
    bridge_client = importlib.import_module(pkg_name + ".bridge_client")
    return operators, bridge_client


# ---------------------------------------------------------------------------
# 辅助
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


def make_texture_record(files, options=None, from_app="photoshop"):
    return {
        "id": "t_ret_1",
        "asset_name": "Wood",
        "kind": "textures",
        "files": [os.path.basename(f) for f in files],
        "_files_abs": list(files),
        "from": {"app": from_app, "project": "TestProj"},
        "to": {"app": "blender", "project": "TestProj"},
        "options": options or {},
        "revision": 2,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    print("\n=== Blender 贴图收发无头测试 ===\n")
    tmp = Path(tempfile.mkdtemp(prefix="ab_blender_tex_"))
    bpy, data = _make_bpy()
    operators, bridge_client = load_plugin(bpy)

    new_png = tmp / "Wood.png"
    new_png.write_bytes(b"photoshop-saved")

    # 场景里已有一张 Wood.png（之前发去 PS 的那张）
    orig = _FakeImage("Wood.png", filepath=str(tmp / "payload_old" / "Wood.png"))
    data.images.items.append(orig)

    # -- 1. PS 保存回传 -> 重载原图 ----------------------------------------
    def _reload_back():
        rec = make_texture_record([str(new_png)], {
            "return_of": "t_send_1",
            "source_file": "Wood.png",
            "editor": "photoshop",
        })
        status, result = operators._import_textures(rec)
        assert status == "done", "{0} / {1}".format(status, result)
        assert orig.reload_calls == 1, "原图没有被 reload"
        assert orig.filepath == str(new_png), "filepath 没更新: {0}".format(orig.filepath)
        assert "重载" in result.get("message", ""), result
        assert data.images.loaded == [], "回传不应新载入图片"
        return "reload x1，材质节点自动跟上（同一 datablock）"

    check("PS 保存回传：原图自动重载", _reload_back)

    # -- 2. 普通贴图 -> 新载入 ----------------------------------------------
    def _plain_load():
        rec = make_texture_record([str(new_png)], {}, from_app="toolbag")
        status, result = operators._import_textures(rec)
        assert status == "done", "{0} / {1}".format(status, result)
        assert data.images.loaded == [str(new_png)], data.images.loaded
        assert "载入" in result.get("message", ""), result
        return "load x1"

    check("普通贴图传输：载入为新图片", _plain_load)

    # -- 3. 无图片文件 -> error ---------------------------------------------
    def _no_files():
        rec = make_texture_record([], {"return_of": "x"})
        status, result = operators._import_textures(rec)
        assert status == "error", status
        assert "没有图片文件" in result.get("message", ""), result
        return result["message"]

    check("贴图传输无图片文件：明确报错", _no_files)

    # -- 4. _find_image_by_source 匹配规则 ----------------------------------
    def _find():
        # 独立场景：只有 orig 一张图，避免被上面的新载入污染
        data.images.items = [orig]
        assert operators._find_image_by_source("Wood.png") is orig, "basename 匹配失败"
        # 名字匹配：加一张名为 Wood 的图
        named = _FakeImage("Wood", filepath="")
        data.images.items = [orig, named]
        assert operators._find_image_by_source("Wood") is named, "名字匹配失败"
        # basename 优先于名字：名为 Wood 的图在前也应命中 orig
        data.images.items = [named, orig]
        assert operators._find_image_by_source("Wood.png") is orig, "basename 未优先"
        assert operators._find_image_by_source("Nope.png") is None
        assert operators._find_image_by_source("") is None
        return "basename 优先 / 名字兜底 / 空值安全"

    check("_find_image_by_source：按源文件名找原图", _find)

    # -- 5. _gather_images ---------------------------------------------------
    def _gather():
        class _Node:
            def __init__(self, img):
                self.type = "TEX_IMAGE"
                self.image = img

        class _Tree:
            def __init__(self, nodes):
                self.nodes = nodes

        class _Mat:
            def __init__(self, nodes):
                self.use_nodes = True
                self.node_tree = _Tree(nodes)

        class _Slot:
            def __init__(self, mat):
                self.material = mat

        class _Obj:
            def __init__(self, slots):
                self.material_slots = slots

        img_a, img_b = _FakeImage("A.png"), _FakeImage("B.png")
        ctx = types.SimpleNamespace(selected_objects=[
            _Obj([_Slot(_Mat([_Node(img_a), _Node(img_b), _Node(img_a)]))]),
        ])
        got = operators._gather_images(ctx)
        assert [i.name for i in got] == ["A.png", "B.png"], [i.name for i in got]
        # 无选中 -> 退化到场景 FILE 图片
        ctx2 = types.SimpleNamespace(selected_objects=[])
        got2 = operators._gather_images(ctx2)
        assert orig in got2, "退化收集漏了场景图片"
        assert img_a not in got2, "退化收集不该带上未加入场景的图"
        return "材质节点优先 + 去重；无选中退化到场景图片"

    check("_gather_images：材质节点优先，无选中退化", _gather)

    # -- 6. _resolve_image_file ----------------------------------------------
    def _resolve():
        with tempfile.TemporaryDirectory() as td:
            # 文件来源
            fp = Path(td) / "Real.png"
            fp.write_bytes(b"x")
            img_file = _FakeImage("Real.png", filepath=str(fp))
            assert operators._resolve_image_file(img_file, td) == fp
            # 打包图 -> save_reload
            img_packed = _FakeImage("Packed", filepath="", packed=True)
            out = operators._resolve_image_file(img_packed, td)
            assert out is not None and out.is_file(), "打包图没存出来"
            assert img_packed.save_reload_target == str(out)
            # 都没有 -> None
            img_none = _FakeImage("Ghost", filepath=str(Path(td) / "missing.png"))
            assert operators._resolve_image_file(img_none, td) is None
        return "文件直接用 / 打包转存 / 缺失返回 None"

    check("_resolve_image_file：三种来源", _resolve)

    # -- 7. create_transfer 带 from.project ----------------------------------
    def _from_project():
        captured = {}

        class _C:
            base_url = "http://127.0.0.1:1"
            app = "blender"
            version = "5.0.1"
            project = "TestProj"

            def _request(self, method, path, body=None, **kw):
                captured["body"] = body
                return {"transfer": dict(body, id="t_new")}

        c = _C()
        c.create_transfer = bridge_client.BridgeClient.create_transfer.__get__(c)
        rec = c.create_transfer({"asset_name": "Wood", "kind": "textures"})
        assert rec["id"] == "t_new"
        assert captured["body"]["from"]["project"] == "TestProj", captured["body"]["from"]
        return "from.project=TestProj（编辑器回传定位用）"

    check("create_transfer 携带 from.project", _from_project)

    print("\n=== 结果: {0} 通过 / {1} 失败 ===\n".format(len(PASSED), len(FAILED)))
    for name, exc in FAILED:
        print("  x {0}: {1}".format(name, exc))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
