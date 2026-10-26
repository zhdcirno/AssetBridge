# -*- coding: utf-8 -*-
"""ZBrush 一对一多轮次修复验证（2026-10-19 穿透修复 #2）。

场景：发 A 收 A（成功）-> 发 B 收 B（成功）-> 发 C 收 C（成功），共 3 轮。
旧 bug：只有第一次成功，后续轮次无反应（状态残留 / 任务未独立处理）。

断言：
  - 每轮 import_pending 都返回 1（各自独立处理本轮回任务）。
  - 每轮回执 status=="ok"，任务文件被消费，且**不会**误吞其它轮次的任务。
  - 3 轮结束后 inbox 顶层无残留，done/ 恰有 3 份 ok 回执。
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLUGIN = load_module(
    "asset_bridge_zbrush_seq3",
    ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "asset_bridge_zbrush.py",
)


def make_obj(path, size=1.0):
    v = size / 2.0
    verts = [(-v, -v, -v), (v, -v, -v), (v, v, -v), (-v, v, -v),
             (-v, -v, v), (v, -v, v), (v, v, v), (-v, v, v)]
    lines = ["v %r %r %r" % t for t in verts] + [
        "f 1 4 3 2", "f 2 3 7 6", "f 3 4 8 7", "f 1 2 6 5", "f 5 6 7 8", "f 1 5 8 4"]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeZbc(object):
    def __init__(self, tool_after="cube", dialog=False, raise_on_press=False):
        self.calls = []
        self.tool_after = tool_after
        self.dialog = dialog
        self.raise_on_press = raise_on_press
        self._subtools = 0   # 建模当前工具子工具数（Import 首建 / Duplicate +1）

    def has_next_filename(self):
        return self.dialog

    def set_next_filename(self, p):
        self.calls.append(("set_next_filename", p))

    def press(self, item):
        self.calls.append(("press", item))
        if item == "Tool:SubTool:Duplicate":
            self._subtools += 1
        elif item == "Tool:Import":
            if self._subtools == 0:
                self._subtools = 1
        if self.raise_on_press:
            raise RuntimeError("native dialog blocked")

    def get_subtool_count(self):
        return self._subtools

    def get_active_tool_path(self):
        return self.tool_after

    def update(self, n=1, redraw=False):
        self.calls.append(("update", n, redraw))

    def set_notebar_text(self, t, p=0.0):
        self.calls.append(("notebar", t))


class TestSeq3OneToOne(unittest.TestCase):
    def setUp(self):
        self._orig_zbc = PLUGIN._zbc
        self.td = tempfile.mkdtemp()
        self.inbox = os.path.join(self.td, "zbrush_inbox")
        os.makedirs(self.inbox)
        self.cfg = dict(PLUGIN.DEFAULT_CONFIG)
        self.cfg.update({
            "payload_dir": self.td,
            "inbox_dir": self.inbox,
            "outbox_dir": os.path.join(self.td, "zbrush_outbox"),
            "import_wait_sec": 3.0,
            "import_max_per_click": 12,
        })
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None
        PLUGIN._STATE["watcher"] = False

    def tearDown(self):
        PLUGIN._zbc = self._orig_zbc
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None
        PLUGIN._STATE["watcher"] = False

    @staticmethod
    def _count_press(fake, item):
        return sum(1 for c in fake.calls if c == ("press", item))

    def _round(self, tid, fname):
        """发一个任务并点 1 次接收，返回 (消费数, fake_zbc)。"""
        fpath = os.path.join(self.td, fname)
        make_obj(fpath)
        task = {"transfer_id": tid, "from_app": "blender", "files": [fpath]}
        Path(os.path.join(self.inbox, tid + ".json")).write_text(
            json.dumps(task), encoding="utf-8")
        fake = FakeZbc(tool_after="cube")
        PLUGIN._zbc = lambda: fake
        n = PLUGIN.import_pending(self.cfg)
        return n, fake

    def test_one_to_one_three_rounds(self):
        # 发 A 收 A
        n1, f1 = self._round("t1", "a.obj")
        self.assertEqual(n1, 1, "第 1 轮应消费 1 个任务")
        self.assertEqual(self._count_press(f1, "Tool:Import"), 1)
        # 发 B 收 B（独立轮次，必须成功，不残留）
        n2, f2 = self._round("t2", "b.obj")
        self.assertEqual(n2, 1, "第 2 轮应消费 1 个任务（旧 bug：只有第一次成功）")
        self.assertEqual(self._count_press(f2, "Tool:Import"), 1)
        # 发 C 收 C
        n3, f3 = self._round("t3", "c.obj")
        self.assertEqual(n3, 1, "第 3 轮应消费 1 个任务")
        self.assertEqual(self._count_press(f3, "Tool:Import"), 1)
        # 3 轮结束后：done/ 恰 3 份 ok 回执，顶层无残留
        for tid in ("t1", "t2", "t3"):
            rp = os.path.join(PLUGIN.done_dir(self.inbox), tid + ".done.json")
            self.assertTrue(os.path.isfile(rp), "缺少回执: " + tid)
            receipt = json.loads(Path(rp).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "ok", tid + " 回执非 ok")
            self.assertTrue(os.path.isfile(
                os.path.join(PLUGIN.done_dir(self.inbox), tid + ".json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t1.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t2.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t3.json")))

    def test_no_cross_contamination_between_rounds(self):
        """每轮只应处理本轮回的任务，绝不能把上一轮已消费的任务再拉回来。"""
        # 第 1 轮：发 t1
        n1, _ = self._round("t1", "a.obj")
        self.assertEqual(n1, 1)
        # 第 2 轮：发 t2（t1 已消费，不应再出现）
        n2, f2 = self._round("t2", "b.obj")
        self.assertEqual(n2, 1)
        # 第 2 轮不应误触 t1 的文件：确认 done/ 里 t1 回执仍只有 1 份、未被改写
        rp1 = os.path.join(PLUGIN.done_dir(self.inbox), "t1.done.json")
        r1 = json.loads(Path(rp1).read_text(encoding="utf-8"))
        self.assertEqual(r1["imported"], ["a.obj"])
        self.assertEqual(os.path.isfile(os.path.join(self.inbox, "t2.json")), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
