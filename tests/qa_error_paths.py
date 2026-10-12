# -*- coding: utf-8 -*-
"""Asset Bridge :: QA 用例6 —— 异常路径回归（纯 HTTP，不打任何 DCC）。

用系统 Python 跑：
    python tests/qa_error_paths.py

覆盖：
    E1. claim 不存在的 transfer id            -> 期望 404
    E2. 重复 claim（已 claimed 的记录）        -> 期望 409
    E3. 非法资产名                             -> 期望 400
    E4. 缺少 to.app                            -> 期望 400
    E5. publish 不存在的 transfer              -> 期望 404
    E6. finish 不存在的 transfer                -> 期望 404
    E7. finish 传非法 status                    -> 期望 400
    E8. inbox 不传 app                          -> 期望 400
    E9. draft 状态直接 finish（状态机越权）      -> 记录实际行为
    E10. 路径穿越文件名（../ 上传）             -> 期望被安全化，不落到 payload 之外
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:27631/api/v1"
OUT_JSON = Path(r"E:\CirnoWorkSpace\AssetBridge\logs\qa_error_paths.json")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS = []
_log = []


def log(msg):
    print(msg, flush=True)
    _log.append(msg)


def req(method, path, body=None, data=None):
    """返回 (http_code, obj_or_None, 异常字符串或 None)。"""
    r = urllib.request.Request(url=BASE + path, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/octet-stream")
        r.data = data
    elif body is not None:
        r.add_header("Content-Type", "application/json; charset=utf-8")
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with _OPENER.open(r, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {}), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        try:
            obj = json.loads(detail) if detail else {}
        except Exception:
            obj = {"raw": detail}
        return exc.code, obj, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def case(cid, title, expected, fn):
    try:
        code, obj, exc = fn()
    except Exception as e:
        code, obj, exc = None, None, f"{type(e).__name__}: {e}"
    ok = (code == expected)
    actual = {"http_code": code, "body": obj, "exception": exc}
    RESULTS.append({
        "id": cid, "title": title, "expected_http": expected,
        "actual_http": code, "pass": ok, "detail": actual,
    })
    log(f"  [{'OK  ' if ok else 'FAIL'}] {cid} {title}: 期望 HTTP {expected}，实际 {code}"
        + (f"  异常={exc}" if exc else ""))
    return ok


def main():
    log("\n=== QA 用例6：异常路径 ===\n")

    # 先建一条干净的 transfer 用于「重复 claim」
    tid = {"v": None}

    def _mk():
        code, obj, exc = req("POST", "/transfers", {
            "asset_name": "SM_QaErrProbe",
            "kind": "static_mesh",
            "to": {"app": "unreal", "project": "AssetBridgeTest"},
            "options": {"mode": "update"},
        })
        tid["v"] = (obj or {}).get("transfer", {}).get("id")
        return code, obj, exc

    case("E0", "创建用于重复 claim 的 transfer", 200, _mk)
    real_tid = tid["v"]
    log(f"       transfer_id = {real_tid}")

    # 上传到 payload 目录（PUT）
    def _upload():
        return req("PUT", f"/transfers/{real_tid}/files/SM_QaErrProbe.fbx",
                   data=b"QA_ERR_PROBE" * 10)
    case("E0b", "上传负载文件", 200, _upload)

    def _publish():
        return req("POST", f"/transfers/{real_tid}/publish", {})
    case("E0c", "发布（draft -> queued）", 200, _publish)

    # E1：不存在的 transfer id 去 claim
    case("E1", "claim 不存在的 transfer id -> 404", 404,
         lambda: req("POST", "/transfers/qa_no_such_id_123/claim", {"session_id": "qa"}))

    # E2：先 claim 一次（应 200），再 claim 一次（应 409）
    def _first_claim():
        return req("POST", f"/transfers/{real_tid}/claim", {"session_id": "qa"})
    case("E2a", "首次 claim -> 200", 200, _first_claim)

    case("E2", "重复 claim -> 409", 409,
         lambda: req("POST", f"/transfers/{real_tid}/claim", {"session_id": "qa"}))

    # E3：非法资产名
    case("E3", "非法资产名 'SM Bad/Name..' -> 400", 400,
         lambda: req("POST", "/transfers", {
             "asset_name": "SM Bad/Name..", "to": {"app": "unreal"}}))

    # 另外几种非法名
    case("E3b", "空资产名 -> 400", 400,
         lambda: req("POST", "/transfers", {
             "asset_name": "", "to": {"app": "unreal"}}))
    case("E3c", "中文资产名 -> 400", 400,
         lambda: req("POST", "/transfers", {
             "asset_name": "测试资产", "to": {"app": "unreal"}}))

    # E4：缺少 to.app
    case("E4", "缺少 to.app -> 400", 400,
         lambda: req("POST", "/transfers", {"asset_name": "SM_NoTarget"}))

    # E5 / E6：不存在的 id
    case("E5", "publish 不存在的 transfer -> 404", 404,
         lambda: req("POST", "/transfers/qa_no_such_id_123/publish", {}))
    case("E6", "finish 不存在的 transfer -> 404", 404,
         lambda: req("POST", "/transfers/qa_no_such_id_123/finish",
                     {"status": "done"}))

    # E7：非法 status
    case("E7", "finish 传非法 status -> 400", 400,
         lambda: req("POST", f"/transfers/{real_tid}/finish", {"status": "awesome"}))

    # E8：inbox 不传 app
    case("E8", "inbox 不传 app -> 400", 400, lambda: req("GET", "/inbox"))

    # E9：draft 状态直接 finish（不 claim）—— 记录实际行为
    draft_tid = {"v": None}

    def _mk_draft():
        code, obj, exc = req("POST", "/transfers", {
            "asset_name": "SM_QaDraftProbe",
            "to": {"app": "unreal", "project": "AssetBridgeTest"},
        })
        draft_tid["v"] = (obj or {}).get("transfer", {}).get("id")
        return code, obj, exc

    case("E9a", "创建 draft 态 transfer", 200, _mk_draft)

    def _draft_finish():
        return req("POST", f"/transfers/{draft_tid['v']}/finish", {"status": "done"})
    case("E9", "draft 态直接 finish（状态机越权）-> 记录行为", None, _draft_finish)
    # E9 的期望值不固定（服务允许），单独标注
    for r in RESULTS:
        if r["id"] == "E9":
            r["expected_http"] = "观察项（当前实现允许）"
            r["pass"] = True
            r["note"] = ("服务允许 draft -> done 的越权 finish；不阻塞主链路，"
                         "但意味着「未经认领的资产也会被标记完成」，建议加状态校验")

    # E10：路径穿越
    def _traversal():
        return req("PUT", f"/transfers/{real_tid}/files/..%2F..%2Fqa_escape.txt",
                   data=b"SHOULD_NOT_ESCAPE")
    case("E10", "上传路径穿越文件名 -> 200 且被安全化", 200, _traversal)
    payload_root = Path(r"E:\CirnoWorkSpace\AssetBridge\runtime")
    escaped = list(payload_root.parent.glob("qa_escape.txt"))
    for r in RESULTS:
        if r["id"] == "E10":
            r["escaped_outside_payload"] = bool(escaped)
            r["pass"] = (r["actual_http"] == 200) and not escaped
            log(f"       路径穿越是否逃出 payload 根: {bool(escaped)} "
                f"（期望 False）")

    # 收尾：把探针记录 finish 掉，别留在收件箱里污染后续用例
    try:
        req("POST", f"/transfers/{real_tid}/finish", {"status": "done",
                                                      "result": {"qa": True}})
    except Exception:
        pass

    passed = sum(1 for r in RESULTS if r.get("pass"))
    total = len(RESULTS)
    log(f"\n=== 用例6 结果：{passed}/{total} 通过 ===\n")

    OUT_JSON.write_text(json.dumps(
        {"passed": passed, "total": total, "results": RESULTS},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"结果已写入 {OUT_JSON}")
    try:
        (OUT_JSON.parent / "qa_error_paths.log").write_text(
            "\n".join(_log), encoding="utf-8")
    except Exception:
        pass
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
