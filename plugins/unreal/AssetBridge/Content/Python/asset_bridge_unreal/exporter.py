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


def export_assets(asset_paths: List[str], out_dir: str, profile: Dict,
                  fmt: str = "fbx") -> List[str]:
    """导出资产，返回写出的文件绝对路径。

    fmt="fbx"：走 FBX（默认，支持 StaticMesh/SkeletalMesh/动画等）。
    fmt="obj"：走引擎内置 UStaticMeshExporterOBJ（RunAssetExportTask 按
              文件扩展名自动选导出器，引擎源码 EditorExporters.cpp 已注册
              SupportedClass=StaticMesh / FormatExtension=OBJ）。
              OBJ 只对 StaticMesh 有效 —— 其它类型资产会跳过并打 warning
              （ZBrush 第一期只收静态网格模型）。
    """
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
        if fmt == "obj" and not isinstance(asset, unreal.StaticMesh):
            unreal.log_warning(
                f"[bridge] OBJ 导出只支持 StaticMesh，跳过: {p}"
                f"（{type(asset).__name__}）")
            continue
        name = Path(p).name
        fp = out_dir / f"{name}.{fmt}"

        task = unreal.AssetExportTask()
        task.set_editor_property("object", asset)
        task.set_editor_property("filename", str(fp))
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        task.set_editor_property("write_empty_files", False)
        if fmt == "fbx":
            opts = unreal.FbxExportOption()
            _safe_set(opts, "ascii", bool(exp_opts.get("ascii", False)))
            _safe_set(opts, "collision", False)
            _safe_set(opts, "export_morph_targets", True)
            # 下面两个在 UE 5.8 已移除，留着是为了兼容旧版本；缺失时 _safe_set 会跳过
            _safe_set(opts, "export_materials", True)
            _safe_set(opts, "export_texture", True)
            _safe_set(opts, "vertex_color", True)
            _safe_set(opts, "level_of_detail", False)
            task.set_editor_property("options", opts)
        # OBJ 不传 options：FbxExportOption 与 OBJ 导出器的选项类不匹配

        tasks.append(task)
        written.append(str(fp))

    if not tasks:
        return []

    ok = unreal.Exporter.run_asset_export_tasks(tasks)
    if not ok:
        unreal.log_warning("[bridge] 部分资产导出失败，请检查 Output Log")
    return [f for f in written if Path(f).exists()]


def export_textures(asset_paths: List[str], out_dir: str) -> List[tuple]:
    """把选中的 Texture2D 导出为 PNG，返回 [(资产包路径, 文件绝对路径)]。

    只有 Texture2D 会被导出；选中里混着网格等其他资产时自动跳过。
    导出的文件名 = 资产名 + .png，编辑器守护进程靠它和原资产对应。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for p in asset_paths:
        asset = unreal.EditorAssetLibrary.load_asset(p)
        if asset is None:
            unreal.log_warning(f"[bridge] 资产不存在: {p}")
            continue
        if not isinstance(asset, unreal.Texture2D):
            continue
        name = Path(p).name
        fp = out_dir / f"{name}.png"
        task = unreal.AssetExportTask()
        task.set_editor_property("object", asset)
        task.set_editor_property("filename", str(fp))
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        try:
            unreal.Exporter.run_asset_export_tasks([task])
        except Exception as exc:
            unreal.log_warning(f"[bridge] 贴图导出失败 {p}: {exc}")
            continue
        if fp.exists():
            out.append((p, str(fp)))
        else:
            unreal.log_warning(f"[bridge] 贴图导出未生成文件: {p}")
    return out
