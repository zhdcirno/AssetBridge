# -*- coding: utf-8 -*-
"""向 Substance Painter 发「回传贴图」请求，并跟踪结果。

这条链路没有外部命令行可驱动 SP，所以用「请求 transfer」的方式远程触发
SP 插件里的 handleTexturesRequest()：
    请求方 -> 建一条 kind=textures_request 的记录（to.app=substance_painter）
           -> SP 轮询到后：导出贴图到一条新建的 kind=textures 记录
           -> SP 把新记录 publish 出去（目标由 options.target 指定）
           -> 本脚本盯住两条记录的状态，把结果打出来

用法：
    python tests/sp_texture_request.py [--asset SM_SpCube] [--target-unreal]
"""
from __future__ import print_function

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://127.0.0.1:27631"

# 本机 127.0.0.1 会被 HTTP_PROXY 劫持（报 502），必须绕开代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(server, method, path, body=None, data=None, timeout=15):
    req = urllib.request.Request(url=server + "/api/v1" + path, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
        req.data = data
    elif body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return {"_error": "HTTP {0}: {1}".format(
            exc.code, exc.read().decode("utf-8", "ignore")[:200])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--asset", default="SM_SpCube")
    ap.add_argument("--to-app", default="substance_painter")
    ap.add_argument("--target-app", default="unreal")
    ap.add_argument("--target-project", default="AssetBridgeTest")
    ap.add_argument("--content-path", default="/Game/Bridge/Textures")
    ap.add_argument("--wait", type=float, default=90.0)
    args = ap.parse_args()

    server = args.server.rstrip("/")
    print("== 1. 建回传请求 ==")
    req = api(server, "POST", "/transfers", {
        "asset_name": args.asset,
        "kind": "textures_request",
        "format": "png",
        "from": {"app": "unreal", "project": args.target_project},
        "to": {"app": args.to_app},
        "options": {
            "mode": "update",
            "target": {
                "app": args.target_app,
                "project": args.target_project,
                "content_path": args.content_path,
            },
        },
        "note": "请 SP 回传贴图",
    })
    if "_error" in req:
        print("建请求失败: {0}".format(req["_error"]))
        return 2
    rid = req["transfer"]["id"]
    print("  请求 id = {0}".format(rid))

    pub = api(server, "POST", "/transfers/{0}/publish".format(rid), {})
    print("  发布状态 = {0}".format(pub.get("transfer", {}).get("status")))

    print("\n== 2. 等 SP 处理（最多 {0:.0f}s）==".format(args.wait))
    out_id = None
    status = ""
    deadline = time.time() + args.wait
    while time.time() < deadline:
        rec = api(server, "GET", "/transfers/{0}".format(rid))
        status = rec.get("status", "")
        if status in ("done", "error"):
            break
        time.sleep(1.0)

    rec = api(server, "GET", "/transfers/{0}".format(rid))
    result = rec.get("result") or {}
    print("  请求状态 : {0}".format(rec.get("status")))
    print("  认领者   : {0}".format(rec.get("claimed_by")))
    print("  回执     : {0}".format(json.dumps(result, ensure_ascii=False)))
    out_id = result.get("transfer_id")

    if out_id:
        print("\n== 3. 检查回传出来的贴图 transfer ==")
        for _ in range(10):
            t = api(server, "GET", "/transfers/{0}".format(out_id))
            if t.get("status") == "queued":
                break
            time.sleep(0.5)
        t = api(server, "GET", "/transfers/{0}".format(out_id))
        print("  id      : {0}".format(t.get("id")))
        print("  kind    : {0}".format(t.get("kind")))
        print("  目标     : {0}".format(json.dumps(t.get("to"), ensure_ascii=False)))
        print("  状态     : {0}".format(t.get("status")))
        print("  文件     : {0}".format(json.dumps(t.get("files"), ensure_ascii=False)))
        for fp in t.get("_files_abs") or []:
            import os
            size = os.path.getsize(fp) if os.path.exists(fp) else -1
            print("    {0}  ({1} 字节)".format(fp, size))

    ok = rec.get("status") == "done" and bool(out_id)
    print("\n{0}".format("PASS：SP 已远程回传贴图" if ok else "FAIL：SP 没能回传贴图"))
    print("（SP 的输出窗口里应能看到 [bridge] 已回传 N 张贴图）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
