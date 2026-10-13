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

            tasks = PLUGIN.scan_inbox(inbox)
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
        self.assertEqual(PLUGIN.scan_inbox(""), [])
        self.assertEqual(PLUGIN.scan_inbox("/nonexistent/xyz"), [])


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


class TestModuleHygiene(unittest.TestCase):
    def test_plugin_has_no_zbrush_import_at_top(self):
        src = (ROOT / "plugins" / "zbrush" / "asset_bridge_zbrush" /
               "asset_bridge_zbrush.py").read_text(encoding="utf-8")
        self.assertNotIn("import zbrush.commands", src.split("def _zbc")[0])

    def test_daemon_app_labels(self):
        self.assertIn("zbrush", DAEMON.APP_LABELS)
        self.assertIn("zbrush", DAEMON.DEFAULT_APPS)
        self.assertIn(".obj", DAEMON.MODEL_EXTS)
        self.assertTrue(DAEMON.ZB_ONE_SHOT.name == "one_shot_import.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
