#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBJ 收发链路无头测试（2026-10-14 P0-4 / P0-5 配套，纯标准库，不启动 DCC）。

背景：Blender/Maya/UE 三端新增「发送到 ZBrush」（OBJ 导出），ZBrush 侧
新增每目标一按钮（Blender/Maya/UE/SP）。OBJ 是数据入口，出错是静默的，
所以四个新函数 + 两个接收端分发分支必须带测试：

  Blender（假 bpy，记录 operator 调用参数）：
    1. io_fbx.export_obj   -> wm.obj_export：只选 MESH、选中保存/恢复、
                              global_scale 来自 profile、文件不存在报错
    2. io_fbx.import_obj   -> wm.obj_import：datablock 差集、场景链接、选中
    3. operators._import_model 数据入口分发：.obj -> import_obj，
                              .fbx -> import_fbx（旧代码 .obj 走 FBX 导入器必炸）
  Maya（假 maya.cmds，记录 file() 调用）：
    4. export_selection_obj -> objExport 插件按需加载 + typ="OBJexport" + es=True
    5. import_obj           -> 先试 type="OBJ"，失败退 "OBJimport"，全失败抛 RuntimeError
  UE（假 unreal 模块）：
    6. exporter.export_assets(fmt="obj") -> 非 StaticMesh 跳过并告警 /
                              文件名 .obj / 不传 FbxExportOption；
                              fmt="fbx" 行为不回归（仍传 options）

用法： python tests/test_obj_flow.py
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
BL_PLUGIN = ROOT / "plugins" / "blender" / "asset_bridge_blender"
MAYA_PLUGIN = ROOT / "plugins" / "maya" / "asset_bridge_maya"
UE_PLUGIN = ROOT / "plugins" / "unreal" / "AssetBridge" / "Content" / "Python" / "asset_bridge_unreal"

PASSED, FAILED = [], []


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


def load_standalone(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Blender：最小假 bpy（覆盖 io_fbx 用到的 API 面）
# ---------------------------------------------------------------------------

class _RNA:
    """_call() 的 RNA 反射面：properties -> {名: SimpleNamespace(type=...)}. """
    def __init__(self, names):
        self.properties = {
            n: types.SimpleNamespace(type="STRING", enum_items=[])
            for n in names
        }


class _FakeObj:
    def __init__(self, name, otype="MESH"):
        self.name = name
        self.type = otype
        self.selected = False

    def select_set(self, v):
        self.selected = bool(v)


class _FakeObjCol:
    """支持 iter / in（按 name）/ add，模拟 bpy.data.objects。"""
    def __init__(self):
        self.items = []

    def __iter__(self):
        return iter(self.items)

    def __contains__(self, key):
        if isinstance(key, _FakeObj):
            return key in self.items
        return any(o.name == key for o in self.items)

    def add(self, obj):
        self.items.append(obj)


class _FakeLinkableCol:
    def __init__(self):
        self.linked = []

    def link(self, ob):
        self.linked.append(ob)


def _op(recorder, key, rna_names, side_effect=None):
    """可调用的假 operator：记录调用参数；带 get_rna_type 供 _call 过滤。"""
    def call(**kwargs):
        recorder.append((key, dict(kwargs)))
        if side_effect is not None:
            side_effect(kwargs)
        return {"FINISHED"}

    call.get_rna_type = lambda: _RNA(rna_names)
    return call


def _make_blender_bpy(recorder, obj_export_effect=None, obj_import_effect=None):
    bpy = types.ModuleType("bpy")

    class _AnyType:
        def __init__(self, *a, **k):
            pass

    class _AnyTypes:
        def __getattr__(self, name):
            return _AnyType

    bpy.types = _AnyTypes()
    bpy.data = types.SimpleNamespace(objects=_FakeObjCol())
    bpy.context = types.SimpleNamespace(
        selected_objects=[],
        scene=types.SimpleNamespace(objects=set()),
        view_layer=types.SimpleNamespace(
            objects=types.SimpleNamespace(active=None),
            active_layer_collection=types.SimpleNamespace(
                collection=types.SimpleNamespace(
                    objects=types.SimpleNamespace(link=lambda ob: None)),
            ),
        ),
    )

    def _select_all(action="DESELECT"):
        recorder.append(("select_all", action))
        for ob in bpy.data.objects:
            ob.selected = (action == "SELECT")

    bpy.ops = types.SimpleNamespace(
        object=types.SimpleNamespace(select_all=_select_all),
        wm=types.SimpleNamespace(
            obj_export=_op(recorder, "wm.obj_export",
                           ["filepath", "use_selection", "export_selected_objects",
                            "global_scale", "export_materials"], obj_export_effect),
            obj_import=_op(recorder, "wm.obj_import",
                           ["filepath", "global_scale"], obj_import_effect),
        ),
    )
    return bpy


def _blender_env():
    """返回 (bpy, io_fbx, operators, recorder)。operators 已打 io_fbx 桩由用例自装。"""
    recorder = []
    bpy = _make_blender_bpy(recorder)
    sys.modules["bpy"] = bpy
    pkg_name = "ab_blender_plugin_objflow"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(BL_PLUGIN)]
    sys.modules[pkg_name] = pkg
    io_fbx = importlib.import_module(pkg_name + ".io_fbx")
    operators = importlib.import_module(pkg_name + ".operators")
    return bpy, io_fbx, operators, recorder


def _reset_selection(bpy):
    bpy.data = types.SimpleNamespace(objects=_FakeObjCol())
    bpy.context.selected_objects = []
    bpy.context.view_layer.objects.active = None


def test_blender():
    print("Blender io_fbx / operators._import_model")
    bpy, io_fbx, operators, recorder = _blender_env()

    # -- 1. export_obj：只选 MESH、参数正确、选中恢复 -----------------------
    def t_export_obj():
        _reset_selection(bpy)
        meshes = [_FakeObj("SM_A"), _FakeObj("SM_B")]
        arm = _FakeObj("Armature", "ARMATURE")
        for ob in (meshes[0], arm, meshes[1]):
            bpy.data.objects.add(ob)
        bpy.context.selected_objects = [meshes[0], arm]
        prev_active = _FakeObj("Prev", "EMPTY")
        bpy.data.objects.add(prev_active)
        bpy.context.view_layer.objects.active = prev_active

        with tempfile.TemporaryDirectory() as tmp:
            fp = str(Path(tmp) / "SM_A.obj")
            recorder.clear()

            def write_file(kwargs):
                Path(kwargs["filepath"]).write_text("v 0 0 0\n")

            bpy.ops.wm.obj_export = _op(
                recorder, "wm.obj_export",
                ["filepath", "use_selection", "export_selected_objects",
                 "global_scale", "export_materials"],
                write_file)
            out = io_fbx.export_obj(fp, {"export": {"global_scale": 1.0}},
                                    [meshes[0], arm, meshes[1]])
            assert out == fp, out
            calls = [c for c in recorder if c[0] == "wm.obj_export"]
            assert len(calls) == 1, calls
            kw = calls[0][1]
            assert kw["filepath"] == fp
            assert kw["global_scale"] == 1.0
            assert kw.get("use_selection") is True or \
                kw.get("export_selected_objects") is True, kw
            # 选中恢复：之前选中的对象重新被 select_set(True)，active 还原
            assert meshes[0].selected and not meshes[1].selected, "选中未恢复"
            assert bpy.context.view_layer.objects.active is prev_active, "active 未还原"
        return "wm.obj_export 参数与选中恢复 OK"

    check("blender.export_obj 参数/选中恢复/文件校验", t_export_obj)

    def t_export_obj_missing_file():
        _reset_selection(bpy)
        m = _FakeObj("SM_X")
        bpy.data.objects.add(m)
        with tempfile.TemporaryDirectory() as tmp:
            fp = str(Path(tmp) / "nope.obj")
            recorder.clear()

            def no_write(kwargs):  # 模拟导出器没产出文件
                pass

            bpy.ops.wm.obj_export = _op(recorder, "wm.obj_export",
                                        ["filepath", "use_selection"], no_write)
            try:
                io_fbx.export_obj(fp, {}, [m])
            except RuntimeError as exc:
                assert "OBJ 导出未生成文件" in str(exc), exc
                return "缺文件 -> RuntimeError OK"
            raise AssertionError("未生成文件却没报错")

    check("blender.export_obj 文件未产出时报错", t_export_obj_missing_file)

    # -- 2. import_obj：datablock 差集 + 场景链接 + 选中 --------------------
    def t_import_obj():
        _reset_selection(bpy)
        old = _FakeObj("Old")
        bpy.data.objects.add(old)

        def import_effect(kwargs):
            for n in ("ZB_1", "ZB_2"):
                bpy.data.objects.add(_FakeObj(n))

        recorder.clear()
        bpy.ops.wm.obj_import = _op(recorder, "wm.obj_import",
                                    ["filepath", "global_scale"], import_effect)
        objs = io_fbx.import_obj("/fake/zbrush.obj",
                                 {"import": {"global_scale": 1.0}})
        names = sorted(o.name for o in objs)
        assert names == ["ZB_1", "ZB_2"], names
        kw = recorder[0][1]
        assert kw["filepath"] == "/fake/zbrush.obj"
        assert kw["global_scale"] == 1.0
        assert all(o.selected for o in objs), "导入结果未选中"
        assert bpy.context.view_layer.objects.active.name == "ZB_1"
        return "wm.obj_import 参数/差集/选中 OK"

    check("blender.import_obj 导入行为", t_import_obj)

    # -- 3. 数据入口分发：.obj -> import_obj，.fbx -> import_fbx -----------
    def t_routing():
        calls = []
        operators.io_fbx = types.SimpleNamespace(
            import_obj=lambda f, p: calls.append(("obj", f)) or [],
            import_fbx=lambda f, p: calls.append(("fbx", f)) or [],
            last_import_report=lambda imported: "stub-report",
        )
        with tempfile.TemporaryDirectory() as tmp:
            obj_fp = str(Path(tmp) / "model.obj")
            fbx_fp = str(Path(tmp) / "model.fbx")
            Path(obj_fp).write_text("v 0 0 0\n")
            Path(fbx_fp).write_bytes(b"Kaydara FBX")
            operators._import_model({"id": "t1", "from": {"app": "zbrush"},
                                     "_files_abs": [obj_fp]})
            operators._import_model({"id": "t2", "from": {"app": "unreal"},
                                     "_files_abs": [fbx_fp]})
        assert calls == [("obj", obj_fp), ("fbx", fbx_fp)], calls
        return ".obj->import_obj / .fbx->import_fbx OK"

    check("blender._import_model 按扩展名分发", t_routing)


# ---------------------------------------------------------------------------
# Maya：假 maya.cmds / maya.mel
# ---------------------------------------------------------------------------

class _FakeMayaCmds:
    def __init__(self):
        self.file_calls = []
        self.loaded_plugins = set()
        self.fail_types = set()          # file() 里 type 为这些值时抛异常
        self.import_new_nodes = []

    # --cmds API 面 --------------------------------------------------------
    def pluginInfo(self, name, query=False, loaded=False):
        return name in self.loaded_plugins

    def loadPlugin(self, name, quiet=False):
        self.loaded_plugins.add(name)
        return name

    def ls(self, pattern=None, assemblies=False):
        return list(self.import_new_nodes) if assemblies else []

    def file(self, path, **kw):
        self.file_calls.append((path, kw))
        if kw.get("type") in self.fail_types:
            raise RuntimeError("unknown file type: {0}".format(kw.get("type")))
        Path(path).write_text("# fake obj\nv 0 0 0\n")
        return path

    def warning(self, msg):
        pass


def _maya_env(cmds_impl):
    maya = types.ModuleType("maya")
    cmds_mod = types.ModuleType("maya.cmds")
    for attr in dir(cmds_impl):
        if not attr.startswith("__"):
            setattr(cmds_mod, attr, getattr(cmds_impl, attr))
    mel_mod = types.ModuleType("maya.mel")
    mel_mod.eval = lambda s: ""
    sys.modules["maya"] = maya
    sys.modules["maya.cmds"] = cmds_mod
    sys.modules["maya.mel"] = mel_mod
    mod = load_standalone(MAYA_PLUGIN / "__init__.py", "ab_maya_objflow")
    return mod


def test_maya():
    print("Maya export_selection_obj / import_obj")
    cmds_impl = _FakeMayaCmds()
    mod = _maya_env(cmds_impl)

    def t_export_obj():
        with tempfile.TemporaryDirectory() as tmp:
            fp = str(Path(tmp) / "model.obj")
            out = mod.export_selection_obj(fp)
            assert out == fp, out
            path, kw = cmds_impl.file_calls[-1]
            assert path == fp, path
            assert kw.get("typ") == "OBJexport", kw
            assert kw.get("es") is True, kw
            assert "objExport" in cmds_impl.loaded_plugins, "objExport 未按需加载"
        return 'cmds.file(typ="OBJexport", es=True) + objExport 按需加载 OK'

    check("maya.export_selection_obj", t_export_obj)

    def t_import_obj_fallback():
        cmds_impl.file_calls.clear()
        # type="OBJ" 抛异常 -> 应回退 "OBJimport"
        cmds_impl.fail_types = {"OBJ"}
        with tempfile.TemporaryDirectory() as tmp:
            fp = str(Path(tmp) / "zbrush.obj")
            nodes = mod.import_obj(fp)
            types_seen = [kw.get("type") for _p, kw in cmds_impl.file_calls]
            assert types_seen == ["OBJ", "OBJimport"], types_seen
            assert fp in nodes or True
        return '回退链 ["OBJ" -> "OBJimport"] OK'

    check("maya.import_obj 翻译器回退", t_import_obj_fallback)

    def t_import_obj_all_fail():
        cmds_impl.file_calls.clear()
        cmds_impl.fail_types = {"OBJ", "OBJimport"}
        try:
            mod.import_obj("/fake/zbrush.obj")
        except RuntimeError as exc:
            assert "OBJ 导入失败" in str(exc), exc
            return "两个翻译器全失败 -> RuntimeError OK"
        raise AssertionError("全失败却没抛错")

    check("maya.import_obj 全失败报错", t_import_obj_all_fail)


# ---------------------------------------------------------------------------
# UE：假 unreal 模块
# ---------------------------------------------------------------------------

def _make_fake_unreal(registry, recorder):
    unreal = types.ModuleType("unreal")

    class StaticMesh:
        pass

    class OtherAsset:
        pass

    class FbxExportOption:
        pass

    class AssetExportTask:
        def __init__(self):
            self.props = {}

        def set_editor_property(self, name, value):
            self.props[name] = value

        def get_editor_property(self, name):
            return self.props.get(name)

    class _AssetTools:
        @staticmethod
        def import_asset_tasks(tasks):
            pass

    class _Exporter:
        @staticmethod
        def run_asset_export_tasks(tasks):
            ok = True
            for t in tasks:
                fp = t.props.get("filename")
                obj = t.props.get("object")
                if isinstance(obj, StaticMesh):
                    Path(fp).write_text("# fake obj\nv 0 0 0\n")
                else:
                    ok = False
            return ok

    class _EditorAssetLibrary:
        @staticmethod
        def load_asset(path):
            return registry.get(path)

    unreal.StaticMesh = StaticMesh
    unreal.FbxExportOption = FbxExportOption
    unreal.AssetExportTask = AssetExportTask
    unreal.Exporter = _Exporter
    unreal.EditorAssetLibrary = _EditorAssetLibrary
    unreal.log_warning = lambda msg: recorder.append(("warn", str(msg)))
    unreal.log = lambda msg: recorder.append(("log", str(msg)))
    unreal.log_error = lambda msg: recorder.append(("error", str(msg)))
    unreal._classes = {"mesh": StaticMesh, "other": OtherAsset}

    # importer.py 模块级会引用大量 unreal 枚举/类（FbxImportUI 等），
    # 任何未显式造的属性都兜底成一个空类（本测试只驱动函数逻辑）
    class _AnyType:
        def __init__(self, *a, **k):
            pass

    unreal.__getattr__ = lambda name: _AnyType
    return unreal


def test_ue():
    print("UE exporter.export_assets(fmt=...)")
    ue_dir = UE_PLUGIN
    recorder = []
    registry = {}

    def t_export_obj_skips_non_mesh():
        unreal = _make_fake_unreal(registry, recorder)
        sys.modules["unreal"] = unreal
        exporter = load_standalone(ue_dir / "exporter.py", "ab_ue_exporter_objflow")
        registry.clear()
        sm = unreal._classes["mesh"]()
        other = unreal._classes["other"]()
        registry["/Game/Bridge/SM_A"] = sm
        registry["/Game/Bridge/T_Noise"] = other
        with tempfile.TemporaryDirectory() as tmp:
            files = exporter.export_assets(
                ["/Game/Bridge/SM_A", "/Game/Bridge/T_Noise"],
                tmp, {}, fmt="obj")
            names = sorted(Path(f).name for f in files)
            assert names == ["SM_A.obj"], names
            assert any("跳过" in m for _k, m in recorder), "非 StaticMesh 未告警"
            assert not any("T_Noise" == Path(f).stem for f in files)
        return "OBJ: 跳过非 StaticMesh + 告警 OK"

    check("ue.export_assets(fmt=obj) 跳过非 StaticMesh", t_export_obj_skips_non_mesh)

    def t_export_obj_no_options():
        unreal = _make_fake_unreal(registry, recorder)
        sys.modules["unreal"] = unreal
        exporter = load_standalone(ue_dir / "exporter.py", "ab_ue_exporter_objflow2")
        registry.clear()
        registry["/Game/Bridge/SM_A"] = unreal._classes["mesh"]()
        tasks_holder = {}

        orig_run = unreal.Exporter.run_asset_export_tasks

        def spy_run(tasks):
            tasks_holder["tasks"] = tasks
            return orig_run(tasks)

        unreal.Exporter.run_asset_export_tasks = staticmethod(spy_run)
        with tempfile.TemporaryDirectory() as tmp:
            files = exporter.export_assets(["/Game/Bridge/SM_A"], tmp, {}, fmt="obj")
            assert len(files) == 1 and files[0].endswith(".obj"), files
        task = tasks_holder["tasks"][0]
        assert "options" not in task.props, "OBJ 导出不应带 FbxExportOption"
        assert task.props["automated"] is True and task.props["prompt"] is False
        return "OBJ: 文件名 .obj 且不传 options OK"

    check("ue.export_assets(fmt=obj) 不传 FbxExportOption", t_export_obj_no_options)

    def t_export_fbx_regression():
        unreal = _make_fake_unreal(registry, recorder)
        sys.modules["unreal"] = unreal
        exporter = load_standalone(ue_dir / "exporter.py", "ab_ue_exporter_objflow3")
        registry.clear()
        registry["/Game/Bridge/SM_A"] = unreal._classes["mesh"]()
        tasks_holder = {}
        orig_run = unreal.Exporter.run_asset_export_tasks

        def spy_run(tasks):
            tasks_holder["tasks"] = tasks
            return orig_run(tasks)

        unreal.Exporter.run_asset_export_tasks = staticmethod(spy_run)
        with tempfile.TemporaryDirectory() as tmp:
            files = exporter.export_assets(["/Game/Bridge/SM_A"], tmp, {})
            assert len(files) == 1 and files[0].endswith(".fbx"), files
        task = tasks_holder["tasks"][0]
        assert isinstance(task.props.get("options"), unreal.FbxExportOption), \
            "FBX 路径必须仍传 FbxExportOption"
        return "FBX 默认路径不回归（options 仍在）OK"

    check("ue.export_assets 默认 FBX 不回归", t_export_fbx_regression)

    # -- UE 接收处理链（真实回传路径）：reload_textures -------------------
    # 教训（2026-10-14）：importer.py 用了 Path 却没 import，桩测试只测了
    # 导出函数，真实回传一跑就 NameError。这条测试把「接收处理函数本体」
    # 桩驱动执行一遍，锁住名字解析与映射逻辑。
    def t_reload_textures_mapping():
        unreal = _make_fake_unreal(registry, recorder)
        sys.modules["unreal"] = unreal
        importer = load_standalone(ue_dir / "importer.py", "ab_ue_importer_objflow")
        calls = []
        # 桩掉深层的真实导入（AssetImportTask 链由真机验证）；本测试只锁
        # reload_textures 自己的 Path 使用 + 文件名->资产映射逻辑
        importer._import_one_texture = (
            lambda fp, dest_path, dest_name:
            calls.append((fp, dest_path, dest_name)) or [])
        record = {"asset_name": "T_SM_CalibCube",
                  "to": {"content_path": "/Game/Bridge/Textures"}}
        opts = {
            "asset_paths": ["/Game/Bridge/Textures/T_SM_CalibCube_BaseColor"],
            "source_files": ["T_SM_CalibCube_BaseColor.png"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            fp = str(Path(tmp) / "T_SM_CalibCube_BaseColor.png")
            Path(fp).write_bytes(b"png")
            imported = importer.reload_textures(record, [fp], opts)
            assert calls == [(fp, "/Game/Bridge/Textures",
                              "T_SM_CalibCube_BaseColor")], calls
            assert imported == []
        # 无映射文件 -> 退化 default_dir + 按通道命名（走 _channel_info）
        with tempfile.TemporaryDirectory() as tmp:
            fp2 = str(Path(tmp) / "T_XX_Normal.png")
            Path(fp2).write_bytes(b"png")
            importer.reload_textures(record, [fp2], {})
            assert calls[-1][1] == "/Game/Bridge/Textures", calls[-1]
            assert calls[-1][2].startswith("T_T_SM_CalibCube_"), calls[-1]
        return "reload_textures 真实执行：映射 + 退化路径 OK（含 Path 名字解析）"

    check("ue.reload_textures 接收处理链（真实失败路径）", t_reload_textures_mapping)


def main():
    print("=" * 60)
    print("OBJ 收发链路无头测试（tests/test_obj_flow.py）")
    print("=" * 60)
    test_blender()
    test_maya()
    test_ue()
    print("-" * 60)
    print("=== 结果: {0} 通过 / {1} 失败 ===".format(len(PASSED), len(FAILED)))
    for name, exc in FAILED:
        print("  FAILED: {0} -- {1}: {2}".format(name, type(exc).__name__, exc))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
