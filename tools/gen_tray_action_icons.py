# -*- coding: utf-8 -*-
"""
Asset Bridge - 托盘安装器行内小图标生成器（指定 📁 / 卸载 🗑）

用法::

    C:/Users/12069/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/gen_tray_action_icons.py

产出::

    tray/icons/act_folder.png        (20x20，安装器「指定」按钮)
    tray/icons/act_trash.png         (20x20，安装器「卸载」按钮)
    assets/icons/png/16/act_*.png    (16px，随图标集标准尺寸沉淀)
    assets/icons/png/32/act_*.png    (32px，同上)

设计规范与 tools/gen_icons.py 完全一致：
32 网格 + 8 倍超采样 + LANCZOS 降采样；圆角方块底 + 白色字形；
扁平、无渐变、无描边；16px 下仍可辨识。
"""

import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAY_ICONS = os.path.join(ROOT, "tray", "icons")
ASSET_PNG = os.path.join(ROOT, "assets", "icons", "png")

SS = 8          # 超采样倍数
GRID = 32       # 设计网格
N = GRID * SS   # 实际绘制分辨率 256

# 调色板与 gen_icons.py 保持一致
BLUE = (59, 130, 246)      # act_folder：与 act_restart 同族的动作蓝
SLATE = (100, 116, 139)    # act_trash：中性灰，避免与 act_stop 的红色混淆
WHITE = (255, 255, 255, 255)
ERASE = (0, 0, 0, 0)


def u(*v):
    """把 32 网格坐标换算成绘制分辨率坐标。"""
    return [x * SS for x in v]


def rrect(d, x0, y0, x1, y1, r=1.2, fill=WHITE):
    d.rounded_rectangle(u(x0, y0, x1, y1), radius=r * SS, fill=fill)


def tile(d, color, inset=1.0, radius=7.5):
    """圆角方块底（与 gen_icons.py 同款）。"""
    d.rounded_rectangle(u(inset, inset, GRID - inset, GRID - inset),
                        radius=radius * SS, fill=color + (255,))


def g_folder(d):
    """文件夹：后 tab + 袋体，圆角。"""
    rrect(d, 7.0, 9.4, 15.2, 12.4, r=1.2)      # 后 tab
    rrect(d, 5.6, 11.6, 26.4, 23.2, r=2.2)     # 袋体（盖住 tab 底部）


def g_trash(d):
    """垃圾桶：提手 + 盖 + 桶身 + 两道镂空条纹（透出底色）。"""
    rrect(d, 12.8, 6.8, 19.2, 9.6, r=1.0)      # 提手
    rrect(d, 8.6, 8.8, 23.4, 11.2, r=1.2)      # 盖
    rrect(d, 10.2, 12.6, 21.8, 25.2, r=2.0)    # 桶身
    d.rounded_rectangle(u(13.6, 14.8, 15.0, 23.2), radius=0.6 * SS, fill=ERASE)
    d.rounded_rectangle(u(17.0, 14.8, 18.4, 23.2), radius=0.6 * SS, fill=ERASE)


ICONS = [
    ("act_folder", BLUE,  g_folder),
    ("act_trash",  SLATE, g_trash),
]


def render(color, glyph):
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tile(d, color)
    glyph(d)
    return img


def save(img, path, size):
    im = img.resize((size, size), Image.LANCZOS)
    im.save(path)
    return path


def main():
    os.makedirs(TRAY_ICONS, exist_ok=True)
    for s in (16, 32):
        os.makedirs(os.path.join(ASSET_PNG, str(s)), exist_ok=True)

    made = 0
    for name, color, glyph in ICONS:
        img = render(color, glyph)
        p20 = save(img, os.path.join(TRAY_ICONS, name + ".png"), 20)
        print("  %-12s -> %s (20px)" % (name, os.path.relpath(p20, ROOT)))
        for s in (16, 32):
            ps = save(img, os.path.join(ASSET_PNG, str(s), name + ".png"), s)
            print("  %-12s -> %s (%dpx)" % (name, os.path.relpath(ps, ROOT), s))
        made += 1
    print("共 %d 个图标" % made)
    return 0


if __name__ == "__main__":
    sys.exit(main())
