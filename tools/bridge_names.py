# -*- coding: utf-8 -*-
"""
asset-bridge :: 名称归一化（中文 -> 拼音 ASCII 驼峰）
============================================================================
所有 DCC 适配器与中转服务共用本模块：把可能含中文/非 ASCII 的
资产名、材质名强制转成 ASCII（拼音驼峰），避免：

  * 服务端 SAFE_NAME_RE 拒绝（HTTP 400，日志里的 `收到: '无标题'`）；
  * FBX / UE 资产名在不同软件之间出现乱码；
  * 中文文件名在某些 DCC 的导入器里解析失败。

依赖：
  * 纯标准库；拼音表随模块一起分发（同目录的 bridge_pinyin.dat）。
  * Python 2.7 / 3.x 通用（Maya 2018 及更早内置 py2.7）。

用法：
    from bridge_names import to_ascii
    to_ascii("木头方块_01")   ->  "MuTouFangKuai_01"
    to_ascii("SM_椅子")       ->  "SM_YiZi"
    to_ascii("SM_Cube")       ->  "SM_Cube"     # 纯 ASCII 原样保留
"""
import os

_BASE = 0x4E00          # CJK 基本区起点
_TOP = 0x9FFF           # CJK 基本区终点
_ROWS = None


def _rows():
    """懒加载拼音表（每行一个码位对应一个音节；"." 表示无读音）。"""
    global _ROWS
    if _ROWS is None:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "bridge_pinyin.dat"), "r") as fh:
                _ROWS = fh.read().split("\n")
        except Exception:
            _ROWS = []
    return _ROWS


def _pinyin(ch):
    rows = _rows()
    idx = ord(ch) - _BASE
    if 0 <= idx < len(rows):
        s = rows[idx]
        if s and s != ".":
            return s
    return ""


def _is_cjk(ch):
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF      # 扩展 A + 基本区


def to_ascii(name, default="Asset"):
    """把任意名称转成 ASCII。

    规则：
      * ASCII 字母/数字/`_`/`-`/`.` 原样保留；
      * 中文（CJK）取拼音，**每个音节首字母大写**（即拼音驼峰，如 MuTouFangKuai）；
      * 其余非 ASCII 字符退化为 `uXXXX`（保证可逆、可读、不丢信息）；
      * 其余 ASCII 符号（空格、括号等）统一换成 `_`，连续 `_` 折叠成一个。
    """
    if name is None:
        return default
    out = []
    for ch in name:
        o = ord(ch)
        if o < 128:
            if ch.isalnum() or ch in "_-.":
                out.append(ch)
            else:
                out.append("_")
            continue
        syl = _pinyin(ch) if _is_cjk(ch) else ""
        if syl:
            out.append(syl[0].upper() + syl[1:])
        elif o <= 0xFFFF:
            out.append("u%04X" % o)
        else:
            out.append("u%X" % o)
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_.")
    return s or default


if __name__ == "__main__":
    for t in ("木头方块_01", "SM_椅子", "SM_Cube", "无标题", "贴图 集(1)", "椅子A_高模"):
        print("%-14s -> %s" % (t, to_ascii(t)))
