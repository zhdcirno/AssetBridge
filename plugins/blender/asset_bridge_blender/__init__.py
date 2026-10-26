# -*- coding: utf-8 -*-
"""
Asset Bridge :: Blender 适配器
============================================================================
安装：把整个 asset_bridge_blender 文件夹放到 Blender 用户级插件目录
      (Windows 默认
       %APPDATA%\\Blender Foundation\\Blender\\5.0\\scripts\\addons)
      然后在 Edit > Preferences > Add-ons 里搜索 "Asset Bridge" 勾选启用。
依赖：需要先启动 bridge_server（python server/server.py --root runtime）
============================================================================
"""

# ---------------------------------------------------------------------------
# bl_info 必须放在文件最顶部（在任何 import 之前）。
# Blender 用静态解析扫描模块顶层的 bl_info 赋值来决定插件是否出现在列表里；
# 如果把 import 写在它前面，子模块导入一旦失败，整个插件在列表里直接消失，
# 连「启用」按钮都看不到。所以这里 bl_info 先行，子模块导入延迟到 register()。
# ---------------------------------------------------------------------------
bl_info = {
    "name": "Asset Bridge (Blender)",
    "author": "asset-bridge",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar > Asset Bridge",
    "description": "与 UE / Maya / Substance Painter / Toolbag / Photoshop / SAI2 互发模型、贴图与骨架",
    "category": "Import-Export",
}

import bpy

# 子模块延迟导入：放到 register() 里执行，并用 try 包裹。
# 这样即使子模块（bridge_state / operators / ui）加载失败，bl_info 已经
# 被 Blender 静态解析拿到，插件仍会出现在列表里（只是暂时无法启用），
# 不会「列表里根本找不到」。（见 Bug1 修复说明）
CLASSES = []  # 在 register() 里填充，避免模块级就 import 子模块


# ---------------------------------------------------------------------------
# 插件偏好
# ---------------------------------------------------------------------------


class BridgeAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    server_url: bpy.props.StringProperty(
        name="服务地址",
        description="中转服务地址",
        default="http://127.0.0.1:27631",
    )
    project: bpy.props.StringProperty(
        name="项目", description="用于区分不同 UE 工程/不同项目", default=""
    )
    content_path: bpy.props.StringProperty(
        name="UE 内容路径", description="资产导入到 UE 的哪个目录", default="/Game/Bridge"
    )
    target_app: bpy.props.EnumProperty(
        name="目标软件",
        items=[
            ("unreal", "Unreal Engine", "发送到 UE"),
            ("maya", "Maya", "发送到 Maya"),
            ("substance_painter", "Substance Painter", "发送到 SP"),
            ("toolbag", "Marmoset Toolbag", "发送到 Toolbag（烘焙/渲染）"),
            ("photoshop", "Photoshop", "发送贴图到 PS，保存后自动重载"),
            ("sai2", "SAI2", "发送贴图到 SAI2，保存后自动重载"),
            ("zbrush", "ZBrush", "发送到 ZBrush（模型，OBJ）"),
            ("blender", "Blender", "发给另一个 Blender 实例"),
        ],
        default="unreal",
    )
    toolbag_mode: bpy.props.EnumProperty(
        name="Toolbag 模式",
        items=[
            ("bake", "烘焙", "导入高低模、建烘焙组并烘焙，贴图回传 SP"),
            ("render", "渲染", "只导入模型/材质，用于渲染"),
        ],
        default="bake",
        description="仅当目标软件为 Marmoset Toolbag 时有效",
    )
    send_mode: bpy.props.EnumProperty(
        name="模式",
        items=[
            ("update", "更新同名资产", "覆盖已有资产，保留 UE 侧引用"),
            ("create", "始终新建", "每次都生成新资产"),
        ],
        default="update",
    )
    use_prefix: bpy.props.BoolProperty(
        name="自动前缀", description="按 SM_/SK_ 规则加前缀", default=True
    )
    import_materials: bpy.props.BoolProperty(name="带材质槽", default=True)
    import_textures: bpy.props.BoolProperty(name="带贴图", default=True)
    combine_meshes: bpy.props.BoolProperty(name="合并网格", default=False)
    import_animations: bpy.props.BoolProperty(name="带动画", default=False)

    auto_receive: bpy.props.BoolProperty(
        name="自动接收", description="后台轮询收件箱并自动导入", default=True
    )
    poll_interval: bpy.props.FloatProperty(
        name="轮询间隔(秒)",
        default=0.5,
        min=0.2,
        max=10.0,
        description="越小越实时，越大越省 CPU",
    )
    direct_write: bpy.props.BoolProperty(
        name="直写负载目录",
        description="同机时直接写进服务端目录（更快）；关掉则用 HTTP 上传",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "server_url")
        col.prop(self, "project")
        col.prop(self, "content_path")
        col.prop(self, "auto_receive")
        col.prop(self, "poll_interval")
        col.prop(self, "direct_write")


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register():
    """注册插件类。子模块延迟导入，避免单个子模块失败导致插件整体不显示。"""
    global CLASSES
    try:
        from . import bridge_state, operators, ui
    except Exception as exc:  # noqa: BLE001 - 子模块故障不能让插件完全消失
        print("[bridge] 子模块导入失败，插件未完全启用:", exc)
        return
    CLASSES = [BridgeAddonPreferences] + operators.CLASSES + ui.CLASSES

    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.bridge_asset_name = bpy.props.StringProperty(
        name="资产名",
        description="接收方按此名存成文件/资产；留空则自动用物体名；同名再发=覆盖更新。"
                    "鼠标悬停此处即可看到说明。",
        default="",
    )
    bpy.types.Scene.bridge_note = bpy.props.StringProperty(
        name="备注",
        description="随资产发送给对方的一段说明文字，仅给人看、不影响文件名，"
                    "方便在另一端识别用途/来源。鼠标悬停此处即可看到说明。",
        default="",
    )

    bridge_state.load_profiles(refresh=True)
    operators.start_poll_thread()
    bpy.app.timers.register(operators.tick, first_interval=2.0, persistent=True)
    bpy.app.timers.register(ui.tick_redraw, first_interval=2.0, persistent=True)
    print("[bridge] Blender 适配器已加载")


def unregister():
    try:
        from . import bridge_state, operators, ui
    except Exception:
        bridge_state = None
        operators = None
        ui = None

    try:
        bpy.app.timers.unregister(operators.tick)
    except Exception:
        pass
    try:
        if ui is not None:
            bpy.app.timers.unregister(ui.tick_redraw)
    except Exception:
        pass
    try:
        if operators is not None:
            operators.stop_poll_thread()
    except Exception:
        pass
    try:
        del bpy.types.Scene.bridge_asset_name
    except Exception:
        pass
    try:
        del bpy.types.Scene.bridge_note
    except Exception:
        pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    try:
        if bridge_state is not None:
            bridge_state.reset_client()
    except Exception:
        pass
