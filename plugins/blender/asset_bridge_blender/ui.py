# -*- coding: utf-8 -*-
"""侧栏面板。只做 UI，不含业务逻辑。"""
import time

import bpy

from . import bridge_state, operators

# 连接状态由后台网络线程（operators._poll_loop）探测并更新，这里只读缓存，
# 面板 draw 绝不发起网络请求，避免没开服务器时主线程卡在超时上。
_STATUS = operators._STATUS


def _connected(url):
    """只读后台线程更新的状态缓存，绝不发起网络请求。"""
    return bool(_STATUS.get("ok", False))


def _draw_status(layout, url):
    """文字 + 颜色的连接状态行：绿(已连接) / 红(未连接) / 红(已暂停)。"""
    row = layout.row()
    if not bridge_state.is_running():
        row.alert = True
        row.label(text="● 桥接已暂停", icon="PAUSE")
    elif _connected(url):
        row.label(text="● 已连接桥接服务器", icon="CHECKMARK")
    else:
        row.alert = True
        row.label(text="● 未连接桥接服务器", icon="ERROR")


def tick_redraw():
    """每 2 秒触发一次 3D 视图重绘，让连接状态灯保持实时。"""
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == "VIEW_3D":
                    a.tag_redraw()
    except Exception:
        pass
    return 2.0


class BRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "Asset Bridge"
    bl_idname = "BRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Asset Bridge"

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences
        scene = context.scene

        # --- 桥接状态 ---------------------------------------------------
        box = layout.box()
        box.label(text="桥接状态", icon="WORLD")
        _draw_status(box, prefs.server_url)
        # 启动 / 暂停 并排
        row = box.row(align=True)
        row.operator("bridge.start", text="启动桥接", icon="PLAY")
        row.operator("bridge.pause", text="暂停桥接", icon="PAUSE")

        # --- 发送 -------------------------------------------------------
        box = layout.box()
        box.label(text="发送", icon="EXPORT")

        row = box.row()
        row.prop(prefs, "target_app", text="目标")

        if prefs.target_app in ("photoshop", "sai2"):
            # PS / SAI2 只能收发贴图：模型发送没有意义，只留贴图入口
            label = "Photoshop" if prefs.target_app == "photoshop" else "SAI2"
            box.label(text=f"只能发送贴图；在 {label} 里保存后自动重载", icon="INFO")
            box.operator("bridge.send_textures",
                         text=f"发送贴图到 {label}", icon="IMAGE_DATA")
        else:
            if prefs.target_app == "toolbag":
                row = box.row()
                row.prop(prefs, "toolbag_mode", text="模式", expand=True)
                if prefs.toolbag_mode == "bake":
                    box.label(text="烘焙：建烘焙组并烘焙，贴图回传 SP", icon="INFO")
                else:
                    box.label(text="渲染：只导入模型/材质", icon="INFO")
            if prefs.target_app == "zbrush":
                box.label(text="ZBrush 只收模型（OBJ）；材质贴图不随行", icon="INFO")
            # 说明文字改为鼠标悬停提示（见 __init__.py 里属性的 description）
            box.prop(scene, "bridge_asset_name", text="资产名")
            box.prop(scene, "bridge_note", text="备注")

            row = box.row()
            row.prop(prefs, "use_prefix", text="前缀")
            row.prop(prefs, "send_mode", text="")

            box.operator("bridge.send_selection", text="发送选中对象", icon="EXPORT")

            box.prop(prefs, "import_materials", text="带材质槽")
            box.prop(prefs, "import_textures", text="带贴图")

        # --- 接收 -------------------------------------------------------
        box = layout.box()
        box.label(text="接收", icon="IMPORT")
        box.prop(prefs, "auto_receive", text="自动接收")
        row = box.row()
        row.operator("bridge.pull_now", icon="IMPORT")
        row.operator("bridge.open_payload_dir", text="", icon="FILE_FOLDER")

        # --- 服务 -------------------------------------------------------
        box = layout.box()
        box.label(text="中转服务", icon="PREFERENCES")
        box.prop(prefs, "server_url", text="地址")
        box.prop(prefs, "project", text="项目")
        box.prop(prefs, "content_path", text="UE 路径")
        row = box.row()
        row.operator("bridge.test_connection", icon="PLUGIN")
        row.prop(prefs, "poll_interval", text="轮询")


CLASSES = [BRIDGE_PT_panel]
