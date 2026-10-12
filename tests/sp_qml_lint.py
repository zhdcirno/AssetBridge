# -*- coding: utf-8 -*-
"""QML/JS 语法自检：把 main.qml 里的 function 体抽出来交给 Node 解析。

为什么需要：SP 的插件加载是「黑盒」——QML 有语法错时 SP 只会静默保留
上一次加载成功的实例（本次调试就踩到了：改了文件但行为还是旧的），
排查起来很费时间。这个脚本用 Node 的解析器先过一遍，把低级语法错误
拦在重启 SP 之前。

注意：只检查 `function` 体内的**纯 JS**部分，QML 自身的语法
（属性声明、onTick: 之类的信号处理器）不在这里校验。

用法：
    python tests/sp_qml_lint.py
"""
from __future__ import print_function

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QML = os.path.join(ROOT, "substance_painter", "plugin", "asset-bridge", "main.qml")
NODE = r"C:\Users\12069\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"


def extract_functions(src):
    """按大括号配平抽出所有 `function name(args) { ... }` 块。"""
    out = []
    for m in re.finditer(r"\bfunction\s+(\w+)\s*\(([^)]*)\)\s*\{", src):
        start = m.end() - 1
        depth = 0
        i = start
        in_str = None
        while i < len(src):
            ch = src[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in "\"'":
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = src[m.start():i + 1]
        out.append((m.group(1), body))
    return out


def main():
    src = open(QML, encoding="utf-8").read()
    funcs = extract_functions(src)
    print("从 {0} 抽出 {1} 个函数".format(os.path.basename(QML), len(funcs)))

    js_path = os.path.join(HERE, "_sp_qml_lint_tmp.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        # alg / XMLHttpRequest / JSON 都是 SP 注入的宿主对象，这里只要语法正确
        fh.write("var alg = {log: {info: function(){}, warn: function(){}, error: function(){}},\n"
                 "  ui: {}, project: {}, texturesets: {}, mapexport: {}, baking: {}, fileIO: {}};\n")
        fh.write("var XMLHttpRequest = function(){};\n")
        for _, body in funcs:
            fh.write(body + "\n")

    if not os.path.exists(NODE):
        print("找不到 Node，跳过解析（{0}）".format(NODE))
        return 2

    proc = subprocess.run([NODE, "--check", js_path],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "ignore").strip()
    os.remove(js_path)

    if proc.returncode == 0:
        print("[PASS] 全部函数体语法正确")
        # 顺手做几条我们踩过坑的规则检查
        checks = [
            ("没有用 let/const（SP 的 QML 引擎只认 ES5 的 var）",
             not re.search(r"\b(let|const)\s+\w", src)),
            ("没有用箭头函数", "=>" not in src),
            ("没有用模板字符串", "`" not in src),
            ("用了 reloadMesh 吗（该 API 不存在）", "reloadMesh" not in src.split("//")[0]),
            ("textures_request 分支存在", "textures_request" in src),
            ("导出结果过滤了空字符串", "if (maps[ch])" in src),
            ("烘焙调用的是 alg.baking.bake", "alg.baking.bake(" in src),
        ]
        bad = 0
        for name, ok in checks:
            print("  [{0}] {1}".format("PASS" if ok else "FAIL", name))
            if not ok:
                bad += 1
        return 1 if bad else 0

    print("[FAIL] 语法错误：")
    print(out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
