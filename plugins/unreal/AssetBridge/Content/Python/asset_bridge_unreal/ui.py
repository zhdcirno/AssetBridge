# -*- coding: utf-8 -*-
"""UE 编辑器 UI 扩展：工具栏启动按钮 + 内容浏览器右键菜单 + 菜单栏入口。

通过 unreal.ToolMenus 注入，无需 C++ 编译。所有点击只执行一行 Python
字符串命令（在游戏线程跑），逻辑都落在 asset_bridge_unreal 的既有函数上，
所以这里只是「接线」，不重复业务代码。

关键坑（已在 5.8 踩实）：
- 组合按钮（TOOL_BAR_COMBO_BUTTON）下拉菜单名 **必须等于 toolbar.menu_name + "." + 按钮名**。
  UE 的 FName 比较不区分大小写，find_menu("...LevelEditorToolBar") 会命中真实名为
  "...LevelEditorToolbar"（小写 b）的菜单；若用硬编码串去 register_menu，名字对不上，
  组合按钮找不到下拉菜单 → **整个按钮不渲染**。因此这里一律用 toolbar.menu_name。
- 注册完调用 refresh_all_widgets() 即可实时生效，无需重启编辑器。
- 菜单文本颜色由编辑器主题控制，无法强制绿/红；连接状态用「● 已连接 / ● 未连接」文字，
  并在每次打开菜单时实时计算（dynamic section）。
"""
import unreal


# 点击后执行的 Python 命令（在游戏线程执行）
_CMD_START = "import asset_bridge_unreal; asset_bridge_unreal.start()"
_CMD_STOP = "import asset_bridge_unreal; asset_bridge_unreal.stop()"
_CMD_SEND_BLENDER = "import asset_bridge_unreal; asset_bridge_unreal.send_selected_assets('blender')"
_CMD_SEND_MAYA = "import asset_bridge_unreal; asset_bridge_unreal.send_selected_assets('maya')"
_CMD_SEND_SP = "import asset_bridge_unreal; asset_bridge_unreal.send_selected_assets('substance_painter')"
_CMD_SEND_TOOLBAG_BAKE = ("import asset_bridge_unreal; "
                          "asset_bridge_unreal.send_selected_assets('toolbag', mode='bake')")
_CMD_SEND_TOOLBAG_RENDER = ("import asset_bridge_unreal; "
                            "asset_bridge_unreal.send_selected_assets('toolbag', mode='render')")
_CMD_SEND_ZBRUSH = "import asset_bridge_unreal; asset_bridge_unreal.send_selected_assets('zbrush')"
_CMD_SEND_TEX_PS = ("import asset_bridge_unreal; "
                    "asset_bridge_unreal.send_selected_textures('photoshop')")
_CMD_SEND_TEX_SAI = ("import asset_bridge_unreal; "
                     "asset_bridge_unreal.send_selected_textures('sai2')")

# 工具栏菜单候选名。
# 重要：现代 UE（5.x 后期）主工具栏是"拼"出来的，屏幕上每个按钮段都是一个独立子菜单
# （ModesToolBar=存档/选择模式、AssetsToolBar=+资产、PlayToolBar=播放…），
# 父级 LevelEditor.LevelEditorToolBar 既未注册也不生成 widget（往它加条目 = 等于没加）。
#
# SettingsToolbar 是工具栏最右侧那一段（设置/「⋯」所在处），放这里 = 显示在最右边。
# SettingsToolbar/PlayToolBar 都试过；PlayToolBar 是工具栏右侧那一段（播放控制所在），
# 确实渲染按钮，放这里最接近"最右边"。
_TOOLBAR_CANDIDATES = [
    "LevelEditor.LevelEditorToolBar.PlayToolBar",       # 右侧（播放控制段）
    "LevelEditor.LevelEditorToolBar.SettingsToolbar",   # 最右（设置/「⋯」段）
    "LevelEditor.LevelEditorToolBar.ModesToolBar",      # 左侧兜底（此前在这能显示）
    "LevelEditor.LevelEditorToolBar",
]

# 我们自己独占一个分区 —— UE 工具栏相邻分区之间会自动画竖分割线（正是想要的那个分隔）
_SECTION = "AssetBridge"

# 用引擎自带图标，风格与原生按钮一致（来自 EditorStyle）
_ICON = ("EditorStyle", "Icons.Refresh")


def _adapter_running():
    """本 UE 适配器是否在运行（后台轮询线程存活）。"""
    try:
        import asset_bridge_unreal as _mod
    except Exception:
        return False
    w = getattr(_mod, "_worker", None)
    try:
        return w is not None and w.is_alive()
    except Exception:
        return False


def is_connected():
    """连接状态 = 本适配器在运行 且 中转服务可达（读后台 worker 维护的缓存）。

    关键：这里**绝不**做同步 HTTP（菜单每次打开都会重算，同步打 /health 会在
    服务器没开时卡游戏线程 1 秒）。缓存由 __init__._worker_loop 在后台维护。
    """
    if not _adapter_running():
        return False
    try:
        import asset_bridge_unreal as _mod
        return bool(getattr(_mod, "_SERVER_REACHABLE", False))
    except Exception:
        return False


@unreal.uclass()
class _StatusSection(unreal.ToolMenuSectionDynamic):
    """每次菜单打开时实时重算连接状态的动态分区。"""

    @unreal.ufunction(override=True)
    def construct_sections(self, menu, context):
        if not _adapter_running():
            label = "● 桥接未启动"
        elif is_connected():
            label = "● 已连接桥接服务器"
        else:
            label = "● 服务器不可达"
        e = unreal.ToolMenuEntry()
        e.name = "ABStatus"
        e.type = unreal.MultiBlockType.MENU_ENTRY
        e.set_label(label)
        menu.add_menu_entry("Status", e)


def _py_entry(name, label, command, tooltip=""):
    e = unreal.ToolMenuEntry()
    e.name = name
    e.type = unreal.MultiBlockType.MENU_ENTRY
    e.set_label(label)
    if tooltip:
        e.set_tool_tip(tooltip)
    e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", command)
    return e


def _populate_actions(menu, prefix):
    """往一个菜单里填 启动/停止/发送×N + 贴图子菜单 + 实时状态
    （工具栏下拉/菜单栏/右键菜单共用）。"""
    menu.add_menu_entry("Bridge", _py_entry(prefix + "_Start", "启动桥接", _CMD_START))
    menu.add_menu_entry("Bridge", _py_entry(prefix + "_Stop", "停止桥接", _CMD_STOP))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_Blender", "发送选中 -> Blender", _CMD_SEND_BLENDER,
        "把内容浏览器里选中的资产导出并推送到 Blender"))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_Maya", "发送选中 -> Maya", _CMD_SEND_MAYA,
        "把内容浏览器里选中的资产导出并推送到 Maya"))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_SP", "发送选中 -> Substance Painter", _CMD_SEND_SP,
        "把内容浏览器里选中的资产导出并推送到 Substance Painter"))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_ToolbagBake", "发送选中 -> Toolbag（烘焙）", _CMD_SEND_TOOLBAG_BAKE,
        "导入高低模、建烘焙组并烘焙，烘焙贴图回传 Substance Painter"))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_ToolbagRender", "发送选中 -> Toolbag（渲染）", _CMD_SEND_TOOLBAG_RENDER,
        "只把模型/材质导入 Toolbag，用于渲染"))
    menu.add_menu_entry("Send", _py_entry(
        prefix + "_ZBrush", "发送选中 -> ZBrush", _CMD_SEND_ZBRUSH,
        "把内容浏览器里选中的 StaticMesh 导出 OBJ 推送到 ZBrush"
        "（OBJ 只对静态网格有效，其它类型自动跳过）"))
    # 贴图子菜单：只发 Texture2D，保存后自动覆盖原资产
    tex_sub = menu.add_sub_menu(menu.get_name(), "AssetBridge",
                                prefix + "TexSub", "发送贴图到编辑器")
    if tex_sub is not None:
        tex_sub.add_menu_entry("Send", _py_entry(
            prefix + "_TexPS", "Photoshop", _CMD_SEND_TEX_PS,
            "把选中的 Texture2D 导出 PNG 发到 PS；保存后 UE 自动重载"))
        tex_sub.add_menu_entry("Send", _py_entry(
            prefix + "_TexSAI", "SAI2", _CMD_SEND_TEX_SAI,
            "把选中的 Texture2D 导出 PNG 发到 SAI2；保存后 UE 自动重载"))
    menu.add_dynamic_section("Status", _StatusSection())


def _get_toolbar(menus, verbose=False):
    """取得 Level Editor 工具栏菜单。返回 (menu, name)；拿不到返回 (None, None)。

    优先用 extend_menu —— UE 官方推荐的「扩展引擎/他人注册的菜单」接口，关键优势
    是**允许在菜单尚未注册时先登记扩展**：工具栏的 ToolMenu 是在工具栏控件第一次
    构建时才注册的，在那之前 find_menu 一直返回 None；若用 find_menu 等它出现再注册，
    工具栏早构建完了，按钮就不会出现（这就是"注册成功却没按钮"的根因）。
    C++ 插件在 StartupModule 里用的正是 ExtendMenu。
    """
    for name in _TOOLBAR_CANDIDATES:
        m = None
        try:
            m = menus.extend_menu(name)
        except Exception as exc:
            if verbose:
                unreal.log_warning("[bridge] extend_menu({0}) 异常: {1}".format(name, exc))
            m = None
        if m is None:
            try:
                m = menus.find_menu(name)
            except Exception as exc:
                if verbose:
                    unreal.log_warning("[bridge] find_menu({0}) 异常: {1}".format(name, exc))
                m = None
        if m is not None:
            if verbose:
                unreal.log("[bridge] 取得工具栏菜单: {0}".format(name))
            return m, name
    return None, None


def _actual_name(menu, fallback):
    """取菜单对象的真实 menu_name（拼子菜单名必须用它，不能用手写串）。"""
    try:
        n = str(menu.menu_name)
        if n and n != "None":
            return n
    except Exception:
        pass
    return fallback


def _register_toolbar(menus, force=True, verbose=False):
    """在 Level Editor 工具栏加一个 AssetBridge 组合按钮（启动/停止/发送）。"""
    toolbar, tb_name = _get_toolbar(menus, verbose)
    if toolbar is None:
        if verbose:
            unreal.log_warning("[bridge] 所有候选名都拿不到工具栏菜单（extend/find 均失败）")
        return False

    actual = _actual_name(toolbar, tb_name)
    entry_name = "AssetBridge"
    sub_name = actual + "." + entry_name

    if not force and menus.find_menu(sub_name) is not None:
        return True  # 已注册过

    # 组合按钮本身（owner 必须是工具栏真实菜单名）
    combo = unreal.ToolMenuEntryExtensions.init_menu_entry(
        toolbar.menu_name,                                  # owner
        entry_name,                                         # name
        "AssetBridge",                                      # label
        "Asset Bridge 资产桥：启动桥接 / 发送选中资产到其他软件",  # tooltip
        unreal.ToolMenuStringCommandType.COMMAND,           # command type
        "",                                                 # custom type
        "",                                                 # command（组合按钮不执行命令，只展开下拉）
    )
    combo.type = unreal.MultiBlockType.TOOL_BAR_COMBO_BUTTON
    # 引擎自带图标，和工具栏其它原生按钮风格一致。
    # 注意：ToolMenuEntry **没有** icon 属性，必须用 set_icon(样式集, 图标名)。
    try:
        combo.set_icon(_ICON[0], _ICON[1])
    except Exception as exc:
        if verbose:
            unreal.log_warning("[bridge] 设置按钮图标失败: {0}".format(exc))

    # 下拉菜单：名字必须 = 工具栏真实菜单名 . 按钮名，否则按钮不渲染
    try:
        sub = menus.register_menu(sub_name, "", unreal.MultiBoxType.MENU, False)
    except Exception as exc:
        unreal.log_warning("[bridge] 注册 AssetBridge 下拉菜单失败: {0}".format(exc))
        return False
    if sub is None:
        unreal.log_warning("[bridge] register_menu 返回 None: {0}".format(sub_name))
        return False
    _populate_actions(sub, "AB_TB")

    # 自建分区 + 显式排到最后：
    #  - UE 会在相邻分区之间自动画竖分割线（就是想要的那个分隔）
    #  - 关键：新建的分区**必须**用 set_section_position 明确位置才会被生成，
    #    否则加进去也不渲染（这就是"换了目标就没了"的原因）
    #  - LAST = 落在该工具栏段的最右侧
    placed = False
    try:
        toolbar.add_section(_SECTION, "AssetBridge")
        menus.set_section_position(actual, _SECTION, "",
                                   unreal.ToolMenuInsertType.LAST)
        toolbar.add_menu_entry(_SECTION, combo)
        placed = True
        if verbose:
            unreal.log("[bridge] 已建分区 {0!r}（排最后）".format(_SECTION))
    except Exception as exc:
        if verbose:
            unreal.log_warning("[bridge] 自建分区方案失败，退回 File 分区: {0}".format(exc))
    if not placed:
        try:
            toolbar.add_menu_entry("File", combo)
        except Exception as exc:
            unreal.log_warning("[bridge] 工具栏 add_menu_entry 失败: {0}".format(exc))
            return False

    # 关键：重建「这个菜单」的 widget。
    # 官方 API 是 refresh_menu_widget（旧代码调的 refresh_menu 根本不存在，被 try 吞了，
    # 所以工具栏从来没被重建过 —— 这就是"注册成功却看不到按钮"的直接原因）。
    try:
        menus.refresh_menu_widget(actual)
    except Exception as exc:
        if verbose:
            unreal.log_warning("[bridge] refresh_menu_widget 异常: {0}".format(exc))
    try:
        menus.refresh_all_widgets()
    except Exception:
        pass
    unreal.log("[bridge] 已在工具栏注入 AssetBridge 按钮 (menu={0}, submenu={1})"
               .format(actual, sub_name))
    return True


def _register_main_menu(menus):
    """在顶部菜单栏加一个 AssetBridge 顶级菜单（备用入口，实时生效、最可靠）。"""
    bar = menus.find_menu("LevelEditor.MainMenu")
    if bar is None:
        unreal.log_warning("[bridge] 找不到 LevelEditor.MainMenu，跳过菜单栏入口")
        return
    try:
        sub = bar.add_sub_menu(bar.get_name(), "AssetBridge", "AssetBridgeMenu", "AssetBridge")
    except Exception as exc:
        unreal.log_warning("[bridge] 菜单栏 add_sub_menu 失败: {0}".format(exc))
        return
    if sub is None:
        return
    _populate_actions(sub, "AB_MB")
    unreal.log("[bridge] 已在菜单栏注入 AssetBridge 菜单")


def _register_content_browser(menus):
    """内容浏览器资产右键菜单：AssetBridge 分隔栏 + 发送到 二级菜单。"""
    cb = menus.find_menu("ContentBrowser.AssetContextMenu")
    if cb is None:
        unreal.log_warning("[bridge] 找不到 ContentBrowser.AssetContextMenu，跳过右键菜单")
        return

    cb.add_section("AssetBridge", "AssetBridge")  # 分隔栏（带标题）
    send_sub = cb.add_sub_menu(cb.get_name(), "AssetBridge", "ABSendSub", "发送到")
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_Blender", "Blender", _CMD_SEND_BLENDER))
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_Maya", "Maya", _CMD_SEND_MAYA))
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_SP", "Substance Painter", _CMD_SEND_SP))
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_ToolbagBake", "Toolbag（烘焙）", _CMD_SEND_TOOLBAG_BAKE))
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_ToolbagRender", "Toolbag（渲染）", _CMD_SEND_TOOLBAG_RENDER))
    send_sub.add_menu_entry("Send", _py_entry("AB_CB_ZBrush", "ZBrush", _CMD_SEND_ZBRUSH))
    tex_sub = cb.add_sub_menu(cb.get_name(), "AssetBridge", "ABTexSub", "发送贴图到编辑器")
    if tex_sub is not None:
        tex_sub.add_menu_entry("Send", _py_entry("AB_CB_TexPS", "Photoshop", _CMD_SEND_TEX_PS))
        tex_sub.add_menu_entry("Send", _py_entry("AB_CB_TexSAI", "SAI2", _CMD_SEND_TEX_SAI))
    cb.add_menu_entry("AssetBridge", _py_entry("AB_CB_Start", "启动桥接", _CMD_START))
    cb.add_menu_entry("AssetBridge", _py_entry("AB_CB_Stop", "停止桥接", _CMD_STOP))
    cb.add_dynamic_section("Status", _StatusSection())
    unreal.log("[bridge] 已在内容浏览器右键菜单注入 AssetBridge")


# ---- 延迟兜底（startup 过早时用；正常情况下同步注册已足够）----
# 句柄存在 unreal 模块上（reload 本模块也不会丢），保证反复 importlib.reload
# 不会残留孤儿回调疯狂刷警告。
_deferred_tries = 0
_deferred_dbg_logged = False
_DEFERRED_CHECK_EVERY = 30      # 每 30 帧（约 0.5s）才真正探测一次
_DEFERRED_MAX = 9000            # 约 2.5 分钟后停止重试


def _store_handle(h):
    try:
        unreal._ab_deferred_handle = h
    except Exception:
        pass


def _load_handle():
    return getattr(unreal, "_ab_deferred_handle", None)


def _try_deferred_toolbar(delta_time):
    global _deferred_tries, _deferred_dbg_logged
    _deferred_tries += 1
    # 静默轮询：只在每 N 帧真正探测一次，避免每帧刷警告
    if _deferred_tries % _DEFERRED_CHECK_EVERY != 0:
        return
    try:
        menus = unreal.ToolMenus.get()
        if _register_toolbar(menus, force=True):
            _release_deferred()
            return
    except Exception as exc:
        # 异常只报一次，避免刷屏
        if not _deferred_dbg_logged:
            _deferred_dbg_logged = True
            unreal.log_warning("[bridge] 延迟注册工具栏异常（仅报一次）: {0}".format(exc))
    if _deferred_tries >= _DEFERRED_MAX:
        unreal.log_warning(
            "[bridge] 长时间未等到 Level Editor 工具栏，已停止重试。"
            "需要工具栏按钮时请在 Output Log 执行："
            "import importlib, asset_bridge_unreal.ui as u;"
            "importlib.reload(u); u.register_ui()")
        _release_deferred()


def _release_deferred():
    h = _load_handle()
    if h is not None:
        try:
            unreal.unregister_slate_post_tick_callback(h)
        except Exception:
            pass
        _store_handle(None)


def _ensure_deferred():
    global _deferred_tries
    _deferred_tries = 0
    _release_deferred()
    try:
        h = unreal.register_slate_post_tick_callback(_try_deferred_toolbar)
        _store_handle(h)
        unreal.log("[bridge] Level Editor 工具栏尚未就绪，已挂延迟注册（就绪后自动出现，无需重启）")
    except Exception as exc:
        unreal.log_warning("[bridge] 无法挂延迟注册: {0}".format(exc))


def register_ui(force=True):
    """注册全部 UI 扩展。由 init_unreal.py 在编辑器启动时调用，也可热重载。"""
    try:
        menus = unreal.ToolMenus.get()
    except Exception as exc:
        unreal.log_error("[bridge] 获取 ToolMenus 失败: {0}".format(exc))
        return

    _register_content_browser(menus)   # 右键菜单
    _register_main_menu(menus)         # 菜单栏顶级菜单（最可靠，实时生效）

    # 工具栏：同步注册（带诊断日志）；拿不到菜单才挂延迟兜底
    if not _register_toolbar(menus, force, verbose=True):
        _ensure_deferred()
