# -*- coding: utf-8 -*-
"""Asset Bridge :: 贴图回传链路 —— SP 发送端（协议层模拟）。

Substance Painter 插件是 QML/JS，无法从命令行驱动，所以这里在**协议层**
复刻它实际发出的请求序列（服务端 API 完全一致）：

    1. POST /api/v1/sessions                                       注册 unreal 会话
    2. POST /api/v1/transfers                                      建 kind=textures 记录
    3. PUT  /api/v1/transfers/<id>/files/<name>  (raw body)        上传 3 张 PNG
    4. POST /api/v1/transfers/<id>/publish                         发布（服务端扫描负载目录）

body 与 SP 插件实际发的那一份保持一致（asset_name / kind=textures / format=png /
to.content_path=/Game/Bridge/Textures / options.mode=update）。

脚本不 import 任何适配器代码，纯标准库 urllib，确保“协议层”这一前提成立。
每次运行都会新建一条 transfer，因此**重复运行**即可构造“增量更新”场景
（同名资产、同名 content_path，revision 递增）。

用法：
    python qa_textures_ue.py                # 默认发 SM_CalibCube 的 3 张图
    python qa_textures_ue.py --note "第二次"
输出：
    stdout 打印 JSON 摘要；transfer_id 同时写入 logs/texture_transfer_id.txt
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SERVER = "http://127.0.0.1:27631"
API = "/api/v1"
PROJECT = "AssetBridgeTest"
ASSET_NAME = "SM_CalibCube"
CONTENT_PATH = "/Game/Bridge/Textures"
FIXTURE_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\tests\fixtures")
LOG_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\logs")
TID_FILE = LOG_DIR / "texture_transfer_id.txt"

TEXTURES = [
    "SM_CalibCube_BaseColor.png",
    "SM_CalibCube_Normal.png",
    "SM_CalibCube_ORM.png",
]

# 公司网络会把 127.0.0.1 也劫持进代理，必须清空代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(method: str, path: str, body=None, data=None,
            content_type="application/json", timeout=30.0) -> dict:
    url = f"{SERVER}{API}{path}"
    req = urllib.request.Request(url=url, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
        req.data = data
    elif body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败 {url}: {exc.reason}") from exc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="协议层模拟 SP 发送贴图到 UE")
    ap.add_argument("--note", default="from Substance Painter",
                    help="transfer 备注（会写入记录，便于区分多次运行）")
    ap.add_argument("--asset", default=ASSET_NAME, help="asset_name")
    args = ap.parse_args(argv)

    summary = {"ok": False, "steps": []}

    def step(msg):
        summary["steps"].append(msg)
        print(f"[sp-sim] {msg}", flush=True)

    # 0. 健康检查
    health = request("GET", "/health")
    step(f"health = {health}")

    # 1. 注册 unreal 会话
    sess = request("POST", "/sessions", {
        "app": "unreal", "version": "5.8.0", "project": PROJECT,
        "host": "SP-SIM", "pid": 0,
        "capabilities": {"formats": ["png"]},
    })["session"]
    summary["session_id"] = sess["session_id"]
    step(f"会话 {sess['session_id']} 已注册")

    # 2. 建 transfer（draft）
    meta = {
        "asset_name": args.asset,
        "kind": "textures",
        "format": "png",
        "to": {"app": "unreal", "project": PROJECT, "content_path": CONTENT_PATH},
        "options": {"mode": "update"},
        "note": args.note,
    }
    rec = request("POST", "/transfers", meta)["transfer"]
    tid = rec["id"]
    summary["transfer_id"] = tid
    summary["revision"] = rec.get("revision")
    step(f"transfer {tid} 已创建 status={rec['status']} rev={rec.get('revision')}")

    # 3. 逐张 PUT 上传（raw body）
    uploaded = []
    for name in TEXTURES:
        fp = FIXTURE_DIR / name
        if not fp.is_file():
            raise RuntimeError(f"缺少样本 {fp}，请先跑 make_texture_fixtures.py")
        request("PUT", f"/transfers/{tid}/files/{name}",
                data=fp.read_bytes(), content_type="application/octet-stream")
        uploaded.append(name)
        step(f"上传 {name} ({fp.stat().st_size} bytes)")
    summary["uploaded"] = uploaded

    # 4. publish（服务端扫描负载目录列出文件）
    rec = request("POST", f"/transfers/{tid}/publish", {})
    rec = rec["transfer"]
    summary["status"] = rec["status"]
    summary["files"] = rec.get("files")
    step(f"publish -> status={rec['status']} files={rec.get('files')}")

    # 5. 落 transfer_id
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TID_FILE.write_text(tid, encoding="utf-8")
    summary["tid_file"] = str(TID_FILE)
    step(f"transfer_id 已写入 {TID_FILE}")

    summary["ok"] = True
    print("[sp-sim] RESULT " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[sp-sim] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
