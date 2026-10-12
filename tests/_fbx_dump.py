# -*- coding: utf-8 -*-
"""一次性诊断：把 FBX 二进制里的节点树关键属性打印出来（系统 Python 跑）。

只为了回答一个问题：Blender 5.0 导出的 FBX，米->厘米换算到底落在哪里
（顶点烘焙 / 根节点缩放 / UnitScaleFactor 声明）。
"""
import struct
import sys
import zlib
from pathlib import Path


def read_prop(data, off):
    t = data[off:off + 1]
    off += 1
    if t == b"Y":
        return struct.unpack_from("<h", data, off)[0], off + 2
    if t == b"C":
        return bool(data[off]), off + 1
    if t == b"I":
        return struct.unpack_from("<i", data, off)[0], off + 4
    if t == b"F":
        return struct.unpack_from("<f", data, off)[0], off + 4
    if t == b"D":
        return struct.unpack_from("<d", data, off)[0], off + 8
    if t == b"L":
        return struct.unpack_from("<q", data, off)[0], off + 8
    if t in (b"R", b"S"):
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        return data[off:off + n], off + n
    if t in (b"b", b"i", b"l", b"f", b"d", b"c"):
        n, enc, clen = struct.unpack_from("<III", data, off)
        off += 12
        return ("ARR", t, n, enc, data[off:off + clen]), off + clen
    raise ValueError("bad type %r @ %d" % (t, off))


def read_node(data, off, hdr64):
    if hdr64:
        end_off, _np, plen = struct.unpack_from("<QQQ", data, off)
        off += 24
    else:
        end_off, _np, plen = struct.unpack_from("<III", data, off)
        off += 12
    nl = data[off]
    off += 1
    name = data[off:off + nl].decode("ascii", "replace")
    off += nl
    props, pend = [], off + plen
    while off < pend:
        v, off = read_prop(data, off)
        props.append(v)
    return name, props, off, end_off


def walk(data, off, end, hdr64, path, sink):
    hdr = 25 if hdr64 else 13
    while off + hdr <= end:
        if data[off:off + hdr] == b"\x00" * hdr:
            return off + hdr
        name, props, coff, end_off = read_node(data, off, hdr64)
        node_end = end_off if end_off else end
        here = path + (name,)
        sink(here, props)
        if coff < node_end:
            walk(data, coff, node_end, hdr64, here, sink)
        off = node_end
        if node_end >= end:
            break
    return off


def main(paths):
    for p in paths:
        p = Path(p)
        print("=" * 78)
        print("FILE:", p.name, p.stat().st_size, "bytes")
        data = p.read_bytes()
        ver = struct.unpack_from("<I", data, 23)[0]
        hdr64 = ver >= 7500
        print("fbx_version:", ver)

        rows = []

        def sink(hpath, props):
            rows.append((hpath, props))

        walk(data, 27, len(data), hdr64, (), sink)

        # GlobalSettings
        for hpath, props in rows:
            if hpath and hpath[0] == "GlobalSettings" and hpath[-1] == "P":
                nm = props[0].decode("ascii", "replace")
                if nm in ("UnitScaleFactor", "OriginalUnitScaleFactor",
                          "UpAxis", "FrontAxis", "CoordSystem", "TimeMode"):
                    val = None
                    for q in reversed(props):
                        if isinstance(q, bool):
                            continue
                        if isinstance(q, (int, float)):
                            val = q
                            break
                    print(f"  GlobalSettings/{nm} = {val}")

        # 所有 P 节点（带完整路径），用于定位
        print("  --- 全部 P 节点 ---")
        for hpath, props in rows:
            if hpath and hpath[-1] == "P" and props and isinstance(props[0], bytes):
                nm = props[0].decode("ascii", "replace")
                nums = [q for q in props if isinstance(q, (int, float))
                        and not isinstance(q, bool)]
                print(f"      {'/'.join(hpath[:-1])} :: {nm} nums={nums}")

        # Model 节点的名字与 Lcl Scaling
        print("  --- Model 节点 ---")
        for hpath, props in rows:
            if len(hpath) >= 2 and hpath[0] == "Objects" and hpath[1] == "Model":
                if len(hpath) == 3 and len(props) >= 2:
                    uid, name = props[0], props[1]
                    nm = name.decode("ascii", "replace") if isinstance(name, bytes) else name
                    print(f"  Model uid={uid} name={nm!r}")
                if hpath[-1] == "P":
                    nm = props[0].decode("ascii", "replace")
                    if nm in ("Lcl Translation", "Lcl Rotation", "Lcl Scaling",
                              "GeometricScaling", "ScalingMax", "DefaultAttributeIndex"):
                        nums = []
                        for q in reversed(props):
                            if isinstance(q, bool):
                                continue
                            if isinstance(q, (int, float)):
                                nums.insert(0, q)
                            else:
                                break
                        print(f"      {nm} = {nums}")

        # 顶点量级
        for hpath, props in rows:
            if hpath and hpath[-1] == "Vertices" and props:
                tag, t, n, enc, raw = props[0]
                if enc == 1:
                    raw = zlib.decompress(raw)
                fmt = "<d" if t == b"d" else "<f"
                step = struct.calcsize(fmt)
                vals = [struct.unpack_from(fmt, raw, i * step)[0] for i in range(n)]
                print(f"  Vertices[{n}] max_abs={max(abs(v) for v in vals)}")
                break


if __name__ == "__main__":
    main(sys.argv[1:])
