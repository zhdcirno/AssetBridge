# -*- coding: utf-8 -*-
"""插件级单例：客户端实例 + 换算档案加载。"""
import json
from pathlib import Path
from typing import Dict, Optional

from .bridge_client import BridgeClient

_client: Optional[BridgeClient] = None
_profiles: Optional[Dict] = None
_running: bool = True     # 桥接是否在运行（默认开启）


def is_running() -> bool:
    """桥接是否处于运行状态（面板上的「启动/暂停」控制）。"""
    return _running


def set_running(flag: bool) -> None:
    """设置桥接运行状态。暂停后定时器不再轮询/自动接收。"""
    global _running
    _running = bool(flag)

DEFAULT_PROFILE = {
    "format": "fbx",
    "export": {"global_scale": 1.0, "apply_unit_scale": True, "use_space_transform": True},
    "import": {"global_scale": 1.0},
}


def get_client(base_url: str = "", version: str = "", project: str = "") -> BridgeClient:
    global _client
    if _client is None:
        _client = BridgeClient(
            base_url=base_url or "http://127.0.0.1:27631",
            app="blender",
            version=version,
            project=project,
        )
    else:
        if base_url:
            _client.base_url = base_url.rstrip("/")
        if project:
            _client.project = project
        if version:
            _client.version = version
    return _client


def reset_client() -> None:
    global _client
    _client = None


def load_profiles(refresh: bool = False) -> Dict:
    global _profiles
    if _profiles is not None and not refresh:
        return _profiles
    path = Path(__file__).resolve().parent / "profiles.json"
    try:
        _profiles = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[bridge] 读取 profiles.json 失败（{exc}），使用内置默认档案")
        _profiles = {"apps": {}, "routes": {}, "naming": {}}
    return _profiles


def route_profile(src: str, dst: str) -> Dict:
    """取 src>dst 的换算档案，找不到就返回默认。"""
    profiles = load_profiles()
    return profiles.get("routes", {}).get(f"{src}>{dst}", DEFAULT_PROFILE)


def app_info(name: str) -> Dict:
    return load_profiles().get("apps", {}).get(name, {})


def naming_rule(kind: str, name: str) -> str:
    """按命名规则加前缀。已经是目标前缀开头的名字不再重复加（幂等）。

    否则「SM_CalibCube」会被加成「SM_SM_CalibCube」。
    """
    rules = load_profiles().get("naming", {})
    tpl = rules.get(kind)
    if not tpl:
        return name
    prefix = tpl.split("{name}", 1)[0]
    if prefix and name.startswith(prefix):
        return name
    return tpl.replace("{name}", name)
