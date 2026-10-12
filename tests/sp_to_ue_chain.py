# -*- coding: utf-8 -*-
"""SP -> UE 贴图回传全链路运行器（一条命令跑完）。

链路：
    1) 向 SP 发「回传贴图」请求（kind=textures_request，options.bake=true）
    2) SP 轮询到 → 烘焙 → 按预设导出 → 新建一条 kind=textures 的 transfer
       并 publish（目标由 options.target 指定，默认 unreal）
    3) 本脚本启动 UE commandlet（ue_import_pending_textures.py）把它收下来
    4) 回读 UE 里的贴图资产，检查尺寸 / 压缩设置 / sRGB

为什么需要它：SP 是 GUI 程序，没有命令行接口，只能靠它自己的插件轮询来触发；
把「发请求 → 等结果 → 起 UE → 校验」串成一条命令，避免人工盯屏。

前置条件：
  * 中转服务在跑（127.0.0.1:27631）
  * SP 开着，并且**加载的是最新版插件**（改过 main.qml 后必须重启 SP，
    SP 不做热重载——本次调试实测：改了文件但行为还是旧的）
  * SP 里打开着一个项目（可以先跑 tests/sp_send_model.py 让 SP 自动开）

用法：
    python tests/sp_to_ue_chain.py --asset SM_SpCube
    python tests/sp_to_ue_chain.py --asset SM_SpCube --no-bake     # 跳过烘焙
    python tests/sp_to_ue_chain.py --skip-ue                       # 只验到 SP 导出为止
"""
from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

UE_CMD = r"G:\Engine\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
UE_PROJECT = os.path.join(ROOT, "unreal", "project", "AssetBridgeTest",
                          "AssetBridgeTest.uproject")
UE_SCRIPT = os.path.join(ROOT, "unreal", "scripts", "ue_import_pending_textures.py")
UE_RESULT = os.path.join(LOGS, "ue_pending_textures_result.json")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(server, method, path, body=None, timeout=20):
    req = urllib.request.Request(url=server + "/api/v1" + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return {"_error": "HTTP {0}: {1}".format(
            exc.code, exc.read().decode("utf-8", "ignore")[:200])}


def step(n, msg):
    print("\n== {0}. {1} ==".format(n, msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:27631")
    ap.add_argument("--asset", default="SM_SpCube")
    ap.add_argument("--target-project", default="AssetBridgeTest")
    ap.add_argument("--content-path", default="/Game/Bridge/Textures")
    ap.add_argument("--no-bake", action="store_true", help="请求 SP 跳过烘焙")
    ap.add_argument("--wait", type=float, default=600.0, help="等 SP 的秒数（烘焙可能较久）")
    ap.add_argument("--skip-ue", action="store_true")
    args = ap.parse_args()
    server = args.server.rstrip("/")

    step(1, "向 SP 发回传请求（asset={0}，bake={1}）".format(args.asset, not args.no_bake))
    req = api(server, "POST", "/transfers", {
        "asset_name": args.asset,
        "kind": "textures_request",
        "format": "png",
        "from": {"app": "unreal", "project": args.target_project},
        "to": {"app": "substance_painter"},
        "options": {
            "mode": "update",
            "bake": not args.no_bake,
            "target": {"app": "unreal", "project": args.target_project,
                       "content_path": args.content_path},
        },
        "note": "请 SP 回传贴图（含烘焙）",
    })
    if "_error" in req:
        print("建请求失败: " + req["_error"])
        return 2
    rid = req["transfer"]["id"]
    api(server, "POST", "/transfers/{0}/publish".format(rid), {})
    print("  请求 id = {0}".format(rid))

    step(2, "等 SP 烘焙 + 导出（最多 {0:.0f}s）".format(args.wait))
    deadline = time.time() + args.wait
    noted = False
    while time.time() < deadline:
        rec = api(server, "GET", "/transfers/{0}".format(rid))
        st = rec.get("status")
        if st == "claimed" and not noted:
            print("  SP 已认领，正在烘焙/导出...")
            noted = True
        if st in ("done", "error"):
            break
        time.sleep(2.0)

    rec = api(server, "GET", "/transfers/{0}".format(rid))
    result = rec.get("result") or {}
    print("  请求状态 : {0}".format(rec.get("status")))
    print("  回执     : {0}".format(json.dumps(result, ensure_ascii=False)[:500]))
    out_id = result.get("transfer_id")
    if rec.get("status") != "done" or not out_id:
        print("\nFAIL：SP 没能回传贴图。请检查 SP 输出窗口里的 [bridge] 日志：")
        print("  - 若仍是旧行为（报「没有附件」）→ 插件没重载，重启 SP")
        print("  - 若报导出为空 → 项目里没有烘焙/绘制的贴图，或预设选择有问题")
        print("  - 若报 alg.baking 相关错误 → 烘焙参数需要调整")
        return 1

    step(3, "检查 SP 产出")
    t = api(server, "GET", "/transfers/{0}".format(out_id))
    files = t.get("_files_abs") or []
    print("  贴图 transfer : {0}".format(out_id))
    print("  目标          : {0}".format(json.dumps(t.get("to"), ensure_ascii=False)))
    print("  状态          : {0}".format(t.get("status")))
    print("  文件 {0} 个:".format(len(files)))
    for fp in files:
        size = os.path.getsize(fp) if os.path.exists(fp) else -1
        print("    {0:60s} {1} 字节".format(os.path.basename(fp), size))
    if not files:
        return 1

    if args.skip_ue:
        print("\nPASS：SP 已回传 {0} 张贴图（按要求跳过 UE 校验）".format(len(files)))
        return 0

    step(4, "启动 UE commandlet 收入贴图（约 20-60s）")
    if os.path.exists(UE_RESULT):
        os.remove(UE_RESULT)
    cmd = [UE_CMD, UE_PROJECT, "-run=pythonscript",
           "-script=" + UE_SCRIPT, "-unattended", "-nosplash",
           "-NullRHI", "-stdout"]
    log_path = os.path.join(LOGS, "sp_to_ue_chain_ue.log")
    with open(log_path, "wb") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=900)
    print("  UE 退出码 = {0}（原始日志 {1}）".format(proc.returncode, log_path))

    if not os.path.exists(UE_RESULT):
        print("  拿不到 UE 结果文件，看日志尾部：")
        with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
            print("".join(fh.readlines()[-25:]))
        return 1

    with open(UE_RESULT, "r", encoding="utf-8") as fh:
        ue = json.load(fh)

    step(5, "UE 侧校验")
    print("  导入记录: {0}".format(json.dumps(ue.get("imported"), ensure_ascii=False)))
    for info in ue.get("textures") or []:
        if not info:
            continue
        print("  {0}".format(json.dumps(info, ensure_ascii=False)))
    for err in ue.get("errors") or []:
        print("  !! {0}".format(json.dumps(err, ensure_ascii=False)[:300]))

    print("\n{0}".format("PASS：SP -> UE 贴图全链路打通" if ue.get("ok")
                         else "FAIL：UE 侧没能收下贴图，看上面错误"))
    return 0 if ue.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
