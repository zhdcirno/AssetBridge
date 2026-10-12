# -*- coding: utf-8 -*-
"""Asset Bridge :: 贴图闭环 —— UE 5.8 接收端（commandlet）。

把「Blender 的 1m 立方体（2 材质槽）+ SP 的 3 张贴图」都收进来，
验证网格与贴图能在同一个 `/Game/Bridge` 内容树里共存、互不覆盖。

用法：
    UnrealEditor-Cmd.exe "<project>.uproject" -run=pythonscript
        -script="...\\ue_texture_closed_loop.py" -unattended -nosplash -NullRHI
        -stdout -FullStdOutLogOutput

结果写到 logs/ue_texture_closed_loop.json。
"""
import json
import os
import sys
import traceback

import unreal

PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"
ROOT = "/Game/Bridge"
MESH_PATH = "/Game/Bridge/SM_CalibCube"
TEX_PATH = "/Game/Bridge/Textures"
ASSET_NAME = "SM_CalibCube"
EXPECT_TEX = ("BaseColor", "Normal", "ORM")
RESULT_JSON = os.path.join(LOG_DIR, "ue_texture_closed_loop.json")

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

RESULT = {"engine_version": "", "steps": [], "errors": [], "checks": {},
          "import": {}, "tree": {}}
_LINES = []


def log(msg, tag="INFO"):
    text = f"[{tag}] {msg}"
    unreal.log("[closed-loop] " + text)
    RESULT["steps"].append(text)
    _LINES.append(text)


def err(context, exc):
    text = f"{context} -> {type(exc).__name__}: {exc}"
    log(text, "ERROR")
    try:
        log(traceback.format_exc().replace("\n", " | "), "ERROR")
    except Exception:
        pass
    RESULT["errors"].append(text)


def check(name, expected, actual, ok=None):
    passed = (expected == actual) if ok is None else bool(ok)
    RESULT["checks"][name] = {"expected": expected, "actual": actual,
                              "pass": bool(passed)}
    log(f"CHECK {name}: {'PASS' if passed else 'FAIL'} "
        f"(expected={expected!r} actual={actual!r})", "CHECK")
    return passed


def list_assets(root, recursive=True):
    try:
        return sorted(unreal.EditorAssetLibrary.list_assets(
            root, recursive=recursive, include_folder=False))
    except Exception as exc:
        err(f"list_assets({root})", exc)
        return []


def _v3(v):
    return [float(v.x), float(v.y), float(v.z)]


def measure(mesh):
    try:
        b = mesh.get_bounds()
        ext = _v3(b.box_extent)
        return {"ok": True, "size": [round(abs(e) * 2.0, 3) for e in ext]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _handle_pending(abu, c, kinds):
    """按 kind 处理收件箱记录，返回处理明细。"""
    inbox = c.inbox(limit=30)
    log(f"收件箱 {len(inbox)} 条: "
        f"{[(r.get('asset_name'), r.get('kind'), r.get('status')) for r in inbox]}")
    processed = []
    for rec in inbox:
        if rec.get("kind") not in kinds or rec.get("asset_name") != ASSET_NAME:
            continue
        tid = rec["id"]
        try:
            full = c.claim(tid)
        except Exception as exc:
            err(f"claim {tid}", exc)
            continue
        log(f"已认领 {tid} kind={full.get('kind')} rev={full.get('revision')} "
            f"files={full.get('_files_abs')}")
        abu._handle(full)          # 真实链路
        processed.append({"transfer_id": tid, "kind": full.get("kind"),
                          "revision": full.get("revision")})
    return processed


def main():
    try:
        RESULT["engine_version"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        err("get_engine_version", exc)
    log(f"engine = {RESULT['engine_version']}")

    import asset_bridge_unreal as abu
    abu.CONFIG["project"] = PROJECT
    abu.CONFIG["server_url"] = SERVER
    abu.CONFIG["spawn_on_import"] = False   # 闭环只验资产共存，不摆关卡的 actor

    c = abu.client()
    try:
        sess = c.register()
        RESULT["import"]["session_id"] = sess.get("session_id")
        log(f"已注册 UE 会话 {sess.get('session_id')}")
    except Exception as exc:
        err("register", exc)

    # 先收网格，再收贴图（顺序无关，这里按真实使用习惯）
    RESULT["import"]["mesh"] = _handle_pending(abu, c, {"static_mesh"})
    RESULT["import"]["textures"] = _handle_pending(abu, c, {"textures"})

    # ---- 内容树
    tree = list_assets(ROOT, recursive=True)
    RESULT["tree"]["under_bridge"] = tree
    log(f"/Game/Bridge 下资产 = {tree}")

    # 网格
    mesh_exists = unreal.EditorAssetLibrary.does_asset_exist(MESH_PATH)
    mesh_obj = (unreal.EditorAssetLibrary.load_asset(MESH_PATH)
                if mesh_exists else None)
    RESULT["tree"]["mesh_path"] = MESH_PATH
    RESULT["tree"]["mesh_exists"] = bool(mesh_exists)
    RESULT["tree"]["mesh_class"] = type(mesh_obj).__name__ if mesh_obj else None
    m = measure(mesh_obj) if mesh_obj else {"ok": False}
    RESULT["tree"]["mesh_size_cm"] = m.get("size")
    slots = 0
    try:
        slots = len(mesh_obj.get_editor_property("static_materials"))
    except Exception:
        pass
    RESULT["tree"]["mesh_material_slots"] = slots
    log(f"网格 {MESH_PATH} exists={mesh_exists} class={RESULT['tree']['mesh_class']} "
        f"size={m.get('size')} slots={slots}")

    # 贴图：只统计 /Game/Bridge/Textures 下的（/Game/Bridge 根目录可能残留
    # 早期 export-probe 留下的旧命名资产，不属于本次闭环范围）
    want = [f"{TEX_PATH}/T_{ASSET_NAME}_{ch}" for ch in EXPECT_TEX]
    tex_found = [p for p in tree
                 if p.startswith(TEX_PATH + "/")
                 and p.rsplit("/", 1)[-1].startswith(f"T_{ASSET_NAME}_")]
    RESULT["tree"]["textures"] = tex_found
    log(f"贴图资产 = {tex_found}")

    # ---- 断言
    check("闭环_网格存在", True, bool(mesh_exists))
    check("闭环_网格为StaticMesh", True, isinstance(mesh_obj, unreal.StaticMesh))
    check("闭环_网格尺寸100cm", True,
          bool(m.get("size")) and all(abs(v - 100.0) <= 0.5 for v in m.get("size")),
          ok=bool(m.get("size")) and all(abs(v - 100.0) <= 0.5 for v in m.get("size")))
    check("闭环_网格材质槽数2", 2, slots)
    check("闭环_三张贴图存在", True,
          all(unreal.EditorAssetLibrary.does_asset_exist(p) for p in want))
    check("闭环_网格贴图目录分离", True,
          all(p.startswith(TEX_PATH + "/") for p in tex_found) and mesh_exists)
    # 网格名字没被贴图覆盖：SM_CalibCube 仍指向网格
    check("闭环_网格未被贴图覆盖", True, isinstance(mesh_obj, unreal.StaticMesh))
    # 贴图内容没被网格覆盖：3 张仍是 Texture2D
    tex_ok = all(isinstance(unreal.EditorAssetLibrary.load_asset(p), unreal.Texture2D)
                 for p in want)
    check("闭环_贴图未被网格覆盖", True, tex_ok)


try:
    main()
except Exception as exc:
    err("main", exc)

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(RESULT_JSON, "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(LOG_DIR, "ue_texture_closed_loop.log"),
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LINES))
except Exception as exc:
    unreal.log_error(f"[closed-loop] 写结果失败: {exc}")

unreal.log("[closed-loop] SUMMARY " + json.dumps({
    "engine": RESULT["engine_version"],
    "checks": {k: v["pass"] for k, v in RESULT["checks"].items()},
    "errors": RESULT["errors"],
}, ensure_ascii=False))
unreal.log("[closed-loop] 结束")
