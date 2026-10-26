# -*- coding: utf-8 -*-
"""
Blender 侧的 FBX 导出 / 导入封装。

关键点：
  * Blender 各版本 FBX 导出器参数不完全一致（4.x 起部分轴向参数被移除），
    所以不硬编码参数表，而是运行时用 RNA 反射过滤掉当前版本不存在的参数。
  * 导出前把所有要发的对象做一次「世界变换烘焙」的选择集管理，
    避免误发未选中对象。
"""
from typing import Dict, List, Optional

import bpy


# ---------------------------------------------------------------------------
# 兼容层
# ---------------------------------------------------------------------------


def _rna_props(op) -> Dict[str, object]:
    """返回 {参数名: RNA 属性}。取不到就返回空 dict（调用方会退化为不过滤）。"""
    try:
        return {p.identifier: p for p in op.get_rna_type().properties}
    except Exception:
        return {}


def _supported_props(op) -> set:
    """返回当前 Blender 版本里该 operator 实际支持的参数名。"""
    return set(_rna_props(op))


def _fit_value(p, value):
    """把单个参数值裁剪成当前版本能接受的形式。

    枚举值在各版本间会被增删（例如 FBX 的 object_types 项），传了不存在的项
    会直接 TypeError。这里把不存在的项剔掉；全被剔光就返回 None 表示放弃该参数。
    """
    try:
        is_enum = (p.type == "ENUM")
    except Exception:
        return value
    if not is_enum:
        return value

    items = {i.identifier for i in p.enum_items}
    # 多选枚举（enum_flag）传的是集合
    if isinstance(value, (set, frozenset, list, tuple)) and not isinstance(value, str):
        kept = {v for v in value if v in items}
        return type(value)(kept) if kept else None
    # 单选枚举
    if value in items:
        return value
    return None


def _call(op, kwargs: Dict) -> None:
    """过滤掉不支持的参数/非法枚举值后再调用，避免 TypeError。"""
    props = _rna_props(op)
    safe: Dict = {}
    dropped: set = set()
    trimmed: Dict = {}

    for k, v in kwargs.items():
        if props and k not in props:
            dropped.add(k)
            continue
        if props:
            fitted = _fit_value(props[k], v)
            if fitted is None:
                dropped.add(k)
                continue
            if isinstance(v, (set, frozenset)) and fitted != v:
                trimmed[k] = sorted(set(v) - set(fitted))
            safe[k] = fitted
        else:
            safe[k] = v

    if dropped:
        print(f"[bridge] 当前 Blender 不支持的参数已忽略: {sorted(dropped)}")
    if trimmed:
        print(f"[bridge] 枚举项被裁剪: {trimmed}")
    op(**safe)


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


def gather_objects(objects: List[bpy.types.Object]) -> List[Dict]:
    """收集要随资产一起传过去的元信息：类型、材质槽、三角面数、世界变换。"""
    out = []
    for ob in objects:
        entry = {
            "name": ob.name,
            "type": ob.type,
            "materials": [
                ms.material.name for ms in ob.material_slots if ms.material
            ],
        }
        if ob.type == "MESH" and ob.data:
            entry["tris"] = sum(
                len(p.loop_indices) - 2 for p in ob.data.polygons if len(p.loop_indices) >= 3
            )
            entry["uv_layers"] = [uv.name for uv in getattr(ob.data, "uv_layers", [])]
            entry["vertex_colors"] = [
                vc.name for vc in getattr(ob.data, "vertex_colors", [])
            ]
        loc, rot, scale = ob.matrix_world.decompose()
        entry["world"] = {
            "location": [round(v, 6) for v in loc],
            "rotation_quat": [round(v, 6) for v in rot],
            "scale": [round(v, 6) for v in scale],
        }
        out.append(entry)
    return out


def detect_kind(objects: List[bpy.types.Object]) -> str:
    if any(ob.type == "ARMATURE" for ob in objects):
        return "skeletal_mesh"
    return "static_mesh"


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _to_ascii_materials(objects):
    """导出前把「被导出对象用到的材质」名字临时转成 ASCII（中文 -> 拼音驼峰）。

    返回还原表 [(material, 原名)]，导出结束后由 _restore_material_names 还原，
    所以用户场景里的材质名**保持不变**，只有 FBX 里是 ASCII。
    这样目标软件（UE/Maya）不会出现中文材质名乱码。
    """
    from .bridge_names import to_ascii
    renamed = []
    seen = set()
    for ob in objects:
        for slot in getattr(ob, "material_slots", []):
            mat = getattr(slot, "material", None)
            if mat is None or id(mat) in seen:
                continue
            seen.add(id(mat))
            orig = mat.name
            new = to_ascii(orig)
            if new and new != orig:
                try:
                    mat.name = new
                    renamed.append((mat, orig))
                except Exception:
                    pass
    return renamed


def _restore_material_names(renamed):
    for mat, orig in reversed(renamed or []):
        try:
            mat.name = orig
        except Exception:
            pass


def export_selection(filepath: str, profile: Dict, objects: List[bpy.types.Object]) -> str:
    """把选中对象导出为 FBX。返回实际写出的文件路径。"""
    exp = dict(profile.get("export", {}))

    kwargs = {
        "filepath": filepath,
        "use_selection": True,
        "object_types": {"MESH", "ARMATURE", "EMPTY", "OTHER"},
        "global_scale": exp.get("global_scale", 1.0),
        "apply_unit_scale": exp.get("apply_unit_scale", True),
        "use_space_transform": exp.get("use_space_transform", True),
        "bake_space_transform": exp.get("bake_space_transform", False),
        "add_leaf_bones": exp.get("add_leaf_bones", False),
        "use_mesh_modifiers": True,
        "use_mesh_modifiers_render": False,
        "mesh_smooth_type": "FACE",
        "use_custom_props": False,
        "path_mode": exp.get("path_mode", "COPY"),
        "embed_textures": exp.get("embed_textures", True),
        "batch_mode": "OFF",
        "use_metadata": True,
    }
    for k in ("primary_bone_axis", "secondary_bone_axis", "axis_forward", "axis_up"):
        if k in exp:
            kwargs[k] = exp[k]

    # 只导出目标对象：临时把它们设为唯一选中
    prev_active = bpy.context.view_layer.objects.active
    prev_sel = list(bpy.context.selected_objects)
    bpy.ops.object.select_all(action="DESELECT")
    for ob in objects:
        ob.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]

    # 材质名临时转 ASCII（中文 -> 拼音），导出后还原
    renamed_mats = _to_ascii_materials(objects)
    try:
        _call(bpy.ops.export_scene.fbx, kwargs)
    finally:
        _restore_material_names(renamed_mats)
        bpy.ops.object.select_all(action="DESELECT")
        for ob in prev_sel:
            try:
                ob.select_set(True)
            except ReferenceError:
                pass
        if prev_active:
            try:
                bpy.context.view_layer.objects.active = prev_active
            except ReferenceError:
                pass

    return filepath


def export_obj(filepath: str, profile: Dict, objects: List[bpy.types.Object]) -> str:
    """把选中对象导出为 OBJ（ZBrush 目标专用：ZBrush 免对话框只认 OBJ）。

    只导网格（骨架/空物体对 ZBrush 无意义）；轴向用导出器默认值
    （profiles.json 的 blender>zbrush 路由已注明轴向待实机校准）。
    参数经 _call 的 RNA 反射过滤，跨 Blender 版本安全。
    返回实际写出的文件路径。
    """
    import os

    exp = dict(profile.get("export", {}))
    scale = float(exp.get("global_scale", 1.0))

    # 只导出目标对象：与 export_selection 相同的选中保存/恢复流程
    prev_active = bpy.context.view_layer.objects.active
    prev_sel = list(bpy.context.selected_objects)
    bpy.ops.object.select_all(action="DESELECT")
    meshes = [ob for ob in objects if ob.type == "MESH"]
    for ob in meshes:
        ob.select_set(True)
    if meshes:
        bpy.context.view_layer.objects.active = meshes[0]

    try:
        # use_selection（4.x+ 新名）/ export_selected_objects（旧名）：
        # _call 会按 RNA 反射自动剔除当前版本不存在的参数名
        _call(bpy.ops.wm.obj_export, {
            "filepath": filepath,
            "use_selection": True,
            "export_selected_objects": True,
            "global_scale": scale,
            "export_materials": False,
        })
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for ob in prev_sel:
            try:
                ob.select_set(True)
            except ReferenceError:
                pass
        if prev_active:
            try:
                bpy.context.view_layer.objects.active = prev_active
            except ReferenceError:
                pass

    if not os.path.isfile(filepath):
        raise RuntimeError(f"OBJ 导出未生成文件: {filepath}")
    return filepath


def export_to_payload_dir(payload_dir, asset_name: str, profile: Dict, objects) -> str:
    from pathlib import Path

    d = Path(payload_dir)
    d.mkdir(parents=True, exist_ok=True)
    fp = str(d / f"{asset_name}.fbx")
    return export_selection(fp, profile, objects)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def import_fbx(filepath: str, profile: Dict) -> List[bpy.types.Object]:
    imp = dict(profile.get("import", {}))
    kwargs = {
        "filepath": filepath,
        "global_scale": imp.get("global_scale", 1.0),
        "use_manual_orientation": imp.get("use_manual_orientation", False),
        "automatic_bone_orientation": imp.get("automatic_bone_orientation", True),
        "use_prepost_rot": imp.get("use_prepost_rot", True),
        "use_custom_props": True,
        "use_image_search": True,
    }

    before = set(bpy.data.objects)
    _call(bpy.ops.import_scene.fbx, kwargs)
    after = set(bpy.data.objects)

    imported = [ob for ob in (after - before) if ob.name in bpy.data.objects]
    imported.sort(key=lambda o: o.name)

    # 放进当前场景 / 当前集合，保证用户看得到
    scene = bpy.context.scene
    target_col = bpy.context.view_layer.active_layer_collection.collection
    for ob in imported:
        if ob.name not in scene.objects:
            try:
                target_col.objects.link(ob)
            except RuntimeError:
                pass

    # 选中并聚焦，符合「发过来就能用」的预期
    bpy.ops.object.select_all(action="DESELECT")
    for ob in imported:
        ob.select_set(True)
    if imported:
        bpy.context.view_layer.objects.active = imported[0]

    return imported


def import_obj(filepath: str, profile: Dict) -> List[bpy.types.Object]:
    """导入 OBJ（ZBrush 回传/发送的模型都是 OBJ）。参数同样经 RNA 反射过滤。

    放进当前场景 / 当前集合并选中的行为与 import_fbx 保持一致。
    """
    imp = dict(profile.get("import", {}))
    kwargs = {
        "filepath": filepath,
        "global_scale": imp.get("global_scale", 1.0),
    }

    before = set(bpy.data.objects)
    _call(bpy.ops.wm.obj_import, kwargs)
    after = set(bpy.data.objects)

    imported = [ob for ob in (after - before) if ob.name in bpy.data.objects]
    imported.sort(key=lambda o: o.name)

    scene = bpy.context.scene
    target_col = bpy.context.view_layer.active_layer_collection.collection
    for ob in imported:
        if ob.name not in scene.objects:
            try:
                target_col.objects.link(ob)
            except RuntimeError:
                pass

    bpy.ops.object.select_all(action="DESELECT")
    for ob in imported:
        ob.select_set(True)
    if imported:
        bpy.context.view_layer.objects.active = imported[0]

    return imported


def last_import_report(imported: List[bpy.types.Object]) -> str:
    if not imported:
        return "没有导入任何对象"
    tris = 0
    for ob in imported:
        if ob.type == "MESH" and ob.data:
            tris += sum(
                len(p.loop_indices) - 2 for p in ob.data.polygons if len(p.loop_indices) >= 3
            )
    return f"导入 {len(imported)} 个对象 / 约 {tris} 三角面"
