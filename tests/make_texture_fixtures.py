# -*- coding: utf-8 -*-
"""Asset Bridge :: 贴图链路测试样本生成器。

Substance Painter 回传的贴图是 PNG，为了不引入任何第三方依赖（不许用 PIL），
这里用标准库 zlib + struct 手写最小 PNG 编码器，生成 3 张 64x64 的纯色图：

    SM_CalibCube_BaseColor.png  纯红 (255, 0, 0)
    SM_CalibCube_Normal.png     法线基准色 (128, 128, 255)
    SM_CalibCube_ORM.png        遮蔽/粗糙/金属打包色 (255, 0, 0)

PNG 结构（真彩色、8bit、无交错）：
    8 字节签名 | IHDR | IDAT | IEND
每个 chunk：长度(4) + 类型(4) + 数据 + CRC32(4)（CRC 覆盖 类型+数据）。
IDAT 是 zlib 压缩后的扫描线流，每条扫描线前置 1 字节滤波器类型（这里恒为 0）。

用法：
    python make_texture_fixtures.py            # 生成 + 自校验
    python make_texture_fixtures.py --verify   # 只校验已有文件
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path

FIXTURE_DIR = Path(r"E:\CirnoWorkSpace\AssetBridge\tests\fixtures")
SIZE = 64

# 文件名 -> RGB 纯色
SAMPLES = {
    "SM_CalibCube_BaseColor.png": (255, 0, 0),
    "SM_CalibCube_Normal.png": (128, 128, 255),
    "SM_CalibCube_ORM.png": (255, 0, 0),
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    """打包一个 PNG chunk：长度 + 类型 + 数据 + CRC32。"""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_rgb_png(width: int, height: int, rgb) -> bytes:
    """把一张纯色 RGB 图编码成 PNG 字节流（无第三方依赖）。"""
    r, g, b = rgb
    raw = bytearray()
    row = bytes((r, g, b)) * width
    for _ in range(height):
        raw.append(0)          # 滤波器类型 0 = None
        raw += row
    ihdr = struct.pack(
        ">IIBBBBB",
        width, height,
        8,                     # bit depth
        2,                     # color type 2 = Truecolor(RGB)
        0,                     # compression method = deflate
        0,                     # filter method
        0,                     # interlace = none
    )
    idat = zlib.compress(bytes(raw), 9)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# 最小解码 / 校验
# ---------------------------------------------------------------------------


def parse_png(path) -> dict:
    """最小 PNG 解析器：校验签名、逐个 chunk 的 CRC，并读出 IHDR 尺寸。

    返回 {"width","height","bit_depth","color_type","chunks":[...]}；失败抛异常。
    """
    data = Path(path).read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("PNG 签名不匹配")

    pos = len(PNG_SIGNATURE)
    info = {"width": None, "height": None, "bit_depth": None,
            "color_type": None, "chunks": [], "idat_bytes": 0, "crc_ok": True}
    seen = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise ValueError(f"chunk {tag!r} 数据长度不足")
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        expect = zlib.crc32(tag + body) & 0xFFFFFFFF
        if crc != expect:
            info["crc_ok"] = False
            raise ValueError(f"chunk {tag!r} CRC 校验失败")
        seen.append(tag.decode("ascii", "replace"))

        if tag == b"IHDR":
            (w, h, depth, ctype, comp, filt, inter) = struct.unpack(
                ">IIBBBBB", body
            )
            info.update(width=w, height=h, bit_depth=depth, color_type=ctype)
            if (comp, filt, inter) != (0, 0, 0):
                raise ValueError("不支持的压缩/滤波/交错方式")
        elif tag == b"IDAT":
            info["idat_bytes"] += length
            zlib.decompress(body)   # 解码失败会抛异常，能及时暴露坏图

        pos += 12 + length
        if tag == b"IEND":
            break

    info["chunks"] = seen
    if info["width"] is None:
        raise ValueError("缺少 IHDR")
    if not seen or seen[-1] != "IEND":
        raise ValueError("缺少 IEND")
    return info


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def generate(force: bool = True) -> dict:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, rgb in SAMPLES.items():
        fp = FIXTURE_DIR / name
        if force or not fp.exists():
            fp.write_bytes(encode_rgb_png(SIZE, SIZE, rgb))
        out[name] = str(fp)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成/校验贴图测试样本")
    ap.add_argument("--verify", action="store_true", help="只校验，不重新生成")
    args = ap.parse_args(argv)

    if not args.verify:
        files = generate(force=True)
        print(f"[fixtures] 已生成 {len(files)} 张样本 -> {FIXTURE_DIR}")
    else:
        files = {n: str(FIXTURE_DIR / n) for n in SAMPLES}

    ok = True
    for name in SAMPLES:
        fp = Path(files[name])
        try:
            info = parse_png(fp)
            dim_ok = (info["width"] == SIZE and info["height"] == SIZE)
            size = fp.stat().st_size
            print(f"[verify] {name}: "
                  f"{info['width']}x{info['height']} "
                  f"bit={info['bit_depth']} color_type={info['color_type']} "
                  f"chunks={info['chunks']} crc_ok={info['crc_ok']} "
                  f"bytes={size} -> {'OK' if dim_ok else 'SIZE MISMATCH'}")
            ok = ok and dim_ok
        except Exception as exc:
            ok = False
            print(f"[verify] {name}: FAIL -> {type(exc).__name__}: {exc}")

    print(f"[fixtures] 校验结果：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
