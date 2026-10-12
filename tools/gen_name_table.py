# -*- coding: utf-8 -*-
"""
生成 bridge_pinyin.dat（拼音表）并把 bridge_names.py / bridge_pinyin.dat
分发到中转服务与各 DCC 插件目录。

依赖：pypinyin（**仅生成时需要**，产物运行时不依赖任何第三方库）。
    python -m pip install pypinyin
运行：
    python tools/gen_name_table.py
"""
import os
import shutil

from pypinyin import Style, pinyin

BASE, TOP = 0x4E00, 0x9FFF
HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# bridge_names.py / bridge_pinyin.dat 需要落到哪些目录（存在才拷）
TARGETS = [
    os.path.join(PROJ, "server"),
    os.path.join(PROJ, "blender", "addon", "asset_bridge_blender"),
    os.path.join(PROJ, "maya", "plugin", "asset_bridge_maya"),
    os.path.join(PROJ, "unreal", "plugin", "asset_bridge_unreal"),
    # UE 实际加载的 Python 目录（工程内 Content/Python）
    os.path.join(PROJ, "unreal", "project", "AssetBridgeTest", "Content", "Python",
                 "asset_bridge_unreal"),
]


def build_rows():
    rows = []
    for cp in range(BASE, TOP + 1):
        ch = chr(cp)
        syl = ""
        try:
            res = pinyin(ch, style=Style.NORMAL, errors="ignore")
            if res and res[0]:
                syl = res[0][0]
        except Exception:
            syl = ""
        syl = "".join(c for c in syl.lower() if "a" <= c <= "z")
        rows.append(syl if syl else ".")
    return rows


def main():
    rows = build_rows()
    dat = os.path.join(HERE, "bridge_pinyin.dat")
    with open(dat, "w") as fh:
        fh.write("\n".join(rows))
    print("pinyin table rows: %d  (%.1f KB)" % (len(rows), os.path.getsize(dat) / 1024.0))

    src_py = os.path.join(HERE, "bridge_names.py")
    # 自检
    import sys
    sys.path.insert(0, HERE)
    import bridge_names
    for t in ("木头方块_01", "SM_椅子", "SM_Cube", "无标题"):
        print("  check %-12s -> %s" % (t, bridge_names.to_ascii(t)))

    for t in TARGETS:
        if not os.path.isdir(t):
            print("  skip (missing): %s" % t)
            continue
        shutil.copy2(src_py, os.path.join(t, "bridge_names.py"))
        shutil.copy2(dat, os.path.join(t, "bridge_pinyin.dat"))
        print("  deployed -> %s" % t)


if __name__ == "__main__":
    main()
