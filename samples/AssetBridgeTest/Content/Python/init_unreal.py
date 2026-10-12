# -*- coding: utf-8 -*-
"""
UE 启动引导脚本 —— 把这段挂到 Project Settings > Python > Startup Scripts，
或在 Output Log 的 Python 命令行里直接执行。

也可以写进 DefaultEngine.ini：
    [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
    +StartupScripts=init_unreal.py
    +AdditionalPaths=Content/Python
"""
import sys
from pathlib import Path

# 把 asset_bridge_unreal 的父目录加进搜索路径
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 换成你自己的实际路径（或把整个 adapters/unreal 目录拷进 Content/Python）
# sys.path.append(r"D:/dev/asset-bridge/adapters/unreal")

try:
    import asset_bridge_unreal as bridge
except ImportError as exc:
    raise ImportError(
        f"找不到 asset_bridge_unreal 包，请在下面这行补上真实路径：{exc}"
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
