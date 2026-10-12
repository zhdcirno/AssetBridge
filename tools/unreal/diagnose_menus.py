# -*- coding: utf-8 -*-
"""AssetBridge 工具栏诊断 v2 —— 挖出"真正在渲染"的工具栏菜单。

上一版发现：find_menu("LevelEditor.LevelEditorToolBar") 能拿到对象，
但 is_menu_registered()=False 且 refresh_menu_widget()=False —— 说明屏幕上的
工具栏不是从这个菜单渲染的。本脚本枚举所有已注册菜单，并逐个探测哪个菜单
"有 widget 生成"（= 真正在显示的），从而找到正确的目标名字。

在 UE 的 Output Log 命令行执行：
    py "E:/CirnoWorkSpace/AssetBridge/unreal/scripts/diagnose_menus.py"

把日志里所有 [bridge-diag] 行发给开发者。
"""
import unreal


def _log(m):
    unreal.log("=== [bridge-diag] " + str(m))


menus = unreal.ToolMenus.get()

# A) 菜单自定义 / profile（如果工具栏被"自定义"过，生成时会按自定义列表过滤条目）
for prop in ("customized_menus", "menu_profiles"):
    try:
        v = menus.get_editor_property(prop)
        _log("{0} ({1}) = {2!r}".format(prop, type(v).__name__, v))
    except Exception as exc:
        _log("读 {0} 失败: {1}".format(prop, exc))

# B) 枚举所有菜单对象（对象名是 RegisteredMenu_<n>，不是 ToolMenu_<n>）
names = set()
for i in range(6000):
    obj = unreal.find_object(None,
                             "/Engine/Transient.ToolMenus_0:RegisteredMenu_{0}".format(i))
    if not obj:
        continue
    try:
        mn = str(obj.menu_name)
    except Exception:
        continue
    if mn and mn != "None":
        names.add(mn)

_log("已注册菜单总数: {0}".format(len(names)))

lev = sorted(n for n in names if n.lower().startswith("leveleditor"))
tb = sorted(n for n in names if "toolbar" in n.lower())
_log("LevelEditor 相关 ({0}): {1}".format(len(lev), lev))
_log("含 toolbar ({0}): {1}".format(len(tb), tb))

# C) 逐个探测：registered? 以及 refresh_menu_widget 是否 True（True = 有 widget 生成自它）
probes = sorted(set(lev + tb) | {
    "LevelEditor.LevelEditorToolBar",
    "ContentBrowser.AssetContextMenu",
    "LevelEditor.MainMenu",
})
for n in probes:
    try:
        reg = menus.is_menu_registered(n)
    except Exception as exc:
        reg = "err:{0}".format(exc)
    try:
        hasw = menus.refresh_menu_widget(n)
    except Exception as exc:
        hasw = "err:{0}".format(exc)
    _log("PROBE {0} | registered={1} | hasWidget={2}".format(n, reg, hasw))

# D) 把所有含 ToolBar 的菜单名单独列一行，方便一眼看出真正的名字
_log(">>> 可能的目标名字: {0}".format(tb if tb else "（没有含 ToolBar 的菜单名）"))
_log("诊断v2 结束")
