# -*- coding: utf-8 -*-
"""UE 侧导出：把内容浏览器里选中的资产发回 Blender / Maya。"""
from pathlib import Path
from typing import Dict, List

import unreal


def _safe_set(obj, name: str, value) -> bool:
    """跨版本安全设值。

    UE 5.8 实测：FbxExportOption 上已经没有 export_materials / export_texture
    （剩下的只有 ascii / collision / export_morph_targets / level_of_detail /
    vertex_color 等），直接 set_editor_property 会抛异常把整个导出搞挂。
    所以这里逐条 try，缺失就打一条 warning 跳过，保证旧版新版都能跑。
    """
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception:
        unreal.log_warning(
            f"[bridge] 当前引擎版本不支持导出选项 {name}，已跳过"
        )
        return False


def selected_asset_paths() -> List[str]:
    try:
        assets = unreal.EditorUtilityLibrary.get_selected_assets()
    except Exception:
        assets = []
    out = []
    for a in assets or []:
        try:
            out.append(a.get_path_name().split(".")[0])
        except Exception:
            pass
    return out


def export_assets(asset_paths: List[str], out_dir: str, profile: Dict) -> List[str]:
    """导出为 FBX，返回写出的文件绝对路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 路由档案里的 export 段（ascii 等），没有档案时全用默认值
    exp_opts = dict((profile or {}).get("export") or {})

    tasks = []
    written = []
    for p in asset_paths:
        asset = unreal.EditorAssetLibrary.load_asset(p)
        if not asset:
            unreal.log_warning(f"[bridge] 资产不存在: {p}")
            continue
        name = Path(p).name
        fp = out_dir / f"{name}.fbx"

        opts = unreal.FbxExportOption()
        _safe_set(opts, "ascii", bool(exp_opts.get("ascii", False)))
        _safe_set(opts, "collision", False)
        _safe_set(opts, "export_morph_targets", True)
        # 下面两个在 UE 5.8 已移除，留着是为了兼容旧版本；缺失时 _safe_set 会跳过
        _safe_set(opts, "export_materials", True)
        _safe_set(opts, "export_texture", True)
        _safe_set(opts, "vertex_color", True)
        _safe_set(opts, "level_of_detail", False)

        task = unreal.AssetExportTask()
        task.set_editor_property("object", asset)
        task.set_editor_property("filename", str(fp))
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        task.set_editor_property("write_empty_files", False)
        task.set_editor_property("options", opts)

        tasks.append(task)
        written.append(str(fp))

    if not tasks:
        return []

    ok = unreal.Exporter.run_asset_export_tasks(tasks)
    if not ok:
        unreal.log_warning("[bridge] 部分资产导出失败，请检查 Output Log")
    return [f for f in written if Path(f).exists()]
