# -*- coding: utf-8 -*-
"""生成 AssetBridge 的 Maya shelf 图标 PNG（纯标准库，零依赖）。

设计：左蓝方块 + 右橙方块 + 中间青色箭头 = 「在两个软件之间传资产」。
输出 64x64 RGBA（带透明通道 + 4x4 超采样抗锯齿）。
"""
import os
import struct
import zlib

W = H = 64
SS = 4                      # 每像素 4x4 超采样，得到平滑边缘

BLUE = (0x42, 0xA5, 0xF5)   # 左方块
ORANGE = (0xFF, 0xB3, 0x00)  # 右方块
TEAL = (0x26, 0xC6, 0xDA)   # 中间箭头


def _rr(px, py, x0, y0, x1, y1, r):
    """圆角矩形命中测试。"""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def _rect(px, py, x0, y0, x1, y1):
    return x0 <= px <= x1 and y0 <= py <= y1


def _tri(px, py, a, b, c):
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = s((px, py), a, b), s((px, py), b, c), s((px, py), c, a)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def sample(px, py):
    """返回该子采样点的颜色 (r,g,b)，不在图形内返回 None。箭头在最上层。"""
    # 箭头：横杆 + 右向三角
    if _rect(px, py, 24, 30, 34, 34) or _tri(px, py, (34, 25), (34, 39), (40, 32)):
        return TEAL
    if _rr(px, py, 6, 20, 24, 44, 4):
        return BLUE
    if _rr(px, py, 40, 20, 58, 44, 4):
        return ORANGE
    return None


def main():
    raw = bytearray()
    for iy in range(H):
        raw.append(0)  # PNG 每行的 filter byte
        for ix in range(W):
            r = g = b = 0.0
            a = 0.0
            for j in range(SS):
                for i in range(SS):
                    px = ix + (i + 0.5) / SS
                    py = iy + (j + 0.5) / SS
                    c = sample(px, py)
                    if c:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        a += 1.0
            n = SS * SS
            if a > 0:
                raw += bytes((int(r / a), int(g / a), int(b / a), int(255 * a / n)))
            else:
                raw += b"\x00\x00\x00\x00"

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(here), "maya", "plugin",
                       "asset_bridge_maya", "assetbridge.png")
    with open(src, "wb") as fh:
        fh.write(png)
    print("[icon] 已生成", src, len(png), "bytes")

    # 复制到 Maya 的图标查找路径。
    # 注意：本机 Maya 2023 是中文界面，prefs 在 <2023>/zh_CN/prefs/ 下，
    # 所以 2023/zh_CN/prefs/icons 才是真正被搜索的目录（2023/prefs/icons 也放一份兜底）。
    home = os.path.expanduser("~")
    for sub in (os.path.join("Documents", "maya", "2023", "zh_CN", "prefs", "icons"),
                os.path.join("Documents", "maya", "2023", "prefs", "icons")):
        try:
            d = os.path.join(home, sub)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "assetbridge.png"), "wb") as fh:
                fh.write(png)
            print("[icon] 已安装到", os.path.join(d, "assetbridge.png"))
        except Exception as exc:  # noqa: BLE001
            print("[icon] 复制失败", sub, exc)


if __name__ == "__main__":
    main()
