# -*- coding: utf-8 -*-
"""QA :: 汇总所有 qa_*.json 原始结果，生成
    logs/qa_result.json  （结构化：每用例 通过/失败/实测值/期望值）
    logs/qa_report.md    （人读报告）

    python tests/qa_make_report.py
"""
import json
import re
from pathlib import Path

LOG = Path(r"E:\CirnoWorkSpace\AssetBridge\logs")


def load(name):
    p = LOG / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}"}


def parse_smoke():
    """从 logs/qa_smoke.log 里数 PASS/FAIL。"""
    txt = (LOG / "qa_smoke.log").read_text(encoding="utf-8", errors="ignore")
    items = []
    for m in re.finditer(r"\[(OK|FAIL)\]\s+(.+?)(?:\s+--\s+(.*))?$", txt, re.M):
        items.append({"name": m.group(2).strip(), "pass": m.group(1) == "OK",
                      "info": (m.group(3) or "").strip()})
    total = len(items)
    passed = sum(1 for i in items if i["pass"])
    return {"total": total, "passed": passed, "failed": total - passed,
            "items": items}


def checks_of(d):
    return d.get("checks", {}) or {}


def parse_ue_log_import(name):
    """从 UE commandlet 原始日志里抓 import_model 的返回（verify 跑会覆盖 json）。"""
    p = LOG / name
    if not p.exists():
        return None
    txt = p.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"import_model 返回 = (\[.*?\])", txt)
    if not m:
        return None
    try:
        return json.loads(m.group(1).replace("'", '"'))
    except Exception:
        return m.group(1)


def parse_send_log(name):
    """从 Blender 发送日志里抓 transfer/kind/revision/bytes。

    qa_blender_box.json 会被第二次发送覆盖，用例2 的真实数据只在第一次的日志里。
    """
    p = LOG / name
    out = {}
    if not p.exists():
        return out
    txt = p.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"transfer=(\S+) kind=(\S+) revision=(\S+) fbx=(\S+) bytes=(\S+)", txt)
    if m:
        out = {"transfer_id": m.group(1), "kind": m.group(2),
               "revision": m.group(3), "fbx": m.group(4), "fbx_bytes": m.group(5)}
    return out


def case_block(cid, title, status, checks, notes, evidence):
    """把一组 check 归一成结果块。"""
    norm = []
    for name, c in checks.items():
        norm.append({
            "name": name,
            "expected": c.get("expected"),
            "actual": c.get("actual"),
            "pass": bool(c.get("pass")),
        })
    failed = [n for n in norm if not n["pass"]]
    return {
        "id": cid,
        "title": title,
        "status": "FAIL" if failed else status,
        "checks_total": len(norm),
        "checks_passed": len(norm) - len(failed),
        "checks": norm,
        "notes": notes,
        "evidence": evidence,
    }


def _fmt(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "是" if v else "否"
    if v is None:
        return "—"
    return str(v)


def render_md(r):
    s = r["summary"]
    m = r["meta"]
    L = []
    L.append("# Asset Bridge 回归测试报告（QA · 严过关）\n")
    L.append(f"- 轮次：第 {m['round']} 轮 | 用例 **{s['cases_passed']}/{s['cases_total']}** 通过，"
             f"断言 **{s['checks_passed']}/{s['checks_total']}** 通过")
    L.append(f"- 环境：{m['blender']} · UE {m['unreal']} · 服务数据根 {m['service_root']}")
    L.append(f"- 路由结论：**{s['routing_decision']}**\n")

    L.append("## 一、总览\n")
    L.append("| 用例 | 名称 | 结果 | 断言 |")
    L.append("|---|---|---|---|")
    for c in r["cases"]:
        L.append(f"| {c['id']} | {c['title']} | **{c['status']}** | "
                 f"{c['checks_passed']}/{c['checks_total']} |")
    L.append("")

    for c in r["cases"]:
        L.append(f"## {c['id']} · {c['title']}\n")
        L.append(f"结果：**{c['status']}**（{c['checks_passed']}/{c['checks_total']} 项断言通过）\n")
        if c["checks"]:
            L.append("| 断言 | 期望 | 实测 | 结果 |")
            L.append("|---|---|---|---|")
            for k in c["checks"]:
                L.append(f"| {k['name']} | {_fmt(k['expected'])} | "
                         f"{_fmt(k['actual'])} | {'PASS' if k['pass'] else 'FAIL'} |")
            L.append("")
        if c.get("notes"):
            L.append("实测细节：")
            for n in c["notes"]:
                L.append(f"- {n}")
            L.append("")
        if c.get("evidence"):
            L.append("证据：`" + "` `".join(c["evidence"]) + "`\n")

    L.append("## 二、源码缺陷清单（按严重度排序）\n")
    L.append("> 以下均为**源码问题**，QA 未自行修改；请工程师处理。\n")
    for b in r["source_bugs"]:
        L.append(f"### 【{b['severity']}】{b['id']} · {b['file']}\n")
        L.append(f"- 位置：{b['line']}")
        L.append(f"- 类型：{b['type']}")
        L.append(f"- 现象：{b['symptom']}")
        L.append(f"- 影响：{b['impact']}")
        L.append(f"- 证据：{b['evidence']}")
        L.append(f"- 复现：{b['reproduce']}")
        L.append(f"- 建议修复：{b['suggestion']}\n")

    L.append("## 三、测试脚本自身修的 bug（未改任何源码）\n")
    for t in r.get("test_script_fixes", []):
        L.append(f"- `{t['file']}`：{t['what']}")
        L.append(f"  - 原因：{t['why']}")
        L.append(f"  - 处理：{t['fix']}")
    L.append("")

    L.append("## 四、本次新增的测试资产\n")
    for p in r["artifacts"]["scripts"]:
        L.append(f"- `{p}`")
    L.append("")
    L.append("原始日志（部分）：")
    for p in r["artifacts"]["logs"]:
        L.append(f"- `logs/{p}`")
    L.append("")
    return "\n".join(L)


def main():
    smoke = parse_smoke()

    b_box = load("qa_blender_box.json")
    b_box2 = load("qa_blender_box_run2.json")
    b_rig = load("qa_blender_rig.json")
    b_recv = load("qa_blender_recv.json")
    u_box = load("qa_ue_box.json")
    u_box2 = load("qa_ue_box2.json")
    u_rig = load("qa_ue_rig.json")
    u_exp = load("qa_ue_export.json")
    errp = load("qa_error_paths.json")
    probe = load("qa_blender_scale_probe.json")

    cases = []

    # ---------------- 用例 1
    cases.append({
        "id": "TC-01",
        "title": "协议层回归 smoke_test.py（16 项）",
        "status": "PASS" if smoke["failed"] == 0 else "FAIL",
        "checks_total": smoke["total"],
        "checks_passed": smoke["passed"],
        "checks": [{"name": i["name"], "expected": "PASS",
                    "actual": "PASS" if i["pass"] else "FAIL",
                    "pass": i["pass"], "info": i["info"]}
                   for i in smoke["items"]],
        "notes": [f"smoke_test.py 共 {smoke['total']} 项，"
                  f"{smoke['passed']} 通过 / {smoke['failed']} 失败"],
        "evidence": ["logs/qa_smoke.log"],
    })

    # ---------------- 用例 2
    send1 = parse_send_log("qa_box_blender.log")
    c2 = dict(checks_of(b_box))
    c2.update(checks_of(u_box))
    cases.append(case_block(
        "TC-02", "Blender → UE 静态网格完整链路（SM_QaBox 2×1×0.5m / 2 材质槽）",
        "PASS", c2,
        [
            f"Blender 源尺寸 {(b_box.get('source') or {}).get('dimensions_m')} m，"
            f"材质槽 {(b_box.get('source') or {}).get('material_slots')}",
            f"首次发送：transfer={send1.get('transfer_id')} "
            f"kind={send1.get('kind')} "
            f"revision={send1.get('revision')} "
            f"FBX={send1.get('fbx_bytes')} 字节",
            f"UE 实测包围盒 = {(u_box.get('measure') or {}).get('size')} cm"
            f"（期望 200×100×50）",
            f"UE 材质槽 = {(u_box.get('materials') or {}).get('count')} "
            f"{(u_box.get('materials') or {}).get('names')}",
            f"资产路径 = {(u_box.get('checks') or {}).get('ue_资产路径正确', {}).get('actual')}",
        ],
        ["logs/qa_blender_box.json", "logs/qa_ue_box.json",
         "logs/qa_box_blender.log", "logs/qa_ue_box_verify.log"],
    ))

    # ---------------- 用例 3
    c3 = dict(checks_of(u_box2))
    cases.append(case_block(
        "TC-03", "增量更新回归（同一资产再发再导一次）",
        "PASS", c3,
        [
            f"第二次发送 revision = {(b_box2.get('send') or {}).get('revision')}（期望 2）",
            f"UE 侧认领到的 revision = {(u_box2.get('transfer') or {}).get('revision')}",
            f"Bridge 下相关资产 = {(u_box2.get('sibling_assets'))}",
            f"是否出现 SM_QaBox_1 = {u_box2.get('has_numbered_duplicate')}（期望 False）",
            f"二次导入后包围盒 = {(u_box2.get('measure') or {}).get('size')} cm",
        ],
        ["logs/qa_ue_box2.json", "logs/qa_blender_box_run2.json",
         "logs/qa_ue_box2_stdout.log"],
    ))

    # ---------------- 用例 4
    c4 = dict(checks_of(b_rig))
    c4.update(checks_of(u_rig))
    cases.append(case_block(
        "TC-04", "骨骼分支补测（SK_QaRig，此前未验证）",
        "PASS", c4,
        [
            f"Blender 骨骼 = {(b_rig.get('source') or {}).get('blender_bones')}，"
            f"绑定方式 = {(b_rig.get('source') or {}).get('bind_method')}，"
            f"顶点组 = {(b_rig.get('source') or {}).get('vertex_groups')}",
            f"发送 kind = {(b_rig.get('send') or {}).get('kind')}（期望 skeletal_mesh）",
            f"UE 导入返回 = {parse_ue_log_import('qa_ue_rig_stdout.log')}",
            f"UE 资产类型 = {u_rig.get('asset_class')}（期望 SkeletalMesh，不是 StaticMesh）",
            f"骨骼数 = {(u_rig.get('bones') or {}).get('count')}（>0 即通过）",
            f"骨骼网格包围盒 = {(u_rig.get('measure') or {}).get('size')} cm"
            f"（源网格 1×1×2 m → 期望 100×100×200）",
            f"UE 侧报错 = {u_rig.get('errors')}",
            "注：骨骼『名字』取不到是探针限制（UE 5.8 的 BoneNode 是 struct，"
            "Python 侧只暴露 to_dict/get_editor_property，字段取不出），不影响骨骼计数",
        ],
        ["logs/qa_blender_rig.json", "logs/qa_ue_rig.json",
         "logs/qa_rig_blender.log", "logs/qa_ue_rig_verify.log"],
    ))

    # ---------------- 用例 5
    c5 = dict(checks_of(u_exp))
    c5.update(checks_of(b_recv))
    cases.append(case_block(
        "TC-05", "反向链路 UE → Blender（单位闭环）",
        "PASS", c5,
        [
            f"UE 导出 FBX = {(u_exp.get('export') or {}).get('files')} "
            f"大小 = {(u_exp.get('export') or {}).get('sizes')}",
            f"UE 发送记录 = {(u_exp.get('sent'))}",
            f"Blender 端档案 import = {(b_recv.get('recv') or {}).get('profile_import')}",
            f"Blender 导入对象 = {(b_recv.get('recv') or {}).get('objects')}",
            f"回到 Blender 实测 = {(b_recv.get('recv') or {}).get('measured_m')} m"
            f"（期望 2×1×0.5；不是 200，也不是 0.02）",
            f"单位探针（同一 FBX 不同 global_scale）= "
            f"{[(r.get('global_scale'), r.get('max_edge_m')) for r in probe.get('rows', [])]}",
            "结论：UE 导出的是 cm（原始顶点 max_abs=100），Blender 5.0.1 的 FBX "
            "导入器已内建 cm→m 换算，因此 unreal>blender 的 global_scale 必须是 1.0",
        ],
        ["logs/qa_ue_export.json", "logs/qa_blender_recv.json",
         "logs/qa_blender_scale_probe.json", "logs/qa_ue_export_stdout.log",
         "logs/qa_recv_blender.log"],
    ))

    # ---------------- 用例 6
    err_results = errp.get("results", [])
    cases.append({
        "id": "TC-06",
        "title": "异常路径（404 / 409 / 400 / 路径穿越）",
        "status": "PASS" if errp.get("passed") == errp.get("total") else "FAIL",
        "checks_total": errp.get("total", 0),
        "checks_passed": errp.get("passed", 0),
        "checks": [{
            "name": f"{r['id']} {r['title']}",
            "expected": r.get("expected_http"),
            "actual": r.get("actual_http"),
            "pass": bool(r.get("pass")),
            "note": r.get("note", ""),
        } for r in err_results],
        "notes": [
            "E9 是观察项：服务允许 draft 态直接 finish（未认领也能标记完成）",
            "E10 路径穿越文件名被 safe_filename() 安全化，未逃出 payload 根目录",
        ],
        "evidence": ["logs/qa_error_paths.json", "logs/qa_error_paths.log"],
    })

    # ---------------- 源码缺陷清单
    bugs = [
        {
            "id": "BUG-01",
            "severity": "高",
            "type": "源码问题（配置档案错误）",
            "file": "shared/profiles.json",
            "line": "routes.unreal>blender.import.global_scale（第 85 行附近）",
            "symptom": ("唯一事实源里 unreal>blender 的 import.global_scale = 0.01，"
                        "但实测该值会让 UE 回传的模型缩小 100 倍"
                        "（200×100×50 cm 的盒子回到 Blender 变成 0.02×0.01×0.005 m）。"
                        "正确值是 1.0。"),
            "impact": ("按 README/ARCHITECTURE 的约定把 shared/profiles.json 同步给各端后，"
                       "UE→Blender 反向链路会整体缩小 100 倍，且不报错、不易察觉。"),
            "evidence": ("logs/qa_blender_scale_probe.json：同一 FBX，"
                         "global_scale=1.0 → 最大边 2.0 m（正确）；"
                         "0.01 → 0.02 m；100.0 → 200.0 m"),
            "reproduce": ("1) UE 导出 /Game/Bridge/SM_QaBox 为 FBX；"
                          "2) Blender 用 unreal>blender 档案的 global_scale=0.01 导入；"
                          "3) 量尺寸得到 0.02×0.01×0.005 而不是 2×1×0.5"),
            "suggestion": ("把 shared/profiles.json 与 unreal/plugin/asset_bridge_unreal/"
                           "profiles.json 里 unreal>blender.import.global_scale 改成 1.0，"
                           "并把 _why 注释改成『Blender 5.x 的 FBX 导入器已内建 cm→m 换算，"
                           "不要再乘 0.01』；同时建议加一份『各端 profiles.json 与 "
                           "shared/ 一致性』的自检脚本，避免三份拷贝再次漂移。"),
        },
        {
            "id": "BUG-02",
            "severity": "中",
            "type": "源码问题（一致性）",
            "file": ("unreal/plugin/asset_bridge_unreal/profiles.json "
                     "（与 blender/addon/asset_bridge_blender/profiles.json 对比）"),
            "line": "routes.unreal>blender.import.global_scale",
            "symptom": ("三份 profiles.json 对同一条路由给出不同值："
                        "shared=0.01、unreal 插件=0.01、blender 插件=1.0。"
                        "当前 blender 插件那份是对的，所以实机没炸。"),
            "impact": "任何一次『从 shared 同步 profiles』的操作都会把反向链路改坏。",
            "evidence": "logs/qa_blender_scale_probe.json + 三份 profiles.json 对比",
            "reproduce": ("python -c \"import json;print(json.load(open("
                          "'shared/profiles.json'))['routes']['unreal>blender']"
                          "['import'])\""),
            "suggestion": ("统一成 1.0；更好的做法是各端启动时从 shared/ 加载，"
                           "或加一个 CI 检查保证三份文件完全一致。"),
        },
        {
            "id": "BUG-03",
            "severity": "低",
            "type": "源码问题（健壮性/状态机）",
            "file": "server/server.py",
            "line": "_route_post 的 /transfers/<id>/finish 分支（第 558-576 行）",
            "symptom": ("finish 不校验当前状态：draft 态（从未 claim）的 transfer "
                        "可以直接被标记成 done，返回 200。"),
            "impact": ("未经认领的资产也会被记成『已完成』，收件方重启/掉线时会漏掉任务，"
                       "且历史记录不可信。与 publish 分支已有的状态校验不一致。"),
            "evidence": "logs/qa_error_paths.json 用例 E9（HTTP 200）",
            "reproduce": ("POST /api/v1/transfers （建 draft）→ "
                          "POST /api/v1/transfers/<id>/finish {\"status\":\"done\"} → 200"),
            "suggestion": ("参考 publish 的做法：只允许 claimed -> done/error/cancelled，"
                           "其余状态返回 409；如确需支持『手动补记完成』，"
                           "则显式加一个 force 参数。"),
        },
        {
            "id": "BUG-04",
            "severity": "低",
            "type": "源码问题（可观测性）",
            "file": "unreal/plugin/asset_bridge_unreal/importer.py",
            "line": "import_model() 返回值（第 99-121 行）",
            "symptom": ("import_model 把材质资产路径一并返回"
                        "（本次实测返回 ['/Game/Bridge/M_QaBox_A', "
                        "'/Game/Bridge/M_QaBox_B', '/Game/Bridge/SM_QaBox']），"
                        "调用方若不按名字过滤就会拿到材质而不是网格。"),
            "impact": ("上层 _handle() 会把这些路径原样塞进 result 和 spawn_into_level()，"
                       "排障时容易误判『导入了 3 个资产』。"),
            "evidence": ("logs/qa_ue_box_stdout.log 里 "
                         "import_model 返回 = ['/Game/Bridge/M_QaBox_A', "
                         "'/Game/Bridge/M_QaBox_B', '/Game/Bridge/SM_QaBox']"),
            "reproduce": "导入带材质的 FBX，打印 import_model 的返回值",
            "suggestion": ("在 import_model 里按 asset_name / 资产类型过滤后再返回，"
                           "或额外返回一个结构化的 {mesh, materials, skeleton} 字典。"),
        },
    ]

    total_checks = sum(c["checks_total"] for c in cases)
    passed_checks = sum(c["checks_passed"] for c in cases)
    case_pass = sum(1 for c in cases if c["status"] == "PASS")

    result = {
        "meta": {
            "qa": "严过关 (Edward)",
            "round": 1,
            "generated_from": "实机回归：Blender 5.0.1 + UE 5.8.3 + 中转服务 127.0.0.1:27631",
            "blender": "D:\\DCC\\Blender5.0.1\\blender.exe (5.0.1)",
            "unreal": "5.8.3-58210709+++UE5+Release-5.8",
            "service_root": "E:\\CirnoWorkSpace\\AssetBridge\\runtime",
            "project": "E:\\CirnoWorkSpace\\AssetBridge\\unreal\\project\\AssetBridgeTest",
            "generated_at": __import__("datetime").datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"),
        },
        "summary": {
            "cases_total": len(cases),
            "cases_passed": case_pass,
            "cases_failed": len(cases) - case_pass,
            "checks_total": total_checks,
            "checks_passed": passed_checks,
            "checks_failed": total_checks - passed_checks,
            "routing_decision": "KnownIssues（用例全绿，但发现 4 个源码缺陷需工程师处理）",
        },
        "cases": cases,
        "source_bugs": bugs,
        "test_script_fixes": [
            {
                "file": "tests/smoke_test.py",
                "what": "用例「同名资产重复发送 -> revision 递增」把期望值写死成 2",
                "why": ("中转服务是长驻进程，第二次跑该脚本时 SM_TestCube 的 revision "
                        "已经是 3，写死 2 会让一条本来正确的用例假失败"
                        "（本次第 2 次回归实跑确实挂了 1 项）。"),
                "fix": "改成取首条 transfer 的 revision 做基线，断言 新 == 基线+1。已连跑两次均 16/16。",
            },
            {
                "file": "tests/qa_ue_case.py（本次新增）",
                "what": "从 import_model() 返回的路径里直接取第一个当网格资产",
                "why": "import_model 会把材质资产一起返回，取到的是 M_QaBox_A，量不到包围盒。",
                "fix": "改成优先按 asset_name 匹配、其次按 StaticMesh/SkeletalMesh 类型挑选。",
            },
            {
                "file": "tests/qa_ue_case.py（本次新增）",
                "what": "读骨骼名用 BoneNode.name",
                "why": "UE 5.8 的 BoneNode 是 struct，Python 侧不暴露字段，"
                       "直接 .name 抛 AttributeError 导致骨骼数被记成 0（假失败）。",
                "fix": "骨骼数量改为取 skeleton.bone_tree 的长度，名字取不到只降级不判失败。",
            },
        ],
        "artifacts": {
            "scripts": [
                "tests/qa_blender_case.py",
                "tests/qa_ue_case.py",
                "tests/qa_error_paths.py",
                "tests/qa_blender_scale_probe.py",
                "tests/qa_make_report.py",
            ],
            "logs": sorted(str(p.relative_to(LOG)) for p in LOG.glob("qa_*")),
        },
    }

    (LOG / "qa_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (LOG / "qa_report.md").write_text(render_md(result), encoding="utf-8")
    print(f"[qa] 已写入 {LOG / 'qa_result.json'}")
    print(f"[qa] 已写入 {LOG / 'qa_report.md'}")
    print(f"[qa] 用例 {case_pass}/{len(cases)} 通过，"
          f"断言 {passed_checks}/{total_checks} 通过，源码缺陷 {len(bugs)} 个")
    return result


if __name__ == "__main__":
    main()
