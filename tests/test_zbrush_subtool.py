# -*- coding: utf-8 -*-
"""ZBrush 子工具追加修复验证（2026-10-19 穿透修复 #1）。

场景：连续发 3 个模型，点 1 次接收 -> 3 个模型应作为 3 个**追加子工具**进入同一工具，
而非后一个覆盖前一个（旧 bug：每个文件都 press("Tool:Import") 替换几何，只剩最后 1 个）。

断言：
  - 单次 import_pending 调用中，FakeZbc.calls 含恰好 1 次 ("press","Tool:Import")
    + 2 次 ("press","Tool:SubTool:Append")（即「首建工具、其后追加」语义）。
  - 3 个任务的回执全部落入 inbox/done/ 且 status=="ok"，任务文件被消费（不在顶层残留）。
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
    "asset_bridge_zbrush_subtool",
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
        self._subtools = 0   # 建模当前工具子工具数（Duplicate/Import 会改变）

    def has_next_filename(self):
        return self.dialog

    def set_next_filename(self, p):
        self.calls.append(("set_next_filename", p))

    def press(self, item):
        self.calls.append(("press", item))
        if item == "Tool:SubTool:Duplicate":
            self._subtools += 1          # 复制当前子工具 -> 子工具 +1
        elif item == "Tool:Import":
            if self._subtools == 0:
                self._subtools = 1       # 首建工具承载几何
            # 否则替换当前子工具几何，数量不变
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


class TestSubtoolAppend(unittest.TestCase):
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

    def _task(self, tid, fname):
        fpath = os.path.join(self.td, fname)
        make_obj(fpath)
        task = {"transfer_id": tid, "from_app": "blender", "files": [fpath]}
        Path(os.path.join(self.inbox, tid + ".json")).write_text(
            json.dumps(task), encoding="utf-8")
        return fpath

    @staticmethod
    def _count_press(fake, item):
        return sum(1 for c in fake.calls if c == ("press", item))

    def test_three_separate_transfers_one_click(self):
        """连续发 3 个独立 transfer，点 1 次接收 -> 3 个追加子工具。

        新机制（2026-10-26）：首文件 Tool:Import 创建工具，其后文件用
        Tool:SubTool:Duplicate + Tool:Import 追加（Append 程序化弹框已弃用）。
        故 3 文件 = 3×Tool:Import + 2×Tool:SubTool:Duplicate。
        """
        self._task("t1", "a.obj")
        self._task("t2", "b.obj")
        self._task("t3", "c.obj")
        fake = FakeZbc(tool_after="a")
        PLUGIN._zbc = lambda: fake
        n = PLUGIN.import_pending(self.cfg)
        self.assertEqual(n, 3, "应消费 3 个任务")
        self.assertEqual(self._count_press(fake, "Tool:Import"), 3,
                         "每文件 1 次 Tool:Import（首建 + 2 追加各 1 次）")
        self.assertEqual(self._count_press(fake, "Tool:SubTool:Duplicate"), 2,
                         "其后 2 个文件须 Duplicate 当前子工具再 Import 追加（绝不覆盖）")
        # 3 个回执全部 ok 且任务文件被消费进 done/
        for tid in ("t1", "t2", "t3"):
            rp = os.path.join(PLUGIN.done_dir(self.inbox), tid + ".done.json")
            self.assertTrue(os.path.isfile(rp), "缺少回执: " + tid)
            receipt = json.loads(Path(rp).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "ok", tid + " 回执非 ok")
            self.assertTrue(os.path.isfile(
                os.path.join(PLUGIN.done_dir(self.inbox), tid + ".json")))
        # 顶层无残留（避免历史垃圾堆积触发扫描卡死）
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t1.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t2.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t3.json")))

    def test_single_transfer_three_files_one_click(self):
        """单 transfer 含 3 个文件，点 1 次接收 -> 1 工具 3 子工具（3 Import + 2 Duplicate）。"""
        fpaths = [os.path.join(self.td, n) for n in ("x.obj", "y.obj", "z.obj")]
        for fp in fpaths:
            make_obj(fp)
        task = {"transfer_id": "tA", "from_app": "blender", "files": fpaths}
        Path(os.path.join(self.inbox, "tA.json")).write_text(
            json.dumps(task), encoding="utf-8")
        fake = FakeZbc(tool_after="x")
        PLUGIN._zbc = lambda: fake
        n = PLUGIN.import_pending(self.cfg)
        self.assertEqual(n, 1)
        self.assertEqual(self._count_press(fake, "Tool:Import"), 3)
        self.assertEqual(self._count_press(fake, "Tool:SubTool:Duplicate"), 2)
        rp = os.path.join(PLUGIN.done_dir(self.inbox), "tA.done.json")
        receipt = json.loads(Path(rp).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(set(receipt["imported"]), {"x.obj", "y.obj", "z.obj"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
