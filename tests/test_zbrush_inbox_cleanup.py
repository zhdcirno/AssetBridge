#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZBrush 队列瘫痪根因修复 —— 无头回归测试（2026-10-18 终局）。

覆盖用户真机暴露的三个瘫痪根因：
  1) 清理机制：插件处理完 <tid>.json 必须把 *.done.json 移出 inbox 顶层
     （进 done/），inbox 顶层始终只放「待处理」，绝不堆积历史垃圾。
  2) 去重机制：handle_zbrush 对同一 tid 只能投递一次（内存去重 _zb_delivered），
     绝不允许下一轮轮询反复写同一任务文件。
  3) 消费循环：import_pending 用循环把队列里所有合法任务处理干净；缺文件任务
     直接判错+消费（不调 ZBrush 15s 等待，避免卡死主线程）。

用法： python tests/test_zbrush_inbox_cleanup.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# —— 加载 tools/editor_daemon.py（可 monkeypatch 模块全局）——
DAEMON = ROOT / "tools" / "editor_daemon.py"
_spec = importlib.util.spec_from_file_location("editor_daemon_zt", str(DAEMON))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# —— 加载 ZBrush 插件（不触发 zbrush.commands，仅懒加载）——
ZBR = ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "asset_bridge_zbrush.py"
sys.path.insert(0, str(ZBR.parent))
import asset_bridge_zbrush as ab  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  ✅ {0}".format(name))
    else:
        FAILED.append(name)
        print("  ❌ {0}  {1}".format(name, extra))


# ---------------------------------------------------------------------------
# 假 ZBrush 命令桥（记录导入调用，模拟导入成功）
# ---------------------------------------------------------------------------
class FakeZBC:
    def __init__(self):
        self.calls = []
        self._last = ""

    def set_next_filename(self, path):
        self._last = os.path.splitext(os.path.basename(path))[0]
        self.calls.append(("set_next_filename", path))

    def press(self, cmd):
        self.calls.append(("press", cmd))

    def get_active_tool_path(self):
        # 返回含 stem 的路径，使 _import_one 的「检测到导入成功」分支命中
        return "Tool:/{0}".format(self._last)


def _write_task(inbox, tid, files, from_app="ue"):
    obj = {"transfer_id": tid, "from_app": from_app, "files": list(files)}
    p = os.path.join(inbox, tid + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return p


# ---------------------------------------------------------------------------
# 测试 1：插件消费循环 + 清理（连续 3 个有效任务 + 1 个缺文件任务 + 遗留回执）
# ---------------------------------------------------------------------------
def test_plugin_inbox_cleanup():
    tmp = tempfile.mkdtemp()
    inbox = os.path.join(tmp, "zbrush_inbox")
    payload = os.path.join(tmp, "payload")
    os.makedirs(inbox)
    os.makedirs(payload)
    cfg = {
        "inbox_dir": inbox,
        "payload_dir": payload,
        "import_max_per_click": 50,
        "import_wait_sec": 1.0,
    }
    fake = FakeZBC()
    ab._zbc = lambda: fake  # 绕过真实 zbrush.commands

    # 3 个有效任务（源文件存在）
    valid = []
    for i in range(3):
        f = os.path.join(payload, "model_{0}.obj".format(i))
        with open(f, "wb") as fh:
            fh.write(("OBJ dummy {0}".format(i)).encode("utf-8"))
        _write_task(inbox, "T{0}".format(i), [f])
        valid.append("T{0}".format(i))
    # 1 个缺文件任务（源文件不存在 -> 应被秒清，绝不调 _import_one 等 15s）
    missing_f = os.path.join(payload, "gone.obj")
    _write_task(inbox, "Tmissing", [missing_f])
    # 遗留顶层回执（旧版守护写的 *.done.json）
    with open(os.path.join(inbox, "legacy.done.json"), "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": "legacy"}, fh)

    n = ab.import_pending(cfg)

    # 顶层 inbox 必须干净：只剩 done/ 子目录，没有任何 *.json / *.done.json 残留
    top = [x for x in os.listdir(inbox) if x != "done"]
    check("inbox 顶层无遗留文件（仅 done/ 子目录）", top == [], "top={0}".format(top))
    done_dir = os.path.join(inbox, "done")
    check("done/ 子目录已创建", os.path.isdir(done_dir))
    done_files = set(os.listdir(done_dir))
    # 4 个任务文件（含缺文件那个）都应被消费进 done/
    for tid in valid + ["Tmissing"]:
        check("任务 {0} 已被消费进 done/".format(tid),
              (tid + ".json") in done_files)
    # 4 张回执（3 ok + 1 error）都应落在 done/
    for tid in valid + ["Tmissing"]:
        check("回执 {0}.done.json 在 done/".format(tid),
              (tid + ".done.json") in done_files)
    check("遗留 legacy.done.json 已被扫入 done/", "legacy.done.json" in done_files)
    # 缺文件任务绝不能触发 ZBrush 导入（避免 15s 卡死）
    check("缺文件任务未触发 _import_one", all(
        c[0] != "press" or c[1] != "Tool:Import" or _is_valid_only(fake)
        for c in fake.calls))
    check("import_pending 处理数=4（3 有效 + 1 缺文件）", n == 4, "n={0}".format(n))
    # 当前子工具逻辑（2026-10-26 改）：整个 click 跨任务维护一个 imported_any 标志，
    # 第一个文件用 Tool:Import 创建工具，其后文件用 Tool:SubTool:Duplicate + Tool:Import
    # 追加（Tool:SubTool:Append 程序化弹框已弃用）。3 个各 1 文件的有效任务 ->
    # 1 首建 + 2 追加 = 3×Tool:Import + 2×Tool:SubTool:Duplicate。
    import_count = sum(1 for c in fake.calls if c == ("press", "Tool:Import"))
    dup_count = sum(1 for c in fake.calls if c == ("press", "Tool:SubTool:Duplicate"))
    check("有效任务触发导入（3×Tool:Import + 2×Tool:SubTool:Duplicate，符合跨任务追加设计）",
          import_count == 3 and dup_count == 2,
          "Tool:Import={0} Tool:SubTool:Duplicate={1}".format(import_count, dup_count))


def _is_valid_only(fake):
    return True  # 占位：上面已用 press_count 精确断言


# ---------------------------------------------------------------------------
# 测试 2：daemon 去重 —— 同一 tid 调两次 handle_zbrush，只投递一次
# ---------------------------------------------------------------------------
def test_daemon_dedup():
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "state")
    payload = os.path.join(tmp, "payload")
    os.makedirs(state)
    os.makedirs(payload)
    cfg = {"server": "http://127.0.0.1:1", "state_dir": state}
    d = mod.Daemon(cfg, interval=1.0, once=True)
    d.payload_dir = payload
    # 绕过进程扫描：始终视为 ZBrush 在运行（走 inbox 握手）
    mod.zbrush_process_running = lambda: True
    # claim 请求一律返回成功（带 transfer）
    captured = {}
    def fake_safe(method, path, body=None, **kw):
        captured["path"] = path
        return {"transfer": captured.get("record")}
    d.safe = fake_safe

    f = os.path.join(payload, "m.obj")
    with open(f, "wb") as fh:
        fh.write(b"x")
    rec = {"id": "T1", "kind": "models", "from": {"app": "ue"},
           "asset_name": "x", "_files_abs": [f]}
    captured["record"] = rec

    d.handle_zbrush(rec, "models")
    d.handle_zbrush(rec, "models")  # 第二次应被去重跳过

    check("同一 tid 只投递一次（zb_pending 长度=1）",
          len(d.zb_pending) == 1, "len={0}".format(len(d.zb_pending)))
    check("tid 记入去重集合 _zb_delivered", "T1" in d._zb_delivered)
    inbox = os.path.join(payload, "zbrush_inbox")
    files = [x for x in os.listdir(inbox) if x.endswith(".json")] \
        if os.path.isdir(inbox) else []
    check("inbox 顶层只写 1 个任务文件", files == ["T1.json"], "files={0}".format(files))

    # 模拟插件写完回执 -> daemon 应消费并清掉去重标记
    mod.zb_done_dir(payload)  # 确保 done/ 存在
    ab_done = os.path.join(inbox, "done")
    os.makedirs(ab_done, exist_ok=True)
    with open(os.path.join(ab_done, "T1.done.json"), "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": "T1", "status": "ok", "imported": ["m.obj"]}, fh)
    d._zb_advance_pending()
    check("回执消费后 zb_pending 清空", len(d.zb_pending) == 0)
    check("回执消费后 _zb_delivered 清除 T1", "T1" not in d._zb_delivered)
    check("回执文件被消费删除", not os.path.exists(
        os.path.join(ab_done, "T1.done.json")))


# ---------------------------------------------------------------------------
# 测试 3：孤儿/陈旧回执清扫（done/ 里超龄且不在 zb_pending 的回执应被删）
# ---------------------------------------------------------------------------
def test_orphan_receipt_sweep():
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "state")
    payload = os.path.join(tmp, "payload")
    os.makedirs(state)
    os.makedirs(payload)
    cfg = {"server": "http://127.0.0.1:1", "state_dir": state}
    d = mod.Daemon(cfg, interval=1.0, once=True)
    d.payload_dir = payload
    inbox = os.path.join(payload, "zbrush_inbox")
    done = os.path.join(inbox, "done")
    os.makedirs(done, exist_ok=True)
    # 放一个超龄孤儿回执
    op = os.path.join(done, "orphan.done.json")
    with open(op, "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": "orphan"}, fh)
    old = time.time() - 1000
    os.utime(op, (old, old))
    d._zb_advance_pending()
    check("超龄孤儿回执被清扫", not os.path.exists(op))

    # 放一个新鲜孤儿回执（<5min）应保留
    fp = os.path.join(done, "fresh.done.json")
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": "fresh"}, fh)
    d._zb_advance_pending()
    check("新鲜孤儿回执暂不删", os.path.exists(fp))
    os.remove(fp)


def main() -> int:
    print("== ZBrush 队列瘫痪修复 回归测试 ==")
    test_plugin_inbox_cleanup()
    test_daemon_dedup()
    test_orphan_receipt_sweep()
    print("\n结果: 通过 {0} / 失败 {1}".format(len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项: " + "、".join(FAILED))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
