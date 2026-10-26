# -*- coding: utf-8 -*-
"""ZBrush 适配器无头自检（不依赖 ZBrush 本体）。

覆盖：
  1. 插件模块纯逻辑：配置加载/目录推导、OBJ bbox 解析（含 #Auto scale 头）、
     inbox 任务扫描、回执写入与消费、transfer body 构造、outbox 稳定判定、
     发布流程（monkeypatch HTTP）。
  2. 守护进程 ZBrush 分支：目录推导、心跳存活判定、任务文件写/回执消费、
     落盘通知（notify.json）。
  3. one_shot_import.py 的参数解析（-script 偏移）。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLUGIN = load_module("asset_bridge_zbrush",
                     ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "asset_bridge_zbrush.py")
ONE_SHOT = load_module("zb_one_shot",
                       ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" / "one_shot_import.py")
DAEMON = load_module("editor_daemon_zb", ROOT / "tools" / "editor_daemon.py")


def make_obj(path, size=1.0, header=("# AssetBridge 1m calibration cube (meters)",)):
    v = size / 2.0
    verts = [(-v, -v, -v), (v, -v, -v), (v, v, -v), (-v, v, -v),
             (-v, -v, v), (v, -v, v), (v, v, v), (-v, v, v)]
    lines = list(header) + ["v %r %r %r" % t for t in verts] + [
        "f 1 4 3 2", "f 2 3 7 6", "f 3 4 8 7", "f 1 2 6 5", "f 5 6 7 8", "f 1 5 8 4"]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestPluginConfig(unittest.TestCase):
    def test_defaults_and_dir_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = PLUGIN.load_config(os.path.join(td, "missing.json"))
            self.assertEqual(cfg["server_url"], "http://127.0.0.1:27631")
            cfg["payload_dir"] = td
            PLUGIN._resolve_dirs(cfg)
            self.assertTrue(cfg["inbox_dir"].endswith("zbrush_inbox"))
            self.assertTrue(cfg["outbox_dir"].endswith("zbrush_outbox"))

    def test_config_file_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "ab_config.json")
            Path(p).write_text(json.dumps({
                "server_url": "http://127.0.0.1:9999",
                "default_to": "maya", "poll_interval": 5.0,
            }), encoding="utf-8")
            cfg = PLUGIN.load_config(p)
            self.assertEqual(cfg["default_to"], "maya")
            self.assertEqual(cfg["poll_interval"], 5.0)

    def test_broken_config_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "ab_config.json")
            Path(p).write_text("{ not json", encoding="utf-8")
            cfg = PLUGIN.load_config(p)
            self.assertEqual(cfg["default_to"], "blender")


class TestObjBbox(unittest.TestCase):
    def test_one_meter_cube(self):
        with tempfile.TemporaryDirectory() as td:
            obj = make_obj(os.path.join(td, "cube.obj"))
            bb = PLUGIN.parse_obj_bbox(obj)
            self.assertEqual(bb["size"], [1.0, 1.0, 1.0])
            self.assertEqual(bb["min"], [-0.5, -0.5, -0.5])
            self.assertTrue(any("AssetBridge" in h for h in bb["header"]))

    def test_auto_scale_header_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            obj = make_obj(os.path.join(td, "cube.obj"),
                           header=("#Auto scale x=2.000000 y=2.000000 z=2.000000",))
            bb = PLUGIN.parse_obj_bbox(obj)
            self.assertTrue(any("Auto scale" in h for h in bb["header"]))


class TestInboxProtocol(unittest.TestCase):
    def test_task_scan_receipt_consume(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = os.path.join(td, "zbrush_inbox")
            os.makedirs(inbox)
            # 任务文件
            task = {"transfer_id": "t1", "from_app": "blender",
                    "files": [os.path.join(td, "a.obj")],
                    "asset_name": "Cube"}
            Path(os.path.join(inbox, "t1.json")).write_text(
                json.dumps(task), encoding="utf-8")
            # 干扰项：回执/非 json/无 id
            Path(os.path.join(inbox, "t1.done.json")).write_text("{}", encoding="utf-8")
            Path(os.path.join(inbox, "notes.txt")).write_text("x", encoding="utf-8")
            Path(os.path.join(inbox, "bad.json")).write_text('{"no_id": 1}', encoding="utf-8")

            tasks, _skipped = PLUGIN.scan_inbox(inbox)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0][1]["transfer_id"], "t1")

            rp = PLUGIN.write_receipt(inbox, "t1", "ok", ["a.obj"])
            self.assertTrue(os.path.isfile(rp))
            receipt = json.loads(Path(rp).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "ok")

            self.assertTrue(PLUGIN.consume(os.path.join(inbox, "t1.json")))
            self.assertFalse(os.path.isfile(os.path.join(inbox, "t1.json")))
            # 回执由守护进程消费
            self.assertTrue(PLUGIN.consume(rp))

    def test_scan_inbox_missing_dir(self):
        self.assertEqual(PLUGIN.scan_inbox("")[0], [])
        self.assertEqual(PLUGIN.scan_inbox("/nonexistent/xyz")[0], [])


class TestTransferBody(unittest.TestCase):
    def test_body_shape(self):
        body = PLUGIN.transfer_body("Cube", "unreal",
                                    options=PLUGIN.build_send_options("Cube", "cube.obj", "2026.2.1"))
        self.assertEqual(body["kind"], "models")
        self.assertEqual(body["format"], "obj")
        self.assertEqual(body["to"]["app"], "unreal")
        self.assertEqual(body["from"]["app"], "zbrush")
        self.assertEqual(body["options"]["editor"], "zbrush")
        self.assertEqual(body["options"]["zbrush_version"], "2026.2.1")


class TestPublishFlow(unittest.TestCase):
    def test_publish_success(self):
        calls = {"post": 0, "put": 0, "publish": 0}
        orig_json, orig_put = PLUGIN.http_json, PLUGIN.http_put_file

        def fake_json(method, url, body=None, timeout=15.0):
            if url.endswith("/transfers") and method == "POST":
                calls["post"] += 1
                return {"transfer": {"id": "tr_1"}}
            if url.endswith("/publish"):
                calls["publish"] += 1
                return {}
            return {}

        def fake_put(url, path, timeout=300.0):
            calls["put"] += 1
            return {}

        PLUGIN.http_json, PLUGIN.http_put_file = fake_json, fake_put
        try:
            with tempfile.TemporaryDirectory() as td:
                obj = make_obj(os.path.join(td, "cube.obj"))
                cfg = dict(PLUGIN.DEFAULT_CONFIG)
                ok, tid, msg = PLUGIN.publish_transfer(
                    cfg, PLUGIN.transfer_body("Cube", "blender"), [obj])
                self.assertTrue(ok)
                self.assertEqual(tid, "tr_1")
                self.assertEqual(calls, {"post": 1, "put": 1, "publish": 1})
        finally:
            PLUGIN.http_json, PLUGIN.http_put_file = orig_json, orig_put

    def test_publish_upload_failure(self):
        orig_json, orig_put = PLUGIN.http_json, PLUGIN.http_put_file
        PLUGIN.http_json = lambda m, u, b=None, t=15.0: {"transfer": {"id": "tr_2"}}

        def boom(url, path, timeout=300.0):
            raise OSError("disk full")

        PLUGIN.http_put_file = boom
        try:
            with tempfile.TemporaryDirectory() as td:
                obj = make_obj(os.path.join(td, "cube.obj"))
                ok, tid, msg = PLUGIN.publish_transfer(
                    dict(PLUGIN.DEFAULT_CONFIG), PLUGIN.transfer_body("Cube", "blender"), [obj])
                self.assertFalse(ok)
                self.assertEqual(tid, "tr_2")
                self.assertIn("上传失败", msg)
        finally:
            PLUGIN.http_json, PLUGIN.http_put_file = orig_json, orig_put


class TestOutboxScan(unittest.TestCase):
    def test_stable_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            outbox = os.path.join(td, "zbrush_outbox")
            os.makedirs(outbox)
            old = os.path.join(outbox, "old.obj")
            make_obj(old)
            os.utime(old, (time.time() - 60, time.time() - 60))
            fresh = os.path.join(outbox, "fresh.obj")
            make_obj(fresh)
            Path(os.path.join(outbox, "note.txt")).write_text("x", encoding="utf-8")
            got = [os.path.basename(p) for p in PLUGIN.scan_outbox(outbox)]
            self.assertEqual(got, ["old.obj"])


class TestDaemonZBrush(unittest.TestCase):
    def test_dirs_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            inbox, outbox, hb = DAEMON.zb_dirs(td)
            self.assertTrue(inbox.endswith(DAEMON.ZB_INBOX))
            self.assertTrue(outbox.endswith(DAEMON.ZB_OUTBOX))
            self.assertTrue(hb.endswith(DAEMON.ZB_HEARTBEAT))
            self.assertFalse(DAEMON.zb_bridge_alive(td))
            os.makedirs(inbox)
            Path(hb).write_text("alive", encoding="utf-8")
            self.assertTrue(DAEMON.zb_bridge_alive(td))
            os.utime(hb, (time.time() - 999, time.time() - 999))
            self.assertFalse(DAEMON.zb_bridge_alive(td))

    def test_task_and_receipt_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            record = {"id": "tr_9", "asset_name": "Cube",
                      "from": {"app": "blender"}}
            task = DAEMON.zb_write_task(td, record, [os.path.join(td, "a.obj")])
            self.assertTrue(os.path.isfile(task))
            data = json.loads(Path(task).read_text(encoding="utf-8"))
            self.assertEqual(data["transfer_id"], "tr_9")
            self.assertEqual(data["from_app"], "blender")

            self.assertIsNone(DAEMON.zb_take_receipt(td, "tr_9"))
            PLUGIN.write_receipt(os.path.join(td, DAEMON.ZB_INBOX),
                                 "tr_9", "ok", ["a.obj"])
            receipt = DAEMON.zb_take_receipt(td, "tr_9")
            self.assertEqual(receipt["status"], "ok")
            # 消费后即删
            self.assertFalse(os.path.isfile(
                os.path.join(td, DAEMON.ZB_INBOX, "tr_9.done.json")))

    def test_notify_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = DAEMON.zb_write_notify(Path(td), {"id": "tr_1", "asset_name": "C"},
                                       [os.path.join(td, "a.obj")])
            self.assertTrue(os.path.isfile(p))
            obj = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(obj["transfer_id"], "tr_1")
            self.assertTrue(obj["path"].endswith("a.obj"))

    def test_one_shot_args_parsing(self):
        # -script 之后：脚本路径占一位，真正的参数从 +2 开始
        argv = ["ZBrush.exe", "-script", "one_shot.py", "a.obj", "b.obj",
                "receipts", "tr_5"]
        args = argv[argv.index("-script") + 2:]
        self.assertEqual(args[-2:], ["receipts", "tr_5"])
        self.assertEqual(args[:-2], ["a.obj", "b.obj"])


class TestImportPending(unittest.TestCase):
    """主线程导入：有界、对话框探针、回执与消费。用假 zbc 注入，不依赖 ZBrush。"""

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

        def add_subpalette(self, *a, **k):
            self.calls.append(("add_subpalette",) + a)

        def add_button(self, *a, **k):
            self.calls.append(("add_button",) + a)

    def setUp(self):
        self._orig_zbc = PLUGIN._zbc
        self.td = tempfile.mkdtemp()
        self.inbox = os.path.join(self.td, "zbrush_inbox")
        os.makedirs(self.inbox)
        self.cfg = dict(PLUGIN.DEFAULT_CONFIG)
        self.cfg.update({"payload_dir": self.td, "inbox_dir": self.inbox,
                         "outbox_dir": os.path.join(self.td, "zbrush_outbox"),
                         "import_wait_sec": 3.0})
        # 各用例独立运行：清掉追加的全局运行态，避免用例间互相污染
        # （autostart / start_watcher / auto_loop 会改这些字段）
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None
        PLUGIN._STATE["watcher"] = False

    def tearDown(self):
        PLUGIN._zbc = self._orig_zbc
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None
        PLUGIN._STATE["watcher"] = False

    def _task(self, tid="t1", files=("a.obj",)):
        for f in files:
            make_obj(os.path.join(self.td, f))
        task = {"transfer_id": tid, "from_app": "blender",
                "files": [os.path.join(self.td, f) for f in files]}
        Path(os.path.join(self.inbox, tid + ".json")).write_text(
            json.dumps(task), encoding="utf-8")

    def test_import_happy_path(self):
        self._task()
        fake = self.FakeZbc(tool_after="a")
        PLUGIN._zbc = lambda: fake
        n = PLUGIN.import_pending(self.cfg)
        self.assertEqual(n, 1)
        self.assertIn(("press", "Tool:Import"), fake.calls)
        # 2026-10-18 终局：回执与消费过的任务文件都归集到 inbox/done/，不在顶层堆积
        rp = os.path.join(PLUGIN.done_dir(self.inbox), "t1.done.json")
        self.assertTrue(os.path.isfile(rp))
        receipt = json.loads(Path(rp).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["imported"], ["a.obj"])
        # 顶层不再残留历史垃圾（修复 ZBrush 扫描卡死的根因）
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t1.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.inbox, "t1.done.json")))
        # 消费过的任务文件进入 done/，可追溯
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN.done_dir(self.inbox), "t1.json")))

    def test_import_proceeds_without_dialog_prescreen(self):
        """2026-10-16 用户实机纠正：has_next_filename() 常态 True（默认就有「下一个
        文件名」），原「对话框挡路」预检会误杀全部导入（每次进来都是 True -> 永远 abort）。
        现删除预检，改为直接调导入 API + 异常捕获：has_next_filename 为 True 时
        也照常导入，不再因为常真值 abort。"""
        self._task()
        fake = self.FakeZbc(dialog=True)   # has_next_filename() -> True
        PLUGIN._zbc = lambda: fake
        n = PLUGIN.import_pending(self.cfg)
        self.assertEqual(n, 1)
        self.assertIn(("press", "Tool:Import"), fake.calls)   # 不再跳过
        receipt = json.loads(Path(os.path.join(PLUGIN.done_dir(self.inbox), "t1.done.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["imported"], ["a.obj"])

    def test_press_exception_is_caught(self):
        self._task()
        fake = self.FakeZbc(raise_on_press=True)
        PLUGIN._zbc = lambda: fake
        PLUGIN.import_pending(self.cfg)   # 不能抛
        receipt = json.loads(Path(os.path.join(PLUGIN.done_dir(self.inbox), "t1.done.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "error")

    def test_import_wait_is_bounded(self):
        """工具名永远不匹配也不能挂死：import_wait_sec 内必须返回。"""
        self._task()
        fake = self.FakeZbc(tool_after="something_else")
        PLUGIN._zbc = lambda: fake
        t0 = time.time()
        PLUGIN.import_pending(self.cfg)
        self.assertLess(time.time() - t0, 10.0)

    def test_watcher_is_pure_io_and_detects_pending(self):
        """观察线程：不起 zbc、能发现待收任务并写状态文件。"""
        self._task(tid="t2")
        PLUGIN._zbc = lambda: (_ for _ in ()).throw(
            AssertionError("观察线程绝不调用 zbc"))
        started = PLUGIN.start_watcher(self.cfg)
        self.assertTrue(started)
        deadline = time.time() + 6
        while time.time() < deadline:
            if os.path.isfile(PLUGIN.heartbeat_path(self.cfg)):
                break
            time.sleep(0.2)
        self.assertTrue(os.path.isfile(PLUGIN.heartbeat_path(self.cfg)))
        time.sleep(2.5)   # 等观察线程扫到任务
        st = json.loads(Path(PLUGIN.status_path(self.cfg)).read_text(encoding="utf-8"))
        self.assertGreaterEqual(st["pending"], 1)
        # 幂等：重复启动只起一个
        self.assertFalse(PLUGIN.start_watcher(self.cfg))

    def test_auto_loop_exits_after_one_iteration(self):
        """自动导入（实验）：update 回调里置停止位 -> 循环必须退出，不能卡死。"""
        fake = self.FakeZbc(tool_after="a")

        def _update(n=1, redraw=False):
            fake.calls.append(("update", n, redraw))
            PLUGIN._STATE["auto"] = False      # 第一圈结束即停

        fake.update = _update
        PLUGIN._zbc = lambda: fake
        PLUGIN._STATE["auto"] = False
        t0 = time.time()
        PLUGIN.auto_loop(self.cfg)             # 必须自行返回
        self.assertLess(time.time() - t0, 10.0)
        self.assertFalse(PLUGIN._STATE["auto"])

    def test_toggle_auto_stop_path(self):
        PLUGIN._zbc = lambda: self.FakeZbc()
        PLUGIN._STATE["auto"] = True
        msg = PLUGIN.toggle_auto(self.cfg)
        self.assertEqual(msg, "自动导入：正在停止…")
        self.assertFalse(PLUGIN._STATE["auto"])

    def test_autostart_zero_block_and_watcher_p0_2(self):
        """P0-2 复核：autostart 必须零阻塞返回（不拉后台 auto_loop 线程）。

        后台线程调 zbc 在 ZBrush 真机无效（嵌入 VM 把 GIL 锁在主线程，
        Maxon ZBrush 2026 Python SDK 明确 Main thread only），故 auto_loop
        只在用户点「自动导入」按钮（主线程 toggle_auto）时运行。autostart
        只建 UI + 起观察线程（纯 IO，不碰 zbc），立即返回；观察线程负责
        心跳与 alive 判定。诊断日志打印插件心跳/状态目录三行（便于实机核对
        alive=false 根因）。
        """
        fake = self.FakeZbc(tool_after="a")
        PLUGIN._zbc = lambda: fake
        # 写一份临时 ab_config.json（payload_dir 非空，心跳/状态目录可解析）
        cfg_path = os.path.join(self.td, "ab_config.json")
        Path(cfg_path).write_text(json.dumps({
            "payload_dir": self.td,
            "auto_max_sec": 3.0,
            "poll_interval": 0.2,
            "import_wait_sec": 1.0,
        }), encoding="utf-8")
        # 模拟全新启动：清掉全局 auto / auto_thread 状态
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None
        t0 = time.time()
        PLUGIN.autostart(cfg_path)               # 必须零阻塞立即返回
        self.assertLess(time.time() - t0, 5.0,
                        "autostart 不应阻塞 ZBrush 启动")
        # 零阻塞 + 不在后台自动起 auto_loop：_STATE["auto"] 仍为 False
        self.assertFalse(PLUGIN._STATE["auto"],
                         "autostart 不应在后台自动起 auto_loop（非主线程调 zbc 无效）")
        self.assertIsNone(PLUGIN._STATE["auto_thread"],
                          "不应存在 auto_thread（后台循环线程已移除）")
        # 观察线程（纯 IO）应在 autostart 内起：心跳文件出现
        deadline = time.time() + 6
        while time.time() < deadline and not os.path.isfile(
                PLUGIN.heartbeat_path(self.cfg)):
            time.sleep(0.2)
        self.assertTrue(os.path.isfile(PLUGIN.heartbeat_path(self.cfg)),
                        "观察线程未写出心跳（autostart 未起 watcher）")
        # 诊断路径打印（P0-2 附赠）：payload_dir / heartbeat / status 三类齐全
        dl = os.path.join(self.td, "zb_bridge_diag.log")
        ddl = time.time() + 6
        while time.time() < ddl and not os.path.isfile(dl):
            time.sleep(0.1)
        self.assertTrue(os.path.isfile(dl), "诊断日志未生成")
        diag_txt = Path(dl).read_text(encoding="utf-8")
        self.assertIn("autostart diag payload_dir={0}".format(self.td), diag_txt)
        self.assertIn("autostart diag heartbeat={0}".format(
            PLUGIN.heartbeat_path(self.cfg)), diag_txt)
        self.assertIn("autostart diag status={0}".format(
            PLUGIN.status_path(self.cfg)), diag_txt)
        # 主线程兜底仍在：toggle_auto 是 auto_loop 的合法主线程入口（用极小
        # auto_max_sec 让循环快速自停，避免阻塞测试；FakeZbc 不依赖真机）
        self.cfg["auto_max_sec"] = 0.5
        self.cfg["poll_interval"] = 0.1
        msg = PLUGIN.toggle_auto(self.cfg)   # 主线程跑一轮后自动停
        self.assertIn("自动导入：已停止", msg)
        self.assertFalse(PLUGIN._STATE["auto"], "toggle_auto 结束后未复位 auto")
        # 收尾：清 auto_thread 防污染后续用例
        PLUGIN._STATE["auto"] = False
        PLUGIN._STATE["auto_thread"] = None


class TestModuleHygiene(unittest.TestCase):
    def test_plugin_has_no_zbrush_import_at_top(self):
        """顶层没有 import zbrush（懒加载在 _zbc() 里）——用 AST 查，别被 docstring 骗。"""
        import ast
        src = (ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" /
               "asset_bridge_zbrush.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        top_imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                top_imports.append(node.module or "")
        self.assertNotIn("zbrush.commands", top_imports)
        self.assertNotIn("zbrush", top_imports)

    def test_daemon_app_labels(self):
        self.assertIn("zbrush", DAEMON.APP_LABELS)
        self.assertIn("zbrush", DAEMON.DEFAULT_APPS)
        self.assertIn(".obj", DAEMON.MODEL_EXTS)
        self.assertTrue(DAEMON.ZB_ONE_SHOT.name == "one_shot_import.py")


class TestExportPostProcess(unittest.TestCase):
    """2026-10-17 修正（用户真机：ZBrush->UE 崩溃 / 模型极小）的纯几何验证。
    不依赖 ZBrush 本体，直接验证 _postprocess_obj 的三角化 / UV 容错 / 缩放。"""

    def _write_obj(self, path, text):
        Path(path).write_text(text, encoding="utf-8")
        return path

    def _parse_faces(self, text):
        faces = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("f "):
                toks = s.split()[1:]
                faces.append(toks)
        return faces

    def _count_vt(self, text):
        return sum(1 for line in text.splitlines()
                   if line.strip().startswith("vt "))

    def _bbox_maxext(self, text):
        verts = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("v "):
                p = s.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
        lo = [min(v[i] for v in verts) for i in range(3)]
        hi = [max(v[i] for v in verts) for i in range(3)]
        return max(hi[i] - lo[i] for i in range(3))

    def test_triangulate_quad_and_uv_fix_and_normalize(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.obj")
            # 极小模型（真机「几毫米」复现）：max extent=0.001；含一个 quad（带 UV）
            # 与一个无 UV 的三角面（触发 UV 容错补齐）
            obj = "\n".join([
                "v 0.001 0 0", "v 0.001 0.001 0", "v 0.002 0.001 0",
                "v 0.002 0 0",
                "vt 0 0", "vt 1 0", "vt 1 1", "vt 0 1",
                "f 1/1 2/2 3/3 4/4",
                "v 0.001 0 0.001", "v 0.001 0.001 0.001", "v 0.002 0.001 0.001",
                "f 5 6 7",
            ]) + "\n"
            self._write_obj(p, obj)

            ok = PLUGIN._postprocess_obj(p, {"zbrush_export_scale": 1.0,
                                             "zbrush_export_normalize_cm": 100.0})
            self.assertTrue(ok, "postprocess 应成功")
            out = Path(p).read_text(encoding="utf-8")

            faces = self._parse_faces(out)
            # 1 quad -> 2 三角 + 1 三角 = 3 个三角面
            self.assertEqual(len(faces), 3, "quad 应被扇三角化为 2 个三角")
            for f in faces:
                self.assertEqual(len(f), 3, "每个面必须恰好 3 个顶点")
                for tok in f:
                    self.assertNotIn("//", tok, "不应残留无 UV 的 v//vn 写法")
                    if "/" in tok:
                        vi, vti = tok.split("/")
                        self.assertTrue(int(vti) >= 1, "UV 索引必须 >=1")
            # UV 容错：原本无 UV 的 face(5 6 7) 现在应有合法 vt 引用
            self.assertIn("5/", faces[2][0], "无 UV 的 face 应被补齐合法 UV")
            # 缩放归一化：max extent 应 ~100cm（归一化上限）
            self.assertAlmostEqual(self._bbox_maxext(out), 100.0, delta=1.0)
            # 所有 vt 引用都在范围内
            nvt = self._count_vt(out)
            for f in faces:
                for tok in f:
                    if "/" in tok:
                        self.assertLessEqual(int(tok.split("/")[1]), nvt)

    def test_sane_size_not_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.obj")
            # 关闭归一化（normalize_cm=0）：原生尺寸应被原样保留（opt-out 路径）
            obj = "\n".join([
                "v -25 0 0", "v 25 0 0", "v 25 50 0", "v -25 50 0",
                "vt 0 0", "vt 1 0", "vt 1 1", "vt 0 1",
                "f 1/1 2/2 3/3 4/4",
            ]) + "\n"
            self._write_obj(p, obj)
            PLUGIN._postprocess_obj(p, {"zbrush_export_scale": 1.0,
                                        "zbrush_export_normalize_cm": 0.0})
            out = Path(p).read_text(encoding="utf-8")
            # 尺寸应保持 50（归一化关闭，不被改动）
            self.assertAlmostEqual(self._bbox_maxext(out), 50.0, delta=0.5)

    def test_small_model_normalized_to_cm(self):
        # 2026-10-18 修复（用户真机：真实模型导出仅 2 单位，旧阈值 <1.0 漏判 -> 极小）：
        # ZBrush 无量纲，应统一归一化到 zbrush_export_normalize_cm（默认 100cm）。
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.obj")
            # 2 单位立方（真机真实尺寸），应被放大到 100cm
            obj = "\n".join([
                "v -1 -1 -1", "v 1 -1 -1", "v 1 1 -1", "v -1 1 -1",
                "v -1 -1 1", "v 1 -1 1", "v 1 1 1", "v -1 1 1",
                "f 1 2 3", "f 1 3 4", "f 5 6 7", "f 5 7 8",
            ]) + "\n"
            self._write_obj(p, obj)
            PLUGIN._postprocess_obj(p, {"zbrush_export_scale": 1.0,
                                        "zbrush_export_normalize_cm": 100.0})
            out = Path(p).read_text(encoding="utf-8")
            # 2 单位 -> 100cm（scale=50）
            self.assertAlmostEqual(self._bbox_maxext(out), 100.0, delta=1.0)

    def _parse_verts(self, text):
        verts = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("v "):
                p = s.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
        return verts

    def test_rotation_rx90_maps_y_up_to_z_up(self):
        # 2026-10-17 修正（用户真机：ZBrush->UE 需手动 Roll=90 才正向）：
        # 默认 zbrush_export_rotation={x:90} 应把 +Y 轴顶点转到 +Z（ZBrush Y-up -> UE Z-up）。
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.obj")
            # 单三角：v1=(0,1,0) 指 +Y；关闭归一化/缩放以免干扰旋转判定
            obj = "\n".join([
                "v 0 1 0", "v 1 0 0", "v 0 0 0",
                "f 1 2 3",
            ]) + "\n"
            self._write_obj(p, obj)
            ok = PLUGIN._postprocess_obj(p, {
                "zbrush_export_scale": 1.0,
                "zbrush_export_normalize_cm": 0,
                "zbrush_export_rotation": {"x": 90.0, "y": 0.0, "z": 0.0},
            })
            self.assertTrue(ok)
            verts = self._parse_verts(Path(p).read_text(encoding="utf-8"))
            # 原 (0,1,0) 经 Rx(+90) 应变为 (0,0,1)
            self.assertAlmostEqual(verts[0][0], 0.0, places=5)
            self.assertAlmostEqual(verts[0][1], 0.0, places=5)
            self.assertAlmostEqual(verts[0][2], 1.0, places=5)

    def test_rotation_disabled_leaves_verts_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.obj")
            obj = "\n".join([
                "v 0 1 0", "v 1 0 0", "v 0 0 0",
                "f 1 2 3",
            ]) + "\n"
            self._write_obj(p, obj)
            PLUGIN._postprocess_obj(p, {
                "zbrush_export_scale": 1.0,
                "zbrush_export_normalize_cm": 0,
                "zbrush_export_rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
            })
            verts = self._parse_verts(Path(p).read_text(encoding="utf-8"))
            self.assertAlmostEqual(verts[0][1], 1.0, places=5)  # 未被旋转
            self.assertAlmostEqual(verts[0][2], 0.0, places=5)

    def test_send_current_clean_asset_name_not_path(self):
        # 2026-10-17 修正（用户真机：回传名变成 payload 路径）：
        # get_active_tool_path() 在 Windows 上返回反斜杠完整路径时，send_current
        # 必须拿干净 basename（SM_Cube），而非整条路径。
        captured = {}
        captured_files = {}

        def fake_export(obj, cfg=None):
            Path(obj).write_text(
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            return True, ""

        def fake_publish(cfg, body, files, log=None):
            captured["body"] = body
            captured_files["files"] = list(files)
            return True, "tid-xyz", "ok"

        orig_export = PLUGIN.export_current
        orig_publish = PLUGIN.publish_transfer
        orig_tool = PLUGIN.current_tool_name
        PLUGIN.export_current = fake_export
        PLUGIN.publish_transfer = fake_publish
        PLUGIN.current_tool_name = lambda: (
            r"E:\CirnoWorkSpace\AssetBridge\runtime\payloads"
            r"\9dda84d50ba9416d84f2fe68ddd81637\SM_Cube")
        try:
            cfg = {"server_url": "http://localhost:1", "default_to": "unreal",
                   "payload_dir": tempfile.mkdtemp()}
            ok, msg = PLUGIN.send_current(cfg, "unreal")
            self.assertTrue(ok, msg)
            self.assertEqual(captured["body"]["asset_name"], "SM_Cube")
            self.assertTrue(captured_files["files"][0].endswith("SM_Cube.obj"),
                            captured_files["files"])
        finally:
            PLUGIN.export_current = orig_export
            PLUGIN.publish_transfer = orig_publish
            PLUGIN.current_tool_name = orig_tool


if __name__ == "__main__":
    unittest.main(verbosity=2)
