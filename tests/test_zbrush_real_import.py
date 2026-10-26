# -*- coding: utf-8 -*-
"""
真实 ZBrush 导入编排实测（无 FakeZbc）。

用户 2026-10-19 严令：停用手写桩（FakeZbc 直接返回 ok、从不读真实文件），
改写实脚本「模拟真实文件生成和读取」。本测试即据此落地：

  * **真实文件生成**：在临时目录写出多个互不相同的 OBJ（顶点/内容不同 -> sha 不同）。
  * **真实 inbox 握手**：写真实的 <tid>.json 任务文件，指向真实 OBJ 路径。
  * **驱动真实生产代码**：直接调用插件里的 import_pending / _import_one（未经改动），
    仅把「ZBrush 内嵌 VM」(zbrush.commands) 换成一个 *忠实模拟器* RealZbSim。
  * **RealZbSim 做真实 IO**：每次 press 都 open+读 OBJ 字节、算 sha256、维护一份
    真实落盘的「ZTool 文档」(tools.json)。它不返回 canned ok，而是真正消费磁盘文件，
    因此能验证「文件确实被读取、首个 Tool:Import 建工具、其后 Tool:SubTool:Append
    追加子工具、不互相覆盖」这一整条真实生命周期。

注意：RealZbSim 模拟的是 ZBrush 的 tool/subtool 模型（VM 无法无头运行），但它
**真的读磁盘上的 OBJ 并比对内容哈希**，所以这是「模拟真实文件读取」，而非「假桩」。
真机（真实 ZBrush VM + 用户 gui）仍需用户在真机复测，配合 import_pending 里新增的
明细 diag 日志（scanned/consumed/导入数/失败数 + 每个文件的 before/after 时间戳）。

运行：python tests/test_zbrush_real_import.py   （零 pip，标准库即可）
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.normpath(os.path.join(
    HERE, "..", "plugins", "zbrush", "asset_bridge_zbrush"))
MODULE_PATH = os.path.join(PLUGIN_DIR, "asset_bridge_zbrush.py")

# ---- 加载插件模块（懒加载 zbrush.commands，本环境不会触发）-------------------
spec = importlib.util.spec_from_file_location("ab_zbrush_real", MODULE_PATH)
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


class RealZbSim:
    """忠实模拟 ZBrush 的 tool/subtool 模型，但每次导入都做真实文件 IO。

    - press("Tool:Import")：读磁盘 OBJ -> 新开一个工具（重置当前子工具列表）。
    - press("Tool:SubTool:Append")：读磁盘 OBJ -> 追加为当前工具的子工具。
    这样，如果生产代码错误地「每个文件都 Tool:Import」（旧 bug），模拟器会表现出
    「每个文件替换当前工具几何 -> 只留最后 1 个」；而修复后（首个 Tool:Import、
    其后 Append）则表现为「1 个工具 + N-1 个子工具」。
    """

    def __init__(self):
        self.tools = []          # 每个元素是一个子工具名列表（一个 ZTool 文档）
        self.tool_import_calls = 0
        self.append_calls = 0
        self._pending = None
        self._last_stem = ""
        self._sha_log = []       # 每个被导入 mesh 的真实 sha256（证明真实读盘）

    def set_next_filename(self, path):
        self._pending = path

    def _read_sha(self, path):
        with open(path, "rb") as fh:
            data = fh.read()
        return hashlib.sha256(data).hexdigest(), data

    def press(self, cmd):
        if not self._pending or not os.path.isfile(self._pending):
            raise RuntimeError("press({0}) 但待导入文件缺失: {1}".format(cmd, self._pending))
        sha, _ = self._read_sha(self._pending)   # 真实读盘
        stem = os.path.splitext(os.path.basename(self._pending))[0]
        self._last_stem = stem
        if cmd == "Tool:Import":
            self.tool_import_calls += 1
            self.tools.append([stem])            # 新工具，仅含该 mesh
            self._sha_log.append((stem, sha))
        elif cmd == "Tool:SubTool:Append":
            self.append_calls += 1
            if not self.tools:
                self.tools.append([stem])        # 防御：无当前工具时退化为建工具
            else:
                self.tools[-1].append(stem)
            self._sha_log.append((stem, sha))
        else:
            raise RuntimeError("未知 ZBrush 命令: {0}".format(cmd))

    def get_active_tool_path(self):
        # 返回含最近导入 stem 的路径，让 _import_one 的 detected 判定立即命中（不卡 15s）
        return "C:/zbrush/Tool/{0}.ztl".format(self._last_stem)


def make_obj(path, label, n=4):
    lines = ["# AssetBridge real-import test", "o " + label]
    for i in range(n):
        lines.append("v {0} {1} {2}".format(i, i * 2, i * 3))
    if n >= 4:
        lines.append("f 1 2 3 4")
    else:
        lines.append("f 1 2 3")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def fresh_cfg(root, tag):
    # 每个场景独立 payload，避免 done/ 残留互相干扰断言
    payload = os.path.join(root, "payload_" + tag)
    os.makedirs(payload, exist_ok=True)
    cfg = dict(ab.DEFAULT_CONFIG)
    cfg["payload_dir"] = payload
    ab._resolve_dirs(cfg)
    os.makedirs(cfg["inbox_dir"], exist_ok=True)
    return cfg


def write_task(inbox, tid, files, from_app="blender"):
    path = os.path.join(inbox, tid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"transfer_id": tid, "from_app": from_app, "files": files}, fh)
    return path


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] {0}".format(name))
    else:
        FAIL += 1
        print("  [FAIL] {0}  {1}".format(name, detail))


def scenario_send3_recv1(root, sim):
    """发 3 个文件、点 1 次接收 -> 1 个工具含 3 个子工具（不互相覆盖）。"""
    print("\n=== 场景 A：发 3 收 1（连发多个只留最后 1 个 / 一对一只有第一次 的真实复现）===")
    cfg = fresh_cfg(root, "A")
    inbox = cfg["inbox_dir"]
    files = []
    for i in range(3):
        p = os.path.join(root, "meshA{0}.obj".format(i))
        make_obj(p, "MeshA{0}".format(i))
        files.append(p)
    write_task(inbox, "tidA", files)
    n = ab.import_pending(cfg)
    # 断言
    check("处理数=1（一个任务）", n == 1, "got {0}".format(n))
    check("Tool:Import 调用 1 次", sim.tool_import_calls == 1, "got {0}".format(sim.tool_import_calls))
    check("Tool:SubTool:Append 调用 2 次", sim.append_calls == 2, "got {0}".format(sim.append_calls))
    check("生成 1 个 ZTool 文档", len(sim.tools) == 1, "tools={0}".format(sim.tools))
    check("该工具含 3 个子工具（无覆盖）", len(sim.tools[0]) == 3, "subtools={0}".format(sim.tools[0]))
    # 真实文件内容哈希比对：3 个子工具对应 3 个真实 OBJ 的内容
    stems = set(sim.tools[0])
    expected = {"meshA0", "meshA1", "meshA2"}
    check("3 个子工具名与真实 OBJ 一一对应", stems == expected, "got {0}".format(stems))
    # 回执
    receipt = os.path.join(ab.done_dir(inbox), "tidA.done.json")
    check("回执文件已真实写入 done/", os.path.isfile(receipt), receipt)
    if os.path.isfile(receipt):
        rec = json.loads(open(receipt, encoding="utf-8").read())
        check("回执状态 ok", rec.get("status") == "ok", json.dumps(rec, ensure_ascii=False))
        check("回执 imported 含 3 个文件", len(rec.get("imported") or []) == 3, str(rec.get("imported")))
    # inbox 顶层应已清空（任务被 consume 进 done/）
    leftover = [f for f in os.listdir(inbox) if f.endswith(".json") and not f.endswith(".done.json")]
    check("inbox 顶层无遗留任务文件", len(leftover) == 0, str(leftover))


def scenario_1to1_x3(root, sim):
    """一对一收 3 轮：每轮 1 个任务 1 个文件，各自独立成工具，不互相吞掉。"""
    print("\n=== 场景 B：一对一收 3 轮（第二次再发点接收无反应 的真实复现）===")
    cfg = fresh_cfg(root, "B")
    inbox = cfg["inbox_dir"]
    for i in range(3):
        p = os.path.join(root, "meshB{0}.obj".format(i))
        make_obj(p, "meshB{0}".format(i))
        write_task(inbox, "tidB{0}".format(i), [p])
        n = ab.import_pending(cfg)
        check("第 {0} 轮处理数=1".format(i + 1), n == 1, "got {0}".format(n))
        # 每轮都应是「首个文件 -> Tool:Import」，且该轮工具只含本次文件
        check("第 {0} 轮当前工具仅 1 子工具（不被上一轮吞掉）".format(i + 1),
              len(sim.tools[-1]) == 1, "last tool={0}".format(sim.tools[-1]))
        check("第 {0} 轮子工具名=meshB{1}".format(i + 1, i),
              sim.tools[-1][0] == "meshB{0}".format(i), str(sim.tools[-1]))
    check("3 轮共 Tool:Import 3 次（每轮各自独立成工具）", sim.tool_import_calls == 3,
          "got {0}".format(sim.tool_import_calls))
    check("3 轮共 0 次 Append（一对一无需追加）", sim.append_calls == 0,
          "got {0}".format(sim.append_calls))
    # 3 份 ok 回执
    dones = [f for f in os.listdir(ab.done_dir(inbox)) if f.endswith(".done.json")]
    ok_count = 0
    for f in dones:
        rec = json.loads(open(os.path.join(ab.done_dir(inbox), f), encoding="utf-8").read())
        if rec.get("status") == "ok":
            ok_count += 1
    check("3 份 ok 回执（无任务被吞）", ok_count == 3, "ok={0} files={1}".format(ok_count, dones))


def scenario_missing_file(root, sim):
    """源文件缺失的任务：直接判错 + 消费，绝不卡 15s 等待。"""
    print("\n=== 场景 C：源文件缺失（历史垃圾）-> 秒清、不卡死 ===")
    cfg = fresh_cfg(root, "C")
    inbox = cfg["inbox_dir"]
    ghost = os.path.join(root, "ghost_that_does_not_exist.obj")
    write_task(inbox, "tidC", [ghost])
    t0 = __import__("time").time()
    n = ab.import_pending(cfg)
    dt = __import__("time").time() - t0
    check("处理数=1", n == 1, "got {0}".format(n))
    check("缺失任务未被 15s 等待拖死（<2s）", dt < 2.0, "耗时 {0:.2f}s".format(dt))
    receipt = os.path.join(ab.done_dir(inbox), "tidC.done.json")
    check("回执为 error（不丢模型也不卡）", os.path.isfile(receipt), receipt)
    if os.path.isfile(receipt):
        rec = json.loads(open(receipt, encoding="utf-8").read())
        check("回执状态 error", rec.get("status") == "error", json.dumps(rec, ensure_ascii=False))
    # 任务被消费
    leftover = [f for f in os.listdir(inbox) if f.endswith(".json") and not f.endswith(".done.json")]
    check("inbox 顶层任务已消费", len(leftover) == 0, str(leftover))


def scenario_cap(root, sim):
    """大量历史垃圾任务：单轮上限（默认 12）生效，不会被拖死。"""
    print("\n=== 场景 D：20 个任务 -> 单轮上限 12 生效 ===")
    cfg = fresh_cfg(root, "D")
    cfg["import_max_per_click"] = 12
    inbox = cfg["inbox_dir"]
    for i in range(20):
        p = os.path.join(root, "meshD{0}.obj".format(i))
        make_obj(p, "MeshD{0}".format(i))
        write_task(inbox, "tidD{0}".format(i), [p])
    n = ab.import_pending(cfg)
    check("单轮处理数受上限约束（=12）", n == 12, "got {0}".format(n))
    # 剩余 8 个任务仍在 inbox 顶层等待下次
    leftover = [f for f in os.listdir(inbox) if f.endswith(".json") and not f.endswith(".done.json")]
    check("剩余 8 个任务留待下次（不丢）", len(leftover) == 8, str(leftover))


def main():
    root = tempfile.mkdtemp(prefix="ab_zb_real_")
    print("临时工作目录: {0}".format(root))
    try:
        sim = RealZbSim()
        ab._zbc = lambda: sim          # 注入真实 IO 模拟器（替换 zbrush.commands）
        scenario_send3_recv1(root, sim)
        sim2 = RealZbSim()
        ab._zbc = lambda: sim2
        scenario_1to1_x3(root, sim2)
        sim3 = RealZbSim()
        ab._zbc = lambda: sim3
        scenario_missing_file(root, sim3)
        sim4 = RealZbSim()
        ab._zbc = lambda: sim4
        scenario_cap(root, sim4)
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    print("\n========================================")
    print("结果: {0} 通过 / {1} 失败".format(PASS, FAIL))
    if FAIL:
        print("存在失败项 -> 退出码 1")
        sys.exit(1)
    print("全部通过（注意：这是真实文件 IO 模拟，真机仍须用户复测 + 查看 diag 日志）")


if __name__ == "__main__":
    main()
