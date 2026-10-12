# -*- coding: utf-8 -*-
"""定向探针：判定 spawn_into_level 在 UE 5.8 上到底是 API 变了还是 commandlet 环境限制。

对照三种摆放方式：
    A. EditorActorSubsystem.spawn_actor_from_object()（适配器当前用法）
    B. EditorActorSubsystem.spawn_actor_from_class(StaticMeshActor)（退路）
    C. EditorLevelLibrary.spawn_actor_from_object()（老 API）
并打印当前编辑器世界，判断是不是「没有世界」导致摆不进去。
"""
import json
import os

import unreal

LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
RESULT_JSON = os.path.join(LOG_DIR, "ue_spawn_probe.json")
MESH = "/Game/Bridge/SM_CalibCube"
LEVEL = "/Game/Bridge/Levels/SpawnProbe"

R = {"steps": [], "errors": []}


def log(m):
    unreal.log("[spawn-probe] " + str(m))
    R["steps"].append(str(m))


def main():
    # 1. 准备关卡
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(LEVEL):
        R["level"] = "load:" + str(les.load_level(LEVEL))
    else:
        R["level"] = "new:" + str(les.new_level(LEVEL))
    log("level = %s" % R["level"])

    # 2. 编辑器世界
    for fn in ("get_editor_world",):
        try:
            R["editor_world_EditorLevelLibrary"] = str(
                getattr(unreal.EditorLevelLibrary, fn)())
        except Exception as exc:
            R["editor_world_EditorLevelLibrary"] = "ERR %s" % exc
    try:
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        R["editor_world_UnrealEditorSubsystem"] = str(ues.get_editor_world())
    except Exception as exc:
        R["editor_world_UnrealEditorSubsystem"] = "ERR %s" % exc
    log("editor world = %s / %s" % (R.get("editor_world_EditorLevelLibrary"),
                                    R.get("editor_world_UnrealEditorSubsystem")))

    mesh = unreal.EditorAssetLibrary.load_asset(MESH)
    R["mesh_loaded"] = mesh is not None
    log("mesh loaded = %s" % R["mesh_loaded"])

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # A. 适配器当前用法
    try:
        a = sub.spawn_actor_from_object(mesh, unreal.Vector(0, 0, 0))
        R["A_from_object"] = str(a)
    except Exception as exc:
        R["A_from_object"] = "ERR %s" % exc
    log("A spawn_actor_from_object = %s" % R["A_from_object"])

    # B. 先生成 StaticMeshActor 再挂网格
    try:
        b = sub.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(200, 0, 0))
        R["B_from_class"] = str(b)
        if b:
            b.static_mesh_component.set_static_mesh(mesh)
            R["B_mesh_set"] = True
            b.set_actor_label("Probe_FromClass")
    except Exception as exc:
        R["B_from_class"] = "ERR %s" % exc
    log("B spawn_actor_from_class = %s" % R["B_from_class"])

    # C. 老 API
    try:
        c = unreal.EditorLevelLibrary.spawn_actor_from_object(mesh, unreal.Vector(400, 0, 0))
        R["C_legacy"] = str(c)
    except Exception as exc:
        R["C_legacy"] = "ERR %s" % exc
    log("C EditorLevelLibrary.spawn_actor_from_object = %s" % R["C_legacy"])

    try:
        actors = sub.get_all_level_actors()
        R["actors"] = [a.get_actor_label() for a in actors]
    except Exception as exc:
        R["actors"] = "ERR %s" % exc
    log("关卡内 actor = %s" % R.get("actors"))

    try:
        unreal.EditorLevelLibrary.save_current_level()
    except Exception as exc:
        R["save_err"] = str(exc)

    with open(RESULT_JSON, "w", encoding="utf-8") as fh:
        json.dump(R, fh, ensure_ascii=False, indent=2)
    log("结果写入 %s" % RESULT_JSON)


try:
    main()
except Exception as exc:
    R["errors"].append("%s: %s" % (type(exc).__name__, exc))
    unreal.log_error("[spawn-probe] 主流程异常: %s" % exc)
    try:
        with open(RESULT_JSON, "w", encoding="utf-8") as fh:
            json.dump(R, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

log("[spawn-probe] 结束")
