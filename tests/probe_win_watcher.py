#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机探针：验证 WinDirWatcher 能捕获 PS/SAI2 的【原地覆盖写】。

本环境（沙箱）会 SIGTERM 杀掉阻塞中的 ReadDirectoryChangesW，无法在此实测；
请在**真机 Windows**（装有 PS/SAI2）运行：

    python tests/probe_win_watcher.py

它会：
  1. 在临时目录起一个真实 WinDirWatcher（ReadDirectoryChangesW）；
  2. 模拟 PS/SAI2 保存——对同一文件**原地追加/改写**（不 rename）；
  3. 轮询 get_last_change 看是否被捕获、耗时多少。

预期输出（修复后）：
  [probe] platform=win32
  [probe] 原地覆盖写 -> Watcher 捕获: True, 耗时 0.0xx s
  [probe] FS_WATCH_FILTER=0x19 (FILE_NAME|SIZE|LAST_WRITE)

若捕获为 False，说明 filter 仍有问题（原地写触发的是 ACTION_MODIFIED=LAST_WRITE）。
"""
import os
import sys
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import editor_daemon as mod  # noqa: E402


def main():
    print("[probe] platform={0}".format(sys.platform))
    print("[probe] FS_WATCH_FILTER=0x{0:x}".format(mod.FS_WATCH_FILTER))
    if mod.FS_WATCH_FILTER & 0x10 == 0:
        print("[probe] !! filter 漏了 LAST_WRITE(0x10)，原地写不会被捕获")
        return 1
    if not mod.WinDirWatcher().available:
        print("[probe] WinDirWatcher 不可用（非 win32），本探针仅在 Windows 有意义")
        return 0

    tmp = tempfile.mkdtemp(prefix="wb_watch_probe_")
    target = os.path.join(tmp, "asset.png")
    with open(target, "wb") as f:
        f.write(b"initial")

    w = mod.WinDirWatcher()
    w.start()
    w.add_dir(tmp)
    print("[probe] 监听目录: {0}".format(tmp))

    # 模拟 PS/SAI2 原地保存（关键：不 rename，直接改写同一文件）
    def do_save():
        time.sleep(0.05)
        with open(target, "ab") as f:
            f.write(b"\x00" * 4096)   # 追加 -> mtime/last-write 变化（原地覆盖写）
        # 再写一次确保 last-write 刷新
        with open(target, "r+b") as f:
            f.seek(0)
            f.write(b"PNGD")           # 原地改写开头几个字节

    import threading
    threading.Thread(target=do_save, daemon=True).start()

    t0 = time.monotonic()
    detected = False
    while time.monotonic() - t0 < 3.0:
        last = w.get_last_change(target)
        if last > 0.0:
            detected = True
            break
        time.sleep(0.01)

    elapsed = (time.monotonic() - t0) if detected else -1.0
    print("[probe] 原地覆盖写 -> Watcher 捕获: {0}, 耗时 {1:.3f} s".format(
        detected, elapsed if detected else -1.0))

    w.remove_dir(tmp)
    w.stop()
    try:
        os.remove(target)
        os.rmdir(tmp)
    except Exception:
        pass
    print("[probe] 结论: {0}".format(
        "OK —— watcher 能捕获原地写，settle 走 watcher 分支" if detected
        else "FAIL —— watcher 未捕获原地写（检查 FS_WATCH_FILTER / 编辑器是否走临时文件改名）"))
    return 0 if detected else 2


if __name__ == "__main__":
    sys.exit(main())
