#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
换算档案同步 / 一致性自检。

背景：profiles.json 被复制进每个适配器目录（插件要能独立安装），
结果出现了「四份档案数值漂移」—— shared 里 unreal>blender 是 0.01，
blender 插件里是 1.0，两边都不报错，但反向链路会静默缩 100 倍。

约定：
    shared/profiles.json  = 唯一事实源
    各适配器副本         = shared 内容 + 该适配器独有的实测注解（_measured_* 之类）

用法：
    python tests/profiles_sync.py           # 只检查，不一致就退出码 1
    python tests/profiles_sync.py --sync    # 把 shared 同步到所有副本（保留副本独有字段）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared" / "profiles.json"

COPIES = {
    "blender": ROOT / "blender" / "addon" / "asset_bridge_blender" / "profiles.json",
    "unreal": ROOT / "unreal" / "plugin" / "asset_bridge_unreal" / "profiles.json",
    "maya": ROOT / "maya" / "plugin" / "asset_bridge_maya" / "profiles.json",
}

# 需要做一致性比对的路径（点分）
CHECK_PATHS = [
    "apps.blender.up_axis",
    "apps.blender.unit",
    "apps.unreal.up_axis",
    "apps.unreal.unit",
    "apps.maya.up_axis",
    "apps.maya.unit",
    "apps.substance_painter.up_axis",
    "routes.blender>unreal.format",
    "routes.blender>unreal.export.global_scale",
    "routes.blender>unreal.import.convert_scene_unit",
    "routes.unreal>blender.import.global_scale",
    "routes.maya>unreal.export.scale_factor",
    "routes.unreal>maya.import.scale_factor",
    "routes.blender>maya.export.global_scale",
    "routes.blender>maya.import.scale_factor",
    "routes.blender>substance_painter.export.global_scale",
    "naming.static_mesh",
    "naming.skeletal_mesh",
]


def dig(obj: Any, dotted: str) -> Tuple[bool, Any]:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def load(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check() -> List[str]:
    """返回不一致描述列表，空列表表示全部一致。"""
    shared = load(SHARED)
    problems: List[str] = []

    for name, path in COPIES.items():
        if not path.exists():
            problems.append(f"[{name}] 副本不存在：{path}")
            continue
        try:
            copy = load(path)
        except Exception as exc:
            problems.append(f"[{name}] 副本不是合法 JSON：{exc}")
            continue

        for dotted in CHECK_PATHS:
            ok_s, val_s = dig(shared, dotted)
            ok_c, val_c = dig(copy, dotted)
            if not ok_s:
                continue  # shared 里没有这项，不强制
            if not ok_c:
                problems.append(f"[{name}] 缺失字段 {dotted}（shared 中为 {val_s!r}）")
            elif val_s != val_c:
                problems.append(
                    f"[{name}] 字段漂移 {dotted}：shared={val_s!r} 副本={val_c!r}"
                )
    return problems


def merge(base: Any, extra: Any) -> Any:
    """base 优先，extra 里 base 没有的键保留。"""
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(extra)          # 先放副本独有内容
        for k, v in base.items():  # shared 覆盖同名字段
            out[k] = merge(v, extra.get(k)) if k in extra else v
        return out
    return base


def sync() -> None:
    shared = load(SHARED)
    for name, path in COPIES.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = load(path) if path.exists() else {}
        merged = merge(shared, extra)
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  已同步 -> {path.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="换算档案同步与一致性自检")
    ap.add_argument("--sync", action="store_true", help="把 shared 同步到所有副本")
    args = ap.parse_args()

    if args.sync:
        print("同步 shared/profiles.json 到各适配器副本（保留副本独有字段）：")
        sync()
        print()

    problems = check()
    if problems:
        print(f"发现 {len(problems)} 处不一致：")
        for p in problems:
            print("  ✗", p)
        print("\n修法：改 shared/profiles.json，然后 python tests/profiles_sync.py --sync")
        return 1

    print(f"✓ 换算档案一致（{1 + len(COPIES)} 份，{len(CHECK_PATHS)} 个关键字段全部对齐）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
