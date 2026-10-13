# -*- coding: utf-8 -*-
"""AssetBridge —— ZBrush 一次性导入脚本（-script 兜底通道）。

用途：ZBrush **未运行**时，守护进程用命令行拉起 ZBrush 执行本脚本：
    ZBrush.exe -script <本脚本.py> <obj1> [obj2 ...] <receipt_dir> <transfer_id>
导入完成后写 <receipt_dir>/<transfer_id>.done.json 回执并退出。

ZBrush 已在运行时不要走这条路（同目录单实例，第二个实例会被拒）——
那种情况由守护进程改走 inbox 任务文件 + 常驻桥接循环。

参数规则（Maxon 官方）：-script 后的第 1 个参数是脚本路径，
真正的脚本参数从 sys.argv.index("-script") + 2 开始。
"""

import json
import os
import sys
import time


def main():
    argv = sys.argv
    if "-script" in argv:
        args = argv[argv.index("-script") + 2:]
    else:
        args = argv[1:]
    if len(args) < 3:
        print("usage: -script one_shot_import.py <files...> <receipt_dir> <transfer_id>")
        sys.exit(2)

    receipt_dir, tid = args[-2], args[-1]
    files = [a for a in args[:-2] if a]

    imported, error = [], ""
    try:
        import zbrush.commands as zbc
        for f in files:
            if not os.path.isfile(f):
                continue
            zbc.set_next_filename(f)
            zbc.press("Tool:Import")
            time.sleep(2.0)          # 给导入留时间（主线程同步）
            imported.append(os.path.basename(f))
    except Exception as exc:
        error = repr(exc)

    receipt = {
        "transfer_id": tid,
        "status": "ok" if (imported and not error) else "error",
        "imported": imported,
        "error": error,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        os.makedirs(receipt_dir, exist_ok=True)
        with open(os.path.join(receipt_dir, tid + ".done.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(receipt, fh, ensure_ascii=False, indent=1)
    except Exception as exc:
        print("写回执失败: {0!r}".format(exc))
        sys.exit(3)
    print("imported={0} error={1}".format(imported, error))
    sys.exit(0 if receipt["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
