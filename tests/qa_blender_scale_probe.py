# -*- coding: utf-8 -*-
"""QA :: Blender 5.0.1 导入端单位探针。

背景：三份 profiles.json 对 unreal>blender 的 import.global_scale 说法不一致
    shared/profiles.json                        -> 0.01
    unreal/plugin/.../profiles.json             -> 0.01
    blender/addon/asset_bridge_blender/...      -> 1.0
到底哪个对，不能用猜的，拿同一个 FBX 用不同 global_scale 各导一次量出来。

用法：
    QA_FBX="<某个 fbx>" "D:\\DCC\\Blender5.0.1\\blender.exe" --background ^
        --python tests\\qa_blender_scale_probe.py
"""
import json
import mathutils
import os
import sys
from pathlib import Path

import bpy

LOG_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\logs")
FBX = os.environ.get("QA_FBX", "").strip()

RESULT = {
    "fbx": FBX,
    "blender_version": bpy.app.version_string,
    "scene": {},
    "rows": [],
    "errors": [],
}

SCALES = [1.0, 0.01, 100.0]


def dims_of(ob):
    bpy.context.view_layer.update()
    corners = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    return [round(max(c[i] for c in corners) - min(c[i] for c in corners), 6)
            for i in range(3)]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    sc = bpy.context.scene
    us = sc.unit_settings
    RESULT["scene"] = {
        "system": getattr(us, "system", "?"),
        "scale_length": round(float(getattr(us, "scale_length", 1.0)), 6),
        "length_unit": getattr(us, "length_unit", "?"),
    }

    if not FBX or not os.path.isfile(FBX):
        RESULT["errors"].append(f"QA_FBX 未指定或文件不存在: {FBX!r}")
        dump()
        return

    valid = {p.identifier for p in bpy.ops.import_scene.fbx.get_rna_type().properties}
    for gs in SCALES:
        before = set(bpy.data.objects)
        kwargs = {"filepath": FBX, "global_scale": gs}
        kwargs = {k: v for k, v in kwargs.items() if k in valid}
        try:
            bpy.ops.import_scene.fbx(**kwargs)
        except Exception as exc:
            RESULT["rows"].append({"global_scale": gs,
                                   "error": f"{type(exc).__name__}: {exc}"})
            continue
        fresh = [o for o in (set(bpy.data.objects) - before) if o.type == "MESH"]
        dims = [dims_of(o) for o in fresh]
        max_edge = max((max(d) for d in dims), default=None)
        RESULT["rows"].append({
            "global_scale": gs,
            "objects": [o.name for o in fresh],
            "dimensions_m": dims,
            "max_edge_m": max_edge,
        })
        print(f"[probe] global_scale={gs} -> 尺寸(米)={dims} 最大边={max_edge}",
              flush=True)
        # 清场，保证下一次导入是干净的
        for o in fresh:
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass


def dump():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = LOG_DIR / "qa_blender_scale_probe.json"
        out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"[probe] 结果已写入 {out}", flush=True)
    except Exception as exc:
        print(f"[probe] 写结果失败: {exc}", flush=True)


try:
    main()
except Exception as exc:
    RESULT["errors"].append(f"{type(exc).__name__}: {exc}")
    import traceback
    traceback.print_exc()
finally:
    dump()
