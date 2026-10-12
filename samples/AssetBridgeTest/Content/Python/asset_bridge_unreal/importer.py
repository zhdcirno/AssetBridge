# -*- coding: utf-8 -*-
"""
UE 侧导入。

核心是 unreal.AssetImportTask + unreal.FbxImportUI 的「自动化导入」路径：
automated=True 表示不弹导入对话框，replace_existing=True 表示覆盖同名资产，
这两条是实现「一键发送 + 自动导入」和「增量更新同名资产」的关键。
"""
from typing import Dict, List

import unreal


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _normalize_content_path(path: str) -> str:
    path = (path or "/Game/Bridge").strip().rstrip("/")
    if not path.startswith("/Game"):
        path = "/Game/" + path.strip("/")
    return path


def ensure_folder(path: str) -> None:
    if not unreal.EditorAssetLibrary.does_directory_exist(path):
        unreal.EditorAssetLibrary.make_directory(path)


# ---------------------------------------------------------------------------
# 选项构造
# ---------------------------------------------------------------------------


def _build_import_ui(record: dict, profile: Dict) -> unreal.FbxImportUI:
    imp = dict(profile.get("import", {}))
    opts = record.get("options", {})
    kind = record.get("kind", "static_mesh")

    ui = unreal.FbxImportUI()
    ui.set_editor_property("automated_import_should_detect_type", False)
    ui.set_editor_property("import_mesh", True)
    ui.set_editor_property(
        "import_materials", bool(opts.get("import_materials", True))
    )
    ui.set_editor_property(
        "import_textures", bool(opts.get("import_textures", True))
    )
    ui.set_editor_property(
        "import_animations", bool(opts.get("import_animations", False))
    )

    if kind == "skeletal_mesh":
        ui.set_editor_property(
            "mesh_type_to_import", unreal.FBXImportType.FBXIT_SKELETAL_MESH
        )
        ui.set_editor_property(
            "original_import_type", unreal.FBXImportType.FBXIT_SKELETAL_MESH
        )
        ui.set_editor_property("import_as_skeletal", True)
        ui.set_editor_property("create_physics_asset", False)
        data = ui.get_editor_property("skeletal_mesh_import_data")
        data.set_editor_property("convert_scene", imp.get("convert_scene", True))
        data.set_editor_property(
            "convert_scene_unit", imp.get("convert_scene_unit", True)
        )
        data.set_editor_property(
            "force_front_x_axis", imp.get("force_front_x_axis", False)
        )
        data.set_editor_property("import_morph_targets", True)
        data.set_editor_property("use_t0_as_ref_pose", True)
    else:
        ui.set_editor_property(
            "mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH
        )
        ui.set_editor_property(
            "original_import_type", unreal.FBXImportType.FBXIT_STATIC_MESH
        )
        data = ui.get_editor_property("static_mesh_import_data")
        data.set_editor_property(
            "combine_meshes", bool(opts.get("combine_meshes", False))
        )
        data.set_editor_property("convert_scene", imp.get("convert_scene", True))
        data.set_editor_property(
            "convert_scene_unit", imp.get("convert_scene_unit", True)
        )
        data.set_editor_property("generate_lightmap_u_vs", True)
        data.set_editor_property("auto_generate_collision", True)

    return ui


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def import_model(record: dict, fbx_path: str, profile: Dict) -> Dict:
    """把一个 FBX 导入成 UE 资产。

    返回结构化结果，而不是一串混合路径 —— UE 的 imported_object_paths 里
    网格和材质是混在一起的（实测返回 ['M_A','M_B','SM_Box']），调用方按
    「取第一个」的写法会拿到材质而不是网格。

        {
          "mesh":      "/Game/Bridge/SM_Box",   # 网格资产路径，没有则为 ""
          "materials": ["/Game/Bridge/M_A", ...],
          "all":       [ ... 全部导入路径 ... ],
        }
    """
    content_path = _normalize_content_path(
        (record.get("to") or {}).get("content_path", "/Game/Bridge")
    )
    ensure_folder(content_path)

    asset_name = record["asset_name"]
    mode = (record.get("options") or {}).get("mode", "update")

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", fbx_path)
    task.set_editor_property("destination_path", content_path)
    # update 模式固定名字 => 覆盖同名资产（增量更新）；
    # create 模式留空 => UE 自己生成唯一名
    task.set_editor_property("destination_name", asset_name if mode == "update" else "")
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)   # 不弹窗
    task.set_editor_property("save", True)
    task.set_editor_property("options", _build_import_ui(record, profile))

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return classify_imported(task.get_editor_property("imported_object_paths") or [])


def classify_imported(paths) -> Dict:
    """把 UE 返回的混合路径列表按资产类型拆开。

    优先用真实资产类型判断；类型拿不到（commandlet 下偶发）则退回按名字匹配
    —— 我们自己的命名规则是 SM_ 前缀为静态网格、SK_ 为骨骼网格。
    """
    paths = list(paths or [])
    mesh, materials, others = "", [], []

    for p in paths:
        kind = ""
        try:
            asset = unreal.EditorAssetLibrary.load_asset(p)
            if asset:
                if isinstance(asset, unreal.StaticMesh):
                    kind = "static_mesh"
                elif isinstance(asset, unreal.SkeletalMesh):
                    kind = "skeletal_mesh"
                elif isinstance(asset, (unreal.MaterialInterface, unreal.Material)):
                    kind = "material"
                elif isinstance(asset, unreal.Texture):
                    kind = "texture"
        except Exception:
            kind = ""

        if not kind:  # 兜底：按名字猜
            name = str(p).rstrip("/").split("/")[-1].split(".")[0]
            if name.startswith("SM_") or name.startswith("SK_"):
                kind = "mesh"
            elif name.startswith("M_") or name.startswith("MI_"):
                kind = "material"
            elif name.startswith("T_"):
                kind = "texture"

        if kind in ("static_mesh", "skeletal_mesh", "mesh"):
            if not mesh:
                mesh = p
            else:
                others.append(p)
        elif kind == "material":
            materials.append(p)
        else:
            others.append(p)

    return {
        "mesh": mesh,
        "materials": materials,
        "all": paths,
        "others": others,
    }


# ---------------------------------------------------------------------------
# 贴图通道识别
# ---------------------------------------------------------------------------
# 文件名里的通道关键字 -> (规范显示名, 压缩设置, 是否 sRGB)。
#   BaseColor 是颜色贴图 : TC_DEFAULT   + sRGB
#   Normal    是法线贴图 : TC_NORMALMAP + 线性（关 sRGB）
#   ORM       是打包遮罩 : TC_MASKS     + 线性（关 sRGB）
#   其余灰度数据(Rough/Metal/AO/Height/Opacity) : TC_GRAYSCALE + 线性
#
# UE 5.8 实测：文件名里带 "normal" 时 UE 会自己按法线导入，但 ORM / Roughness
# 这类数据贴图会被当成颜色图（TC_DEFAULT + sRGB=True），渲染时被错误地做
# gamma 校正 —— 这是必须由我们显式修正的真实缺陷。
_TEXTURE_CHANNELS = (
    ("basecolor", "BaseColor", "TC_DEFAULT", True),
    ("normal", "Normal", "TC_NORMALMAP", False),
    ("orm", "ORM", "TC_MASKS", False),
    ("roughness", "Roughness", "TC_GRAYSCALE", False),
    ("metallic", "Metallic", "TC_GRAYSCALE", False),
    ("ao", "AO", "TC_GRAYSCALE", False),
    ("height", "Height", "TC_GRAYSCALE", False),
    ("opacity", "Opacity", "TC_GRAYSCALE", False),
    ("emissive", "Emissive", "TC_DEFAULT", True),
)

# 认不出通道时的兜底：(显示名, 压缩设置, sRGB)
DEFAULT_CHANNEL = ("Tex", "TC_DEFAULT", True)


def _channel_info(path: str):
    """按文件名推断贴图通道，返回 (显示名, 压缩设置名, 是否 sRGB)。"""
    lower = (path or "").lower()
    for key, display, comp, srgb in _TEXTURE_CHANNELS:
        if key in lower:
            return display, comp, srgb
    return DEFAULT_CHANNEL


def _channel_from_name(path: str) -> str:
    """兼容旧调用：只返回通道显示名（BaseColor / Normal / ORM ...）。"""
    return _channel_info(path)[0]


def _safe_set(obj, name: str, value) -> bool:
    """跨版本安全设值：属性不存在时打 warning 跳过，别把整条链路搞挂。"""
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception as exc:
        unreal.log_warning(
            f"[bridge] 贴图导入：当前引擎不支持属性 {name}（{exc}），已跳过"
        )
        return False


def _compression_enum(name: str):
    """把 'TC_NORMALMAP' 这类名字解析成 unreal.TextureCompressionSettings 枚举。"""
    try:
        return getattr(unreal.TextureCompressionSettings, name)
    except Exception as exc:
        unreal.log_warning(f"[bridge] 压缩设置 {name} 不可用（{exc}）")
        return None


def _apply_texture_settings(asset_path: str, comp_name: str, srgb: bool) -> None:
    """导入后兜底：把压缩设置 / sRGB 直接落到 Texture 资产上再保存。

    工厂的属性在个别版本上会被导入流程覆盖，所以在资产层面再设一次最稳。
    sRGB 尤其关键：法线 / 遮罩 / AO 必须关掉，否则会被做 gamma 校正。
    """
    try:
        asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    except Exception as exc:
        unreal.log_warning(f"[bridge] 载入贴图失败 {asset_path}: {exc}")
        return
    if not isinstance(asset, unreal.Texture):
        return

    changed = False
    comp = _compression_enum(comp_name)
    if comp is not None:
        changed |= _safe_set(asset, "compression_settings", comp)
    changed |= _safe_set(asset, "srgb", bool(srgb))

    if not changed:
        return
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(asset)
    except Exception:
        try:
            unreal.EditorAssetLibrary.save_asset(asset_path)
        except Exception as exc:  # noqa: BLE001
            unreal.log_warning(f"[bridge] 保存贴图失败 {asset_path}: {exc}")


def import_textures(record: dict, file_paths: List[str]) -> List[str]:
    """Substance Painter 回传贴图时使用。

    UE 5.8 实测：默认导入会把 ORM / 粗糙度这类数据贴图也当成颜色图
    （TC_DEFAULT + sRGB=True），渲染时被错误地做 gamma 校正。所以这里
    按通道名显式指定压缩设置与 sRGB，并让「增量更新」覆盖同名资产。
    """
    content_path = _normalize_content_path(
        (record.get("to") or {}).get("content_path", "/Game/Bridge/Textures")
    )
    ensure_folder(content_path)
    imported = []

    for fp in file_paths:
        display, comp_name, srgb = _channel_info(fp)
        name = f"T_{record['asset_name']}_{display}"

        task = unreal.AssetImportTask()
        task.set_editor_property("filename", fp)
        task.set_editor_property("destination_path", content_path)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("automated", True)
        task.set_editor_property("save", True)

        # 让 TextureFactory 在导入阶段就带上正确的压缩设置（设不了后面还会补）
        factory = unreal.TextureFactory()
        comp = _compression_enum(comp_name)
        if comp is not None:
            _safe_set(factory, "compression_settings", comp)
        # no_alpha 在部分版本的 TextureFactory 上未必存在，缺失不影响导入
        _safe_set(factory, "no_alpha", display == "BaseColor")
        _safe_set(task, "factory", factory)

        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        paths = list(task.get_editor_property("imported_object_paths") or [])
        imported += paths

        # 资产层面兜底：压缩设置 / sRGB 再设一遍并保存
        for p in paths:
            _apply_texture_settings(p, comp_name, srgb)

    return imported


def _spawn_static_mesh(subsystem, asset: unreal.StaticMesh,
                       location: unreal.Vector):
    """把一个 StaticMesh 摆进当前关卡，返回生成的 actor，失败返回 None。

    UE 5.8 实测踩到的两个坑：
        1. spawn_actor_from_object() 失败时不抛异常，只打一条
           "SpawnActorFromObject. No actor was spawned." 然后返回 None；
        2. 更麻烦的是它在这台机器上根本摆不进去（同一轮里
           spawn_actor_from_class 却能成功，说明世界本身是好的）。
    所以这里必须有退路：先试 from_object，拿不到 actor 就改成
    「先生成 StaticMeshActor，再把网格挂上去」，并同步改掉 actor 标签，
    免得关卡里堆一堆 StaticMeshActor_0。
    """
    # 方式 1：直接按资产摆放
    if subsystem is not None:
        try:
            actor = subsystem.spawn_actor_from_object(asset, location)
            if actor is not None:
                return actor
        except Exception as exc:
            unreal.log_warning(f"[bridge] spawn_actor_from_object 失败: {exc}")
    else:  # UE4 / 老 API
        try:
            actor = unreal.EditorLevelLibrary.spawn_actor_from_object(asset, location)
            if actor is not None:
                return actor
        except Exception as exc:
            unreal.log_warning(f"[bridge] EditorLevelLibrary 摆放失败: {exc}")

    # 方式 2：先生成空的 StaticMeshActor，再挂网格（UE 5.8 上这条才真的能用）
    try:
        if subsystem is not None:
            actor = subsystem.spawn_actor_from_class(unreal.StaticMeshActor, location)
        else:
            actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
                unreal.StaticMeshActor, location
            )
        if actor is None:
            return None
        actor.static_mesh_component.set_static_mesh(asset)
        actor.set_actor_label(asset.get_name())
        return actor
    except Exception as exc:
        unreal.log_warning(f"[bridge] spawn_actor_from_class 退路也失败: {exc}")
        return None


def spawn_into_level(asset_paths: List[str]) -> int:
    """导入后把静态网格体直接摆进当前关卡 —— 「发过来就能看见」。

    注意：UE 的摆放 API 失败时返回 None 而不抛异常，必须判断返回值，
    否则没有关卡/世界不可用时也会虚报「摆放成功」。
    """
    placed = 0
    try:
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    except Exception:
        subsystem = None

    for p in asset_paths:
        asset = unreal.EditorAssetLibrary.load_asset(p)
        if not isinstance(asset, unreal.StaticMesh):
            continue

        actor = _spawn_static_mesh(subsystem, asset, unreal.Vector(0, 0, 0))
        if actor is not None:
            placed += 1
        else:
            unreal.log_warning(
                f"[bridge] 摆放 {p} 未生成 actor（当前世界不可用，或该资产类型不支持摆放）"
            )
    return placed


# ---------------------------------------------------------------------------
# Groom（毛发）：Alembic(.abc) -> UE Groom 资产 + 毛发材质
# ---------------------------------------------------------------------------


def import_groom(record: dict, abc_path: str, profile: Dict) -> Dict:
    """把 Alembic 毛发缓存导入为 UE 的 Groom 资产，并配套创建毛发(Hair)材质。

    关键坑（UE 5.x 实机）：AssetImportTask.options 必须传
    ``unreal.GroomAssetImportData(options)`` 而不能直接传 GroomImportOptions，
    否则转换设置（轴向/缩放）不会生效。
    """
    content_path = _normalize_content_path(
        (record.get("to") or {}).get("content_path", "/Game/Bridge/Groom")
    )
    ensure_folder(content_path)

    asset_name = record["asset_name"]
    mode = (record.get("options") or {}).get("mode", "update")
    bind = bool((record.get("options") or {}).get("bind_to_character", False))

    conversion = unreal.GroomConversionSettings(
        rotation=[90.0, 0.0, 0.0], scale=[1.0, -1.0, 1.0])
    options = unreal.GroomImportOptions()
    try:
        options.set_editor_property("conversion_settings", conversion)
    except Exception as exc:
        unreal.log_warning(f"[bridge] 设置 Groom 转换参数失败: {exc}")

    try:
        groom_data = unreal.GroomAssetImportData(options)
    except Exception:
        groom_data = options   # 兜底：个别版本可直接传 options

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", abc_path)
    task.set_editor_property("destination_path", content_path)
    task.set_editor_property("destination_name", asset_name if mode == "update" else "")
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", True)
    task.set_editor_property("options", groom_data)

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    paths = list(task.get_editor_property("imported_object_paths") or [])

    groom_path = ""
    for p in paths:
        try:
            asset = unreal.EditorAssetLibrary.load_asset(p)
        except Exception:
            asset = None
        if isinstance(asset, unreal.GroomAsset):
            groom_path = p
            break
    if not groom_path and paths:
        groom_path = paths[0]

    mat_path = ""
    if groom_path:
        mat_path = _create_hair_material(asset_name, content_path, groom_path)
        if bind:
            _bind_groom_to_selected(groom_path)

    return {
        "groom": groom_path,
        "material": mat_path,
        "all": paths,
        "message": f"导入 Groom {groom_path or '(无)'}" + (f"，毛发材质 {mat_path}" if mat_path else ""),
    }


def _create_hair_material(asset_name: str, content_path: str, groom_path: str) -> str:
    """创建 Hair 着色模型的材质，并指派给 Groom 资产的材质槽（best-effort）。"""
    mat_name = f"M_{asset_name}"
    try:
        factory = unreal.MaterialFactoryNew()
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            mat_name, content_path, unreal.Material, factory)
        if mat is None:
            return ""
        # UE5 单着色模型接口：先试 shading_model，再试 shading_models 数组
        try:
            mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_HAIR)
        except Exception:
            try:
                models = mat.get_editor_property("shading_models")
                models.clear()
                models.append(unreal.MaterialShadingModel.MSM_HAIR)
                mat.set_editor_property("shading_models", models)
            except Exception as exc:
                unreal.log_warning(f"[bridge] 设置毛发着色模型失败: {exc}")
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
        # 指派给 Groom 资产的材质槽
        try:
            groom = unreal.EditorAssetLibrary.load_asset(groom_path)
            if groom is not None:
                groom.set_editor_property("material", mat)
                unreal.EditorAssetLibrary.save_loaded_asset(groom)
        except Exception as exc:
            unreal.log_warning(
                f"[bridge] 指派毛发材质失败（可稍后在 Groom 细节面板手动选）: {exc}")
        return mat.get_path_name()
    except Exception as exc:
        unreal.log_warning(f"[bridge] 创建毛发材质失败: {exc}")
        return ""


def _bind_groom_to_selected(groom_path: str) -> bool:
    """把 Groom 绑定到当前关卡选中的骨骼网格体（best-effort）。

    找不到选中的 SkeletalMeshActor 就跳过，绝不报错。
    """
    try:
        actors = unreal.EditorLevelLibrary.get_selected_level_actors()
    except Exception:
        return False
    target = None
    for a in actors:
        if isinstance(a, unreal.SkeletalMeshActor):
            target = a
            break
    if target is None:
        return False
    skm = target.skeletal_mesh_component
    if skm is None:
        return False
    groom = unreal.EditorAssetLibrary.load_asset(groom_path)
    try:
        skm.add_groom_binding(groom)
        return True
    except Exception as exc:
        unreal.log_warning(
            f"[bridge] 自动绑定 Groom 失败（可手动在网格体细节面板添加 Groom）: {exc}")
        return False

