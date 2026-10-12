# -*- coding: utf-8 -*-
"""
Asset Bridge —— UE 引擎级插件启动脚本。

本文件位于插件目录 AssetBridge/Content/Python/init_unreal.py。
PythonScriptPlugin 在插件启用时会自动执行此脚本（无需再改工程的 DefaultEngine.ini），
因此本插件对所有使用同一引擎版本的项目通用。

安装方式：把整个 AssetBridge 目录复制到
    <UE安装目录>/Engine/Plugins/AssetBridge/
然后重启编辑器（或在 Plugins 浏览器里启用 Asset Bridge）。
"""
import sys
from pathlib import Path

# 把 asset_bridge_unreal 的父目录（即 Content/Python）加进搜索路径，
# 这样下面的 `import asset_bridge_unreal` 才能找到同级的包。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    import asset_bridge_unreal as bridge
except ImportError as exc:
    raise ImportError(
        "找不到 asset_bridge_unreal 包，请确认 AssetBridge 插件目录结构完整："
        "AssetBridge/Content/Python/asset_bridge_unreal/__init__.py 存在"
    ) from exc

bridge.start(
    server_url="http://127.0.0.1:27631",
    project="",          # 多工程时填工程名，用于隔离 inbox
    poll_interval=0.5,
    auto_receive=True,
    spawn_on_import=True,
)

# 注入编辑器 UI：工具栏 AssetBridge 按钮 + 内容浏览器右键菜单
# 注意：UI 注入失败绝不影响桥接本身（start 已先完成），只记录错误。
try:
    from asset_bridge_unreal import ui as _bridge_ui
    _bridge_ui.register_ui()
except Exception as exc:  # noqa: BLE001
    try:
        import unreal as _unreal
        _unreal.log_error("[bridge] AssetBridge UI 注册失败: {0}".format(exc))
    except Exception:
        pass
