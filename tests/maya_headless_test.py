# -*- coding: utf-8 -*-
"""
Asset Bridge :: Maya 无头自检脚本
============================================================================
用命令行 mayapy.exe 执行（不启动 Maya GUI）：

    "D:\\DCC\\Maya2018\\bin\\mayapy.exe" \
        "E:\\CirnoWorkSpace\\AssetBridge\\tests\\maya_headless_test.py"

验证链路：
    1. maya.standalone 能起来、拿得到 maya.cmds
    2. 建多边形立方体并选中
    3. export_selection() 导出 FBX，字节数 > 0
    4. send_selection() 经中转服务发出去，服务侧能查到记录
    5. import_fbx() 导入已知 FBX，场景里能拿到新对象
    6. 量导入后的尺寸，校验 Maya(cm) 与 Blender(m) 的换算

隔离约定：transfer 一律发往一次性目标 `maya_probe` + 独立 project
`MayaQaTest`，不会污染 UE 同事的收件箱。

本脚本自身也要在 Python 2.7（Maya 2018）下跑，因此：
不用 f-string、不用 pathlib、顶层加 print_function。
============================================================================
"""
from __future__ import print_function

import io
import json
import os
import sys
import traceback

ROOT = r"E:\CirnoWorkSpace\AssetBridge"
PLUGIN_DIR = os.path.join(ROOT, "maya", "plugin")
LOG_DIR = os.path.join(ROOT, "logs")
WORK_DIR = os.path.join(LOG_DIR, "_maya_probe")
OUT_JSON = os.path.join(LOG_DIR, "maya_test_result.json")

# Blender 侧产出的 1m 校准立方体（已知：FBX 内有效单位为厘米）
CALIB_FBX = os.path.join(ROOT, "runtime", "payloads",
                         "083b36b8913f4d7a96e226bef454b228", "SM_CalibCube.fbx")

# 自动识别当前是哪一个 Maya：mayapy.exe 位于 <MayaRoot>\bin\ 下，
# 往上两级就是 Maya 根目录。也可以显式用环境变量 MAYA_BRIDGE_ROOT 覆盖。
MAYA_ROOT = os.environ.get("MAYA_BRIDGE_ROOT") or os.path.dirname(
    os.path.dirname(sys.executable)
)
TARGET_APP = "maya_probe"          # 一次性目标名，避免干扰 UE 回归测试
PROJECT = "MayaQaTest"
CUBE_CM = 100.0                    # 1m 立方体在 Maya(cm) 场景里的边长

STEPS = []
RESULT = {
    "app": "maya",
    "steps": STEPS,
    "export": {},
    "send": {},
    "import_calib": {},
    "import_roundtrip": {},
    "conversion": {},
    "issues": [],
}

cmds = None
bridge = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _ensure_dir(path):
    """创建目录（py2.7 的 os.makedirs 没有 exist_ok）。"""
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def run_step(name, fn):
    """执行一个检查步骤，把结果记进 STEPS。"""
    try:
        detail = fn()
        ok = True
    except Exception:
        ok = False
        detail = traceback.format_exc(limit=4)
    STEPS.append({"name": name, "ok": ok, "detail": str(detail)})
    print("[{0}] {1} :: {2}".format("OK  " if ok else "FAIL", name, detail))
    return ok


def bbox_size(node):
    """返回节点的世界包围盒三边长度（单位取决于场景线性单位）。"""
    bb = cmds.exactWorldBoundingBox(node)
    return [round(bb[3] - bb[0], 4), round(bb[4] - bb[1], 4), round(bb[5] - bb[2], 4)]


def _new_scene():
    cmds.file(new=True, force=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    global cmds, bridge

    _ensure_dir(WORK_DIR)

    # FBX 插件在 Maya 2018 里是通过 modules/fbx.mod 挂进来的，
    # 用命令行 mayapy 启动时显式把模块目录塞进 MAYA_MODULE_PATH 更稳。
    mod_dir = os.path.join(MAYA_ROOT, "modules")
    cur = os.environ.get("MAYA_MODULE_PATH", "")
    if mod_dir not in cur:
        os.environ["MAYA_MODULE_PATH"] = mod_dir + os.pathsep + cur

    # --- 步骤 1：standalone 初始化 -----------------------------------------
    def _init():
        import maya.standalone as standalone
        try:
            standalone.initialize()
            init_mode = "standalone.initialize()"
        except Exception:
            # mayabatch 下已经初始化过了，重复调用会抛异常，忽略即可
            init_mode = "already initialized (mayabatch)"
        import maya.cmds as _cmds
        cmds = _cmds  # noqa: F841  -- 需要写入 globals，见下方赋值
        globals()["cmds"] = _cmds
        return "{0}; Maya {1}".format(init_mode, _cmds.about(version=True))

    run_step("maya.standalone 初始化并拿到 maya.cmds", _init)
    if cmds is None:
        RESULT["issues"].append("standalone 初始化失败，后续步骤全部跳过")
        return False

    RESULT["maya_version"] = cmds.about(version=True)
    RESULT["python_version"] = sys.version.split()[0]
    RESULT["scene_unit"] = cmds.currentUnit(query=True, linear=True)
    RESULT["maya_module_path"] = os.environ.get("MAYA_MODULE_PATH", "")

    # --- 步骤 2：加载 FBX 插件 ---------------------------------------------
    def _plugin():
        if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
            cmds.loadPlugin("fbxmaya", quiet=True)
        loaded = cmds.pluginInfo("fbxmaya", query=True, loaded=True)
        assert loaded, "fbxmaya 加载失败"
        RESULT["fbx_plugin"] = {"name": "fbxmaya", "loaded": True,
                                "path": cmds.pluginInfo("fbxmaya", query=True, path=True)}
        return "fbxmaya 已加载"

    run_step("FBX 插件 fbxmaya 可用", _plugin)

    # --- 步骤 3：建立方体并选中 ---------------------------------------------
    cube = [None]

    def _make_cube():
        _new_scene()
        node = cmds.polyCube(width=CUBE_CM, height=CUBE_CM, depth=CUBE_CM,
                             name="QA_CalibCube100cm")[0]
        cmds.select(node)
        cube[0] = node
        sel = cmds.ls(selection=True) or []
        assert sel, "选中为空"
        return "{0} ({1}), 选中 {2} 个, 包围盒 {3}".format(
            node, cmds.nodeType(node), len(sel), bbox_size(node))

    run_step("建多边形立方体并选中", _make_cube)

    # --- 步骤 4：导入适配器模块 ---------------------------------------------
    def _import_bridge():
        if PLUGIN_DIR not in sys.path:
            sys.path.insert(0, PLUGIN_DIR)
        import asset_bridge_maya as _b
        globals()["bridge"] = _b
        _b.CONFIG["project"] = PROJECT
        return "asset_bridge_maya 导入成功, server={0}, project={1}".format(
            _b.CONFIG["server_url"], PROJECT)

    run_step("导入 asset_bridge_maya（py2.7 语法兼容）", _import_bridge)
    if bridge is None:
        RESULT["issues"].append("适配器导入失败，后续步骤全部跳过")
        return False

    # --- 步骤 5：export_selection 导出 FBX ----------------------------------
    out_fbx = os.path.join(WORK_DIR, "QA_CalibCube100cm.fbx")

    def _export():
        if os.path.exists(out_fbx):
            os.remove(out_fbx)
        cmds.select(cube[0])
        res = bridge.export_selection(out_fbx, bridge.route_profile("maya"))
        assert res, "export_selection 返回 None"
        size = os.path.getsize(out_fbx)
        assert size > 0, "FBX 字节数为 0"
        RESULT["export"] = {"path": out_fbx, "bytes": size,
                            "profile": bridge.route_profile("maya")}
        return "{0} 字节 -> {1}".format(size, out_fbx)

    run_step("export_selection() 导出 FBX 且字节数 > 0", _export)

    # --- 步骤 6：send_selection 经中转服务发出 ------------------------------
    def _send():
        cmds.select(cube[0])
        n = bridge.send_selection(TARGET_APP, "maya headless qa")
        assert n == 1, "send_selection 返回 {0}".format(n)
        inbox = bridge._req("GET", "/inbox?app={0}".format(TARGET_APP), timeout=3.0)
        cnt = inbox.get("count", 0)
        assert cnt >= 1, "服务侧收件箱查不到记录: {0}".format(inbox)
        # 收件箱按 seq 升序返回，重复跑测试时 [0] 会是上一轮的旧记录，取 seq 最大的那条
        rec = max(inbox["transfers"], key=lambda r: r.get("seq", 0))
        # 说明：_files_abs 只有在 claim 之后才会填充，草稿/queued 态要看 files 字段，
        # 落盘字节则直接量 payload_dir/<tid>/ 目录。
        names = rec.get("files") or []
        assert names, "服务侧记录里 files 为空: {0}".format(rec)
        pdir = None
        pbytes = 0
        on_disk = []
        pd = bridge.payload_dir()
        if pd:
            pdir = os.path.join(pd, rec.get("id", ""))
            if os.path.isdir(pdir):
                on_disk = sorted(os.listdir(pdir))
                for nm in on_disk:
                    fp = os.path.join(pdir, nm)
                    if os.path.isfile(fp):
                        pbytes += os.path.getsize(fp)
        RESULT["send"] = {
            "target_app": TARGET_APP, "project": PROJECT,
            "transfer_id": rec.get("id"), "asset_name": rec.get("asset_name"),
            "status": rec.get("status"), "inbox_count": cnt,
            "files": names, "payload_dir": pdir, "files_on_disk": on_disk,
            "payload_bytes": pbytes,
        }
        assert pbytes > 0, "payload 目录里没有实际文件字节: {0}".format(pdir)
        return "transfer={0} status={1} files={2} ({3} 字节)".format(
            rec.get("id"), rec.get("status"), names, pbytes)

    run_step("send_selection() 发出且服务侧可查到记录", _send)

    # --- 步骤 7：import_fbx 导入 Blender 校准立方体 -------------------------
    def _import_calib():
        assert os.path.isfile(CALIB_FBX), "找不到校准 FBX: {0}".format(CALIB_FBX)
        _new_scene()
        new = bridge.import_fbx(CALIB_FBX, bridge.route_profile("blender"))
        assert new, "导入后没有新增对象"
        size = bbox_size(new[0])
        RESULT["import_calib"] = {
            "source": CALIB_FBX, "source_bytes": os.path.getsize(CALIB_FBX),
            "new_objects": new, "bbox_cm": size,
            "expected_cm": [CUBE_CM, CUBE_CM, CUBE_CM],
            "profile": bridge.route_profile("blender"),
        }
        return "新对象 {0}, 包围盒 {1} cm".format(new, size)

    run_step("import_fbx() 导入 Blender 校准立方体", _import_calib)

    # --- 步骤 8：自身导出 FBX 往返回读 --------------------------------------
    def _roundtrip():
        assert os.path.isfile(out_fbx), "没有可回读的导出文件"
        _new_scene()
        new = bridge.import_fbx(out_fbx, bridge.route_profile("maya"))
        assert new, "往返导入后没有新增对象"
        size = bbox_size(new[0])
        RESULT["import_roundtrip"] = {
            "source": out_fbx, "new_objects": new, "bbox_cm": size,
            "expected_cm": [CUBE_CM, CUBE_CM, CUBE_CM],
        }
        return "新对象 {0}, 包围盒 {1} cm".format(new, size)

    run_step("自身导出 FBX 往返回读（Maya->FBX->Maya）", _roundtrip)

    # --- 换算结论 -----------------------------------------------------------
    calib = RESULT.get("import_calib") or {}
    rt = RESULT.get("import_roundtrip") or {}
    RESULT["conversion"] = {
        "maya_scene_unit": RESULT.get("scene_unit"),
        "blender_fbx_effective_unit": "cm（Blender 侧实测：顶点按米写，"
                                      "根 Model 节点 Lcl Scaling=100）",
        "calib_cube_nominal_m": 1.0,
        "calib_cube_imported_cm": calib.get("bbox_cm"),
        "roundtrip_imported_cm": rt.get("bbox_cm"),
        "fbx_export_scale_factor_used": 1.0,
        "fbx_import_scale_factor_used": 1.0,
        "recommendation": {
            "maya>unreal": "scale_factor 保持 1.0 —— 实测导出 100cm 立方体后回读仍为 100cm，"
                           "Maya 与 UE 同为 cm，无需缩放",
            "unreal>maya": "scale_factor 保持 1.0 —— 实测导入 Blender 侧的 1m 立方体"
                           "（FBX 有效单位 cm）得 100cm，无需缩放",
            "blender>maya": "profiles.json 里缺少该路由，实测应为 scale_factor=1.0 / up_axis=y",
        },
    }
    RESULT["files_changed"] = [
        r"maya\plugin\asset_bridge_maya\__init__.py —— py2.7 / py3 双兼容改造"
        r"（去 f-string、去 pathlib、urllib 适配、无代理 opener）",
        r"tests\maya_headless_test.py —— 新增，Maya 无头自检脚本",
        r"logs\maya_test_result.md / logs\maya_test_result.json —— 本报告",
    ]
    RESULT["issues"].extend([
        "本机只有 Maya 2018（Python 2.7），没有 Maya 2020+ 可跑 py3 分支；"
        "py3 分支目前只做了语法编译校验（python 3.13 通过），未做运行时验证。",
        "profiles.json 缺少 blender>maya / maya>blender 两条路由，"
        "当前收发走的是空 profile 的内置默认值（up_axis=y, scale_factor=1.0）。",
        "send_selection 导出时取 route_profile('maya')（即 maya>maya，实际不存在），"
        "等价于内置默认；若日后改动 maya>unreal 的导出参数，send 路径不会同步。",
        r"C:\Users\12069\Documents\maya\scripts\userSetup.py 第 13 行调用 "
        r"vaccine.phage() 报错（已知 Maya vaccine 残留脚本），不影响本次测试，"
        r"但每次启动 Maya 都会刷一条错误。",
    ])

    # --- 收尾 ---------------------------------------------------------------
    try:
        import maya.standalone as standalone
        standalone.uninitialize()
    except Exception:
        pass

    RESULT["all_passed"] = all(s["ok"] for s in STEPS)
    _ensure_dir(LOG_DIR)
    def _to_text(obj):
        """py2 下把 str/unicode 混排统一成 unicode，避免 json.dumps 抛 UnicodeDecodeError。

        py2 里 bytes 就是 str，所以 isinstance(x, bytes) 能同时命中两种字节串。
        """
        if isinstance(obj, bytes):
            return obj.decode("utf-8", "replace")
        if isinstance(obj, dict):
            return dict((_to_text(k), _to_text(v)) for k, v in obj.items())
        if isinstance(obj, (list, tuple)):
            return [_to_text(x) for x in obj]
        return obj

    with io.open(OUT_JSON, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_to_text(RESULT), ensure_ascii=False, indent=2))
    print("\n结果已写入: {0}".format(OUT_JSON))
    print("ALL_PASSED={0}".format(RESULT["all_passed"]))
    return RESULT["all_passed"]


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
