# -*- coding: utf-8 -*-
"""回归测试：ZBrush 导入验证逻辑（2026-10-20 修复）。

不依赖真机 ZBrush（沙箱跑不了 GUI）。用可控 mock zbc 验证「真实逻辑」：
  · 彻底弃用 get_active_tool_path 字符串匹配（旧 poll 循环必须消失）；
  · 改用子工具数量变化判定（Append: +1 / Import: >=1 且工具被替换）；
  · 硬超时（import_verify_sec）后下结论，绝不无限挂起；
  · 返回准确 ok/False（不再无条件 SUCCESS），根除「假 done」；
  · 单次接收上限 import_max_per_click（默认 3），超出留 inbox 下次再收；
  · 每任务打 begin/end 与 结果=成功/失败/超时。

注意：mock 只模拟 zbc 的「返回值」，不假装「导入一定成功」——失败场景如实返回 False。
"""

import os
import sys
import json
import tempfile
import shutil

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "..", "plugins", "zbrush",
                          "asset_bridge_zbrush")
sys.path.insert(0, os.path.abspath(PLUGIN_DIR))

import asset_bridge_zbrush as zb  # noqa: E402


class MockZbc:
    """可控 zbc：模拟「导入前后子工具数量 / 工具路径」的变化。

    set_next_filename 在每个 _import_one 开头被调用 -> 重置 pressed，模拟「新一轮导入」。
    press 模拟同步导入完成 -> 把计数从 before 切到 after。
    """

    def __init__(self, before=0, after=1, path_before="", path_after="mesh.obj"):
        self.before = before
        self.after = after
        self.path_before = path_before
        self.path_after = path_after
        self.pressed = False
        self.press_calls = []
        self.update_calls = 0

    def set_next_filename(self, path):
        self.pressed = False  # 新一轮导入：计数回到 before

    def press(self, cmd):
        self.pressed = True  # 同步导入完成：计数切到 after
        self.press_calls.append(cmd)

    def get_subtool_count(self):
        return self.after if self.pressed else self.before

    def get_active_tool_path(self):
        return self.path_after if self.pressed else self.path_before

    def update(self, a, b):
        self.update_calls += 1  # 非阻塞让出主线程（替代 sleep 阻塞 UI）


def _cfg(**over):
    cfg = dict(zb.DEFAULT_CONFIG)
    cfg.update(over)
    # 缩短验证窗口，让超时测试秒级完成（真实默认是 DEFAULT_CONFIG 的 5.0s）
    cfg["import_verify_sec"] = over.get("import_verify_sec", 0.3)
    cfg["import_wait_sec"] = over.get("import_wait_sec", 1.0)
    return cfg


def _run_import_one(before, after, append, path_before="", path_after="mesh.obj",
                    press_never_changes=False):
    """跑一次 _import_one，返回 (ok, log_lines)。

    press_never_changes：模拟「press 不切换计数」（硬超时场景）。
    """
    captured = []
    zb.diag_log = lambda cfg, msg: captured.append(msg)
    # 需要一个真实存在的文件，否则 _import_one 在 os.path.isfile 守卫处直接返回
    tmpf = tempfile.NamedTemporaryFile(suffix=".obj", delete=False)
    tmpf.write(b"# test\nv 0 0 0\n")
    tmpf.close()
    try:
        m = MockZbc(before, after, path_before, path_after)
        if press_never_changes:
            def _noop_press(cmd):
                m.press_calls.append(cmd)  # 不切计数，模拟 UI 无响应
            m.press = _noop_press
        cfg = _cfg()
        ok, msg = zb._import_one(m, tmpf.name, cfg, append=append)
        return ok, captured, m
    finally:
        try:
            os.unlink(tmpf.name)
        except OSError:
            pass


def test_append_success_via_subtool_delta():
    # Append：before=1 -> after=2 (delta +1) => 成功
    ok, log, m = _run_import_one(before=1, after=2, append=True)
    assert ok is True, "Append 子工具 +1 应判成功"
    assert "Tool:SubTool:Duplicate" in m.press_calls, \
        "追加必须走 Duplicate+Tool:Import 套路（Append 程序化会弹框已弃用）"
    assert "Tool:Import" in m.press_calls, \
        "Duplicate 后必须用已验证无对话框的 Tool:Import 替换几何"
    assert not any("get_active_tool_path poll" in l for l in log), \
        "旧 poll 循环必须消失"
    assert any("import_one result" in l and "SUCCESS" in l for l in log)


def test_import_success_via_subtool_count_and_path_change():
    # Import：before=0 -> after=1 (>=1 且工具路径切换) => 成功
    ok, log, m = _run_import_one(before=0, after=1, append=False,
                                 path_before="", path_after="E:/x/mesh.obj")
    assert ok is True, "Import 后承载几何应判成功"
    assert "Tool:Import" in m.press_calls
    # 旧判定依赖 stem 命中 get_active_tool_path —— 必须不再出现
    assert not any("detected tool match stem" in l for l in log)


def test_append_failure_when_no_subtool_added():
    # 追加但子工具数没 +1（before=1, after=1）=> 失败。新机制用 Duplicate+Tool:Import
    # 追加：Duplicate 复制当前子工具（纯内存），若最终子工具数未 +1（如 Duplicate/Import
    # 被宿主拦截）必须如实判失败，不得静默兜底或盲猜降级。
    ok, log, m = _run_import_one(before=1, after=1, append=True)
    assert ok is False, "子工具未 +1 必须判失败"
    assert "Tool:SubTool:Duplicate" in m.press_calls, "追加必须走 Duplicate+Import 套路"
    assert any("FAIL" in l for l in log)


def test_import_failure_when_no_geometry():
    # Import 后无任何几何（after=0）=> 失败
    ok, log, m = _run_import_one(before=0, after=0, append=False)
    assert ok is False, "导入后无几何必须判失败"


def test_hard_timeout_returns_false_not_hang():
    # press 不切换计数（UI 无响应）：硬超时后返回 False，绝不无限挂起
    import time
    t0 = time.time()
    ok, log, m = _run_import_one(before=1, after=1, append=True,
                                 press_never_changes=True)
    dt = time.time() - t0
    assert ok is False, "超时必须判失败"
    # 验证窗口为 import_verify_sec(0.3)，整体远小于旧 15s 死等
    assert dt < 2.0, "硬超时后必须快速返回，不得长挂起（耗时 %.2fs）" % dt
    assert m.update_calls > 0, "超时窗口内必须调用 zbc.update 让出主线程"


def test_per_receive_limit_and_begin_end_log():
    """单次接收上限 import_max_per_click=3：5 个任务只处理 3 个，剩 2 个留 inbox。

    同时验证每任务打 begin/end 与 结果=成功/失败/超时。
    """
    tmp = tempfile.mkdtemp(prefix="zb_inbox_test_")
    try:
        inbox = os.path.join(tmp, "zbrush_inbox")
        os.makedirs(inbox)
        # 一个真实 obj 文件，5 个任务都引用它
        obj = os.path.join(tmp, "mesh.obj")
        with open(obj, "w", encoding="utf-8") as fh:
            fh.write("# test\nv 0 0 0\n")
        for i in range(5):
            tid = "task%02d" % i
            data = {"transfer_id": tid, "from_app": "unreal", "files": [obj]}
            with open(os.path.join(inbox, tid + ".json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)

        captured = []
        zb.diag_log = lambda cfg, msg: captured.append(msg)
        zb._touch_heartbeat = lambda cfg: None
        zb._write_status = lambda cfg: None
        zb._notebar = lambda *a, **k: None
        zb._resolve_inbox_path = lambda cfg: (inbox, "")
        m = MockZbc(before=0, after=1, path_before="", path_after="mesh.obj")
        zb._zbc = lambda: m

        cfg = _cfg(import_max_per_click=3, payload_dir=tmp, inbox_dir=inbox)
        done = zb.import_pending(cfg, log=None)

        # 只处理 3 个
        assert done == 3, "单次接收上限 3，实际处理 %r" % done
        # inbox 剩 2 个未处理
        remain = [n for n in os.listdir(inbox) if n.endswith(".json")]
        assert len(remain) == 2, "应剩 2 个任务留 inbox，实际 %r" % remain
        # done/ 里有 3 个消费文件 + 3 个回执
        done_dir = os.path.join(inbox, "done")
        assert os.path.isdir(done_dir)
        consumed = [n for n in os.listdir(done_dir) if n.endswith(".json") and not n.endswith(".done.json")]
        receipts = [n for n in os.listdir(done_dir) if n.endswith(".done.json")]
        assert len(consumed) == 3, "应有 3 个消费文件，实际 %r" % consumed
        assert len(receipts) == 3, "应有 3 个回执，实际 %r" % receipts

        # begin/end 日志 + 剩余提示
        begins = [l for l in captured if "begin" in l and "import " in l]
        ends = [l for l in captured if "end" in l and "结果=" in l]
        assert len(begins) == 3, "应打 3 条 begin，实际 %r" % begins
        assert len(ends) == 3, "应打 3 条 end，实际 %r" % ends
        assert any("结果=成功" in l for l in ends), "应含 结果=成功"
        assert any("本次导入 3 个任务，剩余 2 个留在 inbox" in l for l in captured), \
            "必须打印剩余提示"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_append_success_via_subtool_delta()
    test_import_success_via_subtool_count_and_path_change()
    test_append_failure_when_no_subtool_added()
    test_import_failure_when_no_geometry()
    test_hard_timeout_returns_false_not_hang()
    test_per_receive_limit_and_begin_end_log()
    print("ALL ZBRUSH IMPORT ONE TESTS PASSED")
