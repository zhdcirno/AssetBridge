# -*- coding: utf-8 -*-
"""
Asset Bridge - 图标集生成器（可复现）

用法::

    C:/Users/12069/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/gen_icons.py

产出::

    assets/icons/png/<size>/*.png     (16 / 24 / 32 / 48 / 64)
    assets/icons/ico/*.ico            (16+24+32+48+64 多尺寸)
    assets/icons/PREVIEW.png          (联系人表，方便一眼挑选)

设计规范（统一视觉语言）
------------------------
* 画布 32x32 网格；以 8 倍超采样绘制再降采样 -> 边缘干净、无锯齿。
* 「状态」= 实心圆 + 白色字形：绿=运行中 / 红=已停止 / 琥珀=自动恢复中 / 灰=守护已暂停
* 「动作」= 圆角方块 + 白色字形（品牌色统一）
* 「DCC 徽标」= 圆角方块 + 白色首字母
* 扁平化、无渐变、无描边，保证 16px 下仍可辨识。
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icons")
PNG_DIR = os.path.join(OUT, "png")
ICO_DIR = os.path.join(OUT, "ico")

SS = 8                      # 超采样倍数
GRID = 32                   # 设计网格
N = GRID * SS               # 实际绘制分辨率 256

SIZES = (16, 24, 32, 48, 64)

# 调色板
C = {
    "green":    (34, 197, 94),
    "red":      (239, 68, 68),
    "amber":    (245, 158, 11),
    "slate":    (100, 116, 139),
    "blue":     (59, 130, 246),
    "teal":     (20, 184, 166),
    "graphite": (47, 56, 74),
    "orange":   (251, 146, 60),
    "cyan":     (6, 182, 212),
    "violet":   (139, 92, 246),
}
WHITE = (255, 255, 255, 255)
ERASE = (0, 0, 0, 0)


def u(*v):
    """把 32 网格坐标换算成绘制分辨率坐标。"""
    return [x * SS for x in v]


# ---------------------------------------------------------------- 基础形状
def tile(d, color, inset=1.0, radius=7.5):
    """圆角方块底。"""
    d.rounded_rectangle(u(inset, inset, GRID - inset, GRID - inset),
                        radius=radius * SS, fill=color + (255,))


def disc(d, color, inset=1.0):
    """实心圆底。"""
    d.ellipse(u(inset, inset, GRID - inset, GRID - inset), fill=color + (255,))


def tri(d, pts, fill=WHITE):
    d.polygon([tuple(u(*p)) for p in pts], fill=fill)


def rrect(d, x0, y0, x1, y1, r=1.2, fill=WHITE):
    d.rounded_rectangle(u(x0, y0, x1, y1), radius=r * SS, fill=fill)


def hole(d, x0, y0, x1, y1):
    d.ellipse(u(x0, y0, x1, y1), fill=ERASE)


def teeth(d, n=8, r=9.7, w=3.8, h=5.6, fill=WHITE):
    """齿轮的齿：以「径向为高的矩形」精确旋转排布（而不是摆方块）。"""
    cx = cy = GRID / 2.0
    for i in range(n):
        a = math.radians(i * (360.0 / n))
        ca, sa = math.cos(a), math.sin(a)
        pts = []
        for lx, ly in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            px = cx + ca * (r + ly) - sa * lx
            py = cy + sa * (r + ly) + ca * lx
            pts.append(tuple(u(px, py)))
        d.polygon(pts, fill=fill)


def arc_arrow(d, color=WHITE):
    """重启：缺口圆弧 + 切向箭头（经典 refresh 造型）。

    PIL 的 arc 角度以 3 点钟为 0、顺时针递增（因为屏幕 y 向下）。
    """
    cx = cy = GRID / 2.0
    r = 10.0
    d.arc(u(16 - r, 16 - r, 16 + r, 16 + r), start=40, end=320,
          fill=color, width=int(3.0 * SS))
    # 箭头放在圆弧终点(320°，右上)，沿切线（顺时针）方向
    a = math.radians(320)
    tx, ty = cx + r * math.cos(a), cy + r * math.sin(a)
    dx, dy = -math.sin(a), math.cos(a)        # 顺时针切向
    nx, ny = math.cos(a), math.sin(a)         # 径向
    tip = (tx + dx * 5.2, ty + dy * 5.2)
    b1 = (tx - dx * 0.9 + nx * 3.5, ty - dy * 0.9 + ny * 3.5)
    b2 = (tx - dx * 0.9 - nx * 3.5, ty - dy * 0.9 - ny * 3.5)
    d.polygon([tuple(u(*tip)), tuple(u(*b1)), tuple(u(*b2))], fill=color)


# ------------------------------------------------------------------- 字形
def g_play(d):
    tri(d, [(12.6, 9.2), (23.2, 16.0), (12.6, 22.8)])


def g_stop(d):
    rrect(d, 11.4, 11.4, 20.6, 20.6, r=1.4)


def g_pause(d):
    rrect(d, 11.8, 9.8, 14.6, 22.2, r=1.0)
    rrect(d, 17.4, 9.8, 20.2, 22.2, r=1.0)


def g_warn(d):
    rrect(d, 14.5, 8.2, 17.5, 18.4, r=1.3)
    d.ellipse(u(14.3, 20.2, 17.7, 23.6), fill=WHITE)


def g_gear(d):
    # 齿要「短而密」贴着圆盘，否则 32px 下会像雪花/星芒
    teeth(d, n=8, r=10.2, w=3.6, h=4.4)
    d.ellipse(u(6.9, 6.9, 25.1, 25.1), fill=WHITE)      # 盘体 r=9.1
    hole(d, 12.7, 12.7, 19.3, 19.3)                       # 中心孔 r=3.3


def g_up(d):
    rrect(d, 14.4, 13.4, 17.6, 23.4, r=1.1)
    tri(d, [(16.0, 7.4), (22.0, 14.6), (10.0, 14.6)])


def g_down(d):
    rrect(d, 14.4, 8.6, 17.6, 18.6, r=1.1)
    tri(d, [(16.0, 24.6), (22.0, 17.4), (10.0, 17.4)])


def g_restart(d):
    arc_arrow(d)


def g_swap(d):
    """同步：上下两个反向箭头（竖直互发）。"""
    # 上：向上
    rrect(d, 14.5, 12.4, 17.5, 23.4, r=1.0)
    tri(d, [(16.0, 7.6), (21.1, 13.6), (10.9, 13.6)])
    # 下：向下
    rrect(d, 14.5, 8.6, 17.5, 19.6, r=1.0)
    tri(d, [(16.0, 24.4), (21.1, 18.4), (10.9, 18.4)])


def g_swap_min(d):
    """极简 ⇄：只有两个反向三角（无杆），16px 下比带杆的更干净。"""
    # 上：向右
    tri(d, [(8.6, 8.2), (8.6, 15.4), (21.0, 11.8)])
    # 下：向左
    tri(d, [(23.4, 16.6), (23.4, 23.8), (11.0, 20.2)])


def g_swap_h(d):
    """应用主图标：左右两个反向箭头（⇄ 互发）。"""
    # 上：向右
    rrect(d, 8.4, 10.4, 20.6, 13.2, r=1.0)
    tri(d, [(20.0, 7.4), (26.6, 11.8), (20.0, 16.2)])
    # 下：向左
    rrect(d, 11.4, 18.8, 23.6, 21.6, r=1.0)
    tri(d, [(12.0, 15.8), (5.4, 20.2), (12.0, 24.6)])


# ------------------------------------------------------------------- 字体
def pick_font(size, bold=True):
    cands = [
        "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/tahomabd.ttf", "C:/Windows/Fonts/tahoma.ttf",
    ]
    for p in cands:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def g_letter(d, ch):
    f = pick_font(int(19 * SS))
    d.text((N / 2, N / 2 + 0.4 * SS), ch, font=f, fill=WHITE, anchor="mm")


# ------------------------------------------------------------------- 图标表
# (文件名, 类别, 底色, 绘制函数)
ICONS = [
    # --- 状态（托盘用，圆角方块 + 状态字形。
    #     实测 16px 下「带杆的 ⇄」比「两个光三角」清楚得多，故运行中用 g_swap_h）---
    ("state_running",  "tile",  C["green"], g_swap_h),
    ("state_stopped",  "tile",  C["red"],   g_stop),
    ("state_recover",  "tile",  C["amber"], g_warn),
    ("state_paused",   "tile",  C["slate"], g_pause),
    # --- 动作（圆角方块） ---
    ("act_start",      "tile",  C["green"], g_play),
    ("act_stop",       "tile",  C["red"],   g_stop),
    ("act_restart",    "tile",  C["blue"],  g_restart),
    ("act_send",       "tile",  C["teal"],  g_up),
    ("act_receive",    "tile",  C["teal"],  g_down),
    ("act_sync",       "tile",  C["blue"],  g_swap),
    ("act_settings",   "tile",  C["slate"], g_gear),
    ("app_logo",       "tile",  C["graphite"], g_swap_h),
    # --- DCC 徽标 ---
    ("dcc_blender",    "tile",  C["orange"],   lambda d: g_letter(d, "B")),
    ("dcc_unreal",     "tile",  C["graphite"], lambda d: g_letter(d, "U")),
    ("dcc_maya",       "tile",  C["cyan"],     lambda d: g_letter(d, "M")),
    ("dcc_painter",    "tile",  C["violet"],   lambda d: g_letter(d, "S")),
]


def render(name, kind, color, glyph):
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    (disc if kind == "disc" else tile)(d, color)
    glyph(d)
    outs = {}
    for s in SIZES:
        outs[s] = img.resize((s, s), Image.LANCZOS)
    return outs


def save_ico(name, imgs, sizes=(16, 24, 32, 48, 64)):
    """PIL 的 ICO 保存要求 sizes 是 (w,h) 元组列表。"""
    path = os.path.join(ICO_DIR, name + ".ico")
    imgs.save(path, sizes=[(s, s) for s in sizes])
    return path


def main():
    for d in (PNG_DIR, ICO_DIR):
        os.makedirs(d, exist_ok=True)
    for s in SIZES:
        os.makedirs(os.path.join(PNG_DIR, str(s)), exist_ok=True)

    made, skipped = [], []
    for name, kind, color, glyph in ICONS:
        outs = render(name, kind, color, glyph)
        # PNG 与 ICO 分开兜底：某个尺寸的 PNG 被占用（例如托盘已加载该图）时，
        # 仍然要把 ico 和其余尺寸写出去，而不是整轮失败。
        bad = []
        for s, im in outs.items():
            try:
                im.save(os.path.join(PNG_DIR, str(s), name + ".png"))
            except PermissionError as exc:
                bad.append(os.path.basename(exc.filename or str(s)))
        try:
            save_ico(name, outs[64])
        except PermissionError as exc:
            bad.append(os.path.basename(exc.filename or "ico"))
        if bad:
            skipped.append(name)
            print("  %-16s PARTIAL (被占用跳过: %s)" % (name, ", ".join(bad)))
        else:
            print("  %-16s ok" % name)
        made.append((name, outs[64]))

    # ---- 联系人表 ----
    cell, pad, cols = 88, 12, 4
    rows = int(math.ceil(len(made) / cols))
    label_h = 20
    W = cols * (cell + pad) + pad
    H = rows * (cell + label_h + pad) + pad
    sheet = Image.new("RGBA", (W, H), (248, 250, 252, 255))
    sd = ImageDraw.Draw(sheet)
    font = pick_font(12)
    for i, (name, im) in enumerate(made):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = pad + r * (cell + label_h + pad)
        sheet.paste(im.resize((cell - 8, cell - 8), Image.LANCZOS),
                    (x + 4, y), im.resize((cell - 8, cell - 8), Image.LANCZOS))
        sd.text((x + cell / 2, y + cell + 2), name, font=font,
                fill=(30, 41, 59, 255), anchor="ma")
    prev = os.path.join(OUT, "PREVIEW.png")
    sheet.save(prev)
    print("\nPREVIEW ->", prev)

    # ---- 小尺寸可辨识度检查：16/24/32「实际大小 + 3 倍放大」对照 ----
    zoom, small, row_pad = 3, (16, 24, 32), 34
    rows = []
    for sz in small:
        cw = sz * zoom + sz + 8
        rows.append((sz, cw, pad + len(made) * cw + pad))
    SW = max(r[2] for r in rows)
    SH = sum(r[0] * zoom + row_pad for r in rows) + pad
    ss = Image.new("RGBA", (SW, SH), (248, 250, 252, 255))
    sd2 = ImageDraw.Draw(ss)
    f2 = pick_font(11)
    y = pad
    for sz, cw, _ in rows:
        sd2.text((pad, y + 4), "%dpx  实际大小  |  右侧为 %dx 放大" % (sz, zoom),
                 font=f2, fill=(100, 116, 139, 255))
        for c, (name, _) in enumerate(made):
            im = Image.open(os.path.join(PNG_DIR, str(sz), name + ".png"))
            x = pad + c * cw
            yy = y + 20
            ss.paste(im, (x, yy), im)
            big = im.resize((sz * zoom, sz * zoom), Image.NEAREST)
            ss.paste(big, (x + sz + 8, yy), big)
        y += sz * zoom + row_pad
    small_prev = os.path.join(OUT, "PREVIEW_SMALL.png")
    ss.save(small_prev)
    print("SMALL   ->", small_prev)
    print("PNG     ->", PNG_DIR)
    print("ICO     ->", ICO_DIR)
    print("共 %d 个图标" % len(made))


if __name__ == "__main__":
    sys.exit(main())
