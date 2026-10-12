# -*- coding: utf-8 -*-
"""Asset Bridge :: UE 侧「收下所有待处理贴图」自检（commandlet）。

和 ue_texture_selftest.py 的区别：那个是**校准用**（盯死资产名 64x64 的
合成图），这个是**通用收件**——把收件箱里所有 kind="textures" 的记录都用
适配器自身的真实链路（`_handle` → `importer.import_textures`）收下来，
然后报告生成了哪些资产、尺寸多少、压缩设置对不对。

这正是 UE 编辑器开着时后台轮询会做的事，只是搬到 commandlet 里跑。

用法：
    UnrealEditor-Cmd.exe "<project>.uproject" -run=pythonscript
        -script="E:\\CirnoWorkSpace\\AssetBridge\\unreal\\scripts\\ue_import_pending_textures.py"
        -unattended -nosplash -NullRHI -stdout -FullStdOutLogOutput
"""
import json
import os
import sys
import traceback

import unreal

PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

R = {"imported": [], "skipped": [], "errors": []}


def log(msg):
    print("[ue_tex] " + str(msg), flush=True)


def measure_tex(path):
    """取贴图资产的关键属性（拿不到就返回 None）。"""
    try:
        a = unreal.EditorAssetLibrary.load_asset(path)
        if not a:
            return None
        info = {"path": path, "class": type(a).__name__}
        for attr in ("blueprint_get_size_x", "blueprint_get_size_y"):
            fn = getattr(a, attr, None)
            if callable(fn):
                info[attr.replace("blueprint_get_", "")] = fn()
        cs = getattr(a, "compression_settings", None)
        if cs is not None:
            info["compression_settings"] = str(cs)
        srgb = getattr(a, "srgb", None)
        if srgb is not None:
            info["srgb"] = bool(srgb)
        return info
    except Exception as exc:
        return {"path": path, "error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    import asset_bridge_unreal as abu

    log("UE 版本 " + unreal.SystemLibrary.get_engine_version())
    R["engine"] = unreal.SystemLibrary.get_engine_version()

    abu.CONFIG["server_url"] = SERVER
    abu.CONFIG["project"] = PROJECT

    c = abu.BridgeClient(SERVER, app="unreal", project=PROJECT)
    try:
        c.register()
    except Exception as exc:
        log("注册会话失败（继续用现有会话）：%s" % exc)

    inbox = c.inbox(limit=20)
    log("收件箱 %d 条: %s" % (len(inbox),
                              [(r.get("asset_name"), r.get("kind"), r.get("status"))
                               for r in inbox]))

    pending = [r for r in inbox if r.get("kind") == "textures"
               and r.get("status") == "queued"]
    if not pending:
        log("没有待处理的贴图记录")
        R["skipped"].append({"reason": "收件箱里没有 kind=textures 且 queued 的记录"})
    for rec in pending:
        tid = rec["id"]
        log("--- 处理 %s (%s) ---" % (rec.get("asset_name"), tid))
        try:
            # claim() 直接返回记录本身（内部已经取出 "transfer" 那一层）
            full = c.claim(tid)
            files = full.get("_files_abs") or []
            log("  附件 %d 个: %s" % (len(files), [os.path.basename(f) for f in files]))
            # 真实链路：适配器的 _handle（内部会 import_textures 并把结果 finish 回去）
            abu._handle(full)
            log("  处理完成，附件: %s" % ([os.path.basename(f) for f in files],))
            R["imported"].append({"transfer_id": tid,
                                  "asset_name": rec.get("asset_name"),
                                  "files": [os.path.basename(f) for f in files]})
        except Exception as exc:
            log("  失败: %s" % traceback.format_exc())
            R["errors"].append({"transfer_id": tid,
                                "error": "%s: %s" % (type(exc).__name__, exc)})

    # 把 /Game/Bridge/Textures 下的贴图全列出来量一遍
    tex_dir = "/Game/Bridge/Textures"
    try:
        assets = unreal.EditorAssetLibrary.list_assets(tex_dir, recursive=False, include_folder=False)
        log("目录 %s 下 %d 个资产" % (tex_dir, len(assets)))
        R["textures"] = [measure_tex(p) for p in assets]
        for info in R["textures"]:
            log("  %s" % info)
    except Exception as exc:
        R["errors"].append({"list_assets": traceback.format_exc()})

    R["ok"] = not R["errors"] and bool(R.get("imported"))
    out = os.path.join(LOG_DIR, "ue_pending_textures_result.json")
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(R, fh, ensure_ascii=False, indent=2)
    log("结果写入 " + out)
    log("OK=%s" % R["ok"])


try:
    main()
except Exception:
    print("[ue_tex] 顶层异常:\n" + traceback.format_exc(), flush=True)
    R["errors"].append({"top": traceback.format_exc()})
