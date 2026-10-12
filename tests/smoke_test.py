#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：不打任何一个 DCC，纯 HTTP 把整条链路跑一遍。

    python bridge_server/server.py --root ./.bridge_test &
    python tools/smoke_test.py

覆盖：health -> register -> create -> upload -> publish -> inbox
      -> claim -> finish -> 重复发送(revision 递增) -> 非法资产名
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:27631/api/v1"
PASSED, FAILED = [], []

# 本机回环必须绕过代理：公司网络里 HTTP_PROXY 会把 127.0.0.1 也劫走，
# 表现是 502 upstream connect failed，非常难查。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, data=None, raw=False):
    r = urllib.request.Request(url=BASE + path, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/octet-stream")
        r.data = data
    elif body is not None:
        r.add_header("Content-Type", "application/json; charset=utf-8")
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with _OPENER.open(r, timeout=10) as resp:
            out = resp.read()
            return out if raw else (json.loads(out.decode("utf-8")) if out else {})
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} {path} -> HTTP {exc.code}: "
                             f"{exc.read().decode('utf-8', 'ignore')}")


def check(name, fn):
    try:
        info = fn()
        PASSED.append(name)
        print(f"  [OK]   {name}" + (f"  -- {info}" if info else ""))
        return True
    except Exception as exc:
        FAILED.append((name, exc))
        print(f"  [FAIL] {name}  -- {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print(f"\n=== asset-bridge 冒烟测试 ({BASE}) ===\n")

    # 1 健康
    check("服务健康检查", lambda: req("GET", "/health")["ok"])

    # 2 会话
    blender_sess, unreal_sess = {}, {}

    def _reg_b():
        global blender_sess
        blender_sess = req("POST", "/sessions", {
            "app": "blender", "version": "4.2.0", "project": "Demo"})
        return blender_sess["session"]["session_id"]

    def _reg_u():
        global unreal_sess
        unreal_sess = req("POST", "/sessions", {
            "app": "unreal", "version": "5.4.0", "project": "Demo"})
        return unreal_sess["session"]["session_id"]

    check("注册 Blender 会话", _reg_b)
    check("注册 Unreal 会话", _reg_u)

    def _sessions():
        r = req("GET", "/sessions")
        return f"{len(r['sessions'])} 个在线端"
    check("列出在线端", _sessions)

    # 3 建 transfer + 上传 + 发布
    tid = {}

    def _create():
        r = req("POST", "/transfers", {
            "asset_name": "SM_TestCube",
            "kind": "static_mesh",
            "format": "fbx",
            "to": {"app": "unreal", "project": "Demo",
                   "content_path": "/Game/Bridge"},
            "unit": {"scene_unit": "m", "up_axis": "Z", "unit_scale": 1.0},
            "options": {"mode": "update", "import_materials": True},
            "objects": [{"name": "Cube", "type": "MESH", "materials": ["M_Test"]}],
        })
        tid["id"] = r["transfer"]["id"]
        # 记下首条的 revision，供后面「重复发送 -> revision 递增」当基线。
        # 不能把递增目标写死成 2：服务是长驻的，重跑一次这个脚本 revision 就是 3、4...，
        # 写死会让一条本来正确的用例假失败（QA 回归时踩到过）。
        tid["rev1"] = r["transfer"]["revision"]
        return f"revision={r['transfer']['revision']} status={r['transfer']['status']}"
    check("创建 transfer（草稿态）", _create)

    def _upload():
        payload = b"FAKE_FBX_BINARY_CONTENT" * 100
        r = req("PUT", f"/transfers/{tid['id']}/files/SM_TestCube.fbx", data=payload)
        return f"{r['bytes']} 字节"
    check("上传 FBX 负载", _upload)

    def _publish():
        r = req("POST", f"/transfers/{tid['id']}/publish", {})
        assert r["transfer"]["status"] == "queued", r["transfer"]["status"]
        return f"files={r['transfer']['files']}"
    check("发布到收件箱", _publish)

    # 4 接收侧
    def _inbox():
        r = req("GET", "/inbox?app=unreal&project=Demo")
        assert r["count"] >= 1, "收件箱为空"
        return f"{r['count']} 条待处理"
    check("UE 拉取收件箱", _inbox)

    def _inbox_isolated():
        r = req("GET", "/inbox?app=maya")
        assert r["count"] == 0, "Maya 不该看到发给 UE 的资产"
        return "Maya 收件箱为空（隔离正确）"
    check("按软件隔离收件箱", _inbox_isolated)

    def _claim():
        r = req("POST", f"/transfers/{tid['id']}/claim", {"session_id": "x"})
        files = r["transfer"].get("_files_abs") or []
        assert files, "claim 没返回绝对路径"
        assert Path(files[0]).exists(), f"负载文件不存在: {files[0]}"
        return f"拿到 {len(files)} 个文件"
    check("UE 认领 transfer（含绝对路径）", _claim)

    def _double_claim():
        try:
            req("POST", f"/transfers/{tid['id']}/claim", {"session_id": "y"})
        except AssertionError as exc:
            assert "409" in str(exc), f"应当返回 409，实际: {exc}"
            return "重复认领被拒绝（409）"
        raise AssertionError("重复认领居然成功了")
    check("重复认领防重", _double_claim)

    def _finish():
        r = req("POST", f"/transfers/{tid['id']}/finish", {
            "status": "done",
            "result": {"assets": ["/Game/Bridge/SM_TestCube.SM_TestCube"],
                       "message": "导入 1 个资产"}})
        return r["transfer"]["status"]
    check("回执完成", _finish)

    # 5 增量更新：同名再发一次，revision 应该 +1
    def _revision():
        r = req("POST", "/transfers", {
            "asset_name": "SM_TestCube",
            "to": {"app": "unreal", "project": "Demo"},
            "options": {"mode": "update"}})
        rev = r["transfer"]["revision"]
        want = tid["rev1"] + 1
        assert rev == want, f"revision 应为 {want}（上一条 {tid['rev1']} +1），实际 {rev}"
        return f"revision={rev}（增量更新计数正确，上一条 {tid['rev1']}）"
    check("同名资产重复发送 -> revision 递增", _revision)

    # 6 非法输入
    def _bad_name():
        try:
            req("POST", "/transfers", {
                "asset_name": "SM Bad/Name..", "to": {"app": "unreal"}})
        except AssertionError as exc:
            assert "400" in str(exc), f"应当 400，实际: {exc}"
            return "非法资产名被拒绝（400）"
        raise AssertionError("非法资产名居然通过了")
    check("非法资产名拦截", _bad_name)

    def _missing_target():
        try:
            req("POST", "/transfers", {"asset_name": "SM_X"})
        except AssertionError as exc:
            assert "400" in str(exc), f"应当 400，实际: {exc}"
            return "缺少 to.app 被拒绝（400）"
        raise AssertionError("缺少目标软件居然通过了")
    check("缺少目标软件拦截", _missing_target)

    # 7 历史
    def _history():
        r = req("GET", "/transfers?limit=10")
        return f"{len(r['transfers'])} 条记录"
    check("查询历史记录", _history)

    print(f"\n=== 结果：{len(PASSED)} 通过 / {len(FAILED)} 失败 ===\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
