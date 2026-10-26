#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Asset Bridge 一键安装器 (One-click installer).

把 DCC 适配器部署到本机各软件，并生成中转服务的启动脚本 start_bridge_server.bat：
  - Blender / Maya / Substance Painter / Unreal：复制插件到各自用户/引擎目录
  - Marmoset Toolbag 5：复制插件到 <安装目录>\\data\\plugin\\asset_bridge
  - Photoshop / SAI2：不装插件，仅把 exe 路径同步进 tools\\editor_daemon.json
    （贴图由「编辑器守护进程」调用本程序打开，保存后自动回传）


用法:
    python install.py            # 实际执行部署
    python install.py --dry-run   # 仅打印将改动的路径清单，不写任何文件、不备份

已知坑 (KNOWN GOTCHAS):
1. 客户端强制绕过 HTTP 代理：连 127.0.0.1 也会被公司代理劫成 502，因此所有
   本地 HTTP 调用必须设置 NO_PROXY / no_proxy=127.0.0.1,localhost。
2. 中转服务只监听 127.0.0.1，无鉴权；跨机访问请用 SSH 隧道转发端口 27631。
3. Maya 的 open() 在中文 Windows 上默认用 GBK 解码，读/写 userSetup.py 一律
   显式指定 encoding="utf-8"，避免 UnicodeDecodeError。
4. Maya commandPort 不能远程投喂长任务（会卡死解释器），因此 Maya 适配器通过
   userSetup.py 本地 executeDeferred 启动，而不是 commandPort。
5. Substance Painter 改 main.qml 后必须重启 SP 才生效（无热重载）。
6. 中转服务端口被占用会拒绝启动（防止双实例绑定）。
7. Blender / Unreal 插件启用需在 GUI 里手动勾选（Blender Add-ons / UE 已通过
   DefaultEngine.ini 的 PythonScriptPlugin 自动启用）；Maya 通过 userSetup 重启
   自启；SP 重启生效。

安全要求:
- 任何复制/覆盖前，先备份目标已有同名文件或目录到
  backups/install_<YYYYMMDD_HHMMSS>/，绝不删除用户原文件。
- 对个人目录（Documents 等）只用「复制 + 先备份」，不做破坏性删除。
"""

import argparse
import datetime
import json
import os
import re
import winreg
import shutil
import subprocess
import sys
import traceback

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIME_ROOT = os.path.join(PROJECT_ROOT, "runtime")
SYSTEM_PYTHON = r"C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe"

# 中转服务启动脚本模板（{python_cmd} 在运行时替换为 python 或绝对路径）
# 增加 --watch 守护模式：调用 bridge_watchdog.ps1 看护中转服务。
BAT_TEMPLATE = (
    '@echo off\n'
    'cd /d "%~dp0"\n'
    'if "%1"=="--watch" (\n'
    '    echo 启动 Asset Bridge 守护模式（watchdog 看护中转服务）...\n'
    '    powershell -ExecutionPolicy Bypass -NoLogo -File "%~dp0bridge_watchdog.ps1"\n'
    '    goto :eof\n'
    ')\n'
    '{python_cmd} "%~dp0server\\server.py" --root "%~dp0runtime"\n'
    'pause\n'
)

# 四套适配器的源/目标定义
# Blender 5.0 的用户级插件目录（ADDON_PATHS 里真正被扫描的那个）。
# 注意：D:\DCC\Blender5.0.1\5.0\scripts\addons 并不在 5.0 的搜索路径里
# （5.0 内置目录叫 addons_core），第三方插件必须放用户级 addons。
# 四套适配器的源/目标定义（整理后的仓库布局：plugins/ 下按软件分目录）
BLENDER_SRC = os.path.join(PROJECT_ROOT, "plugins", "blender", "asset_bridge_blender")
# Blender 用户级插件根目录（多版本各自独立，安装时按检测到的版本分别部署）
BLENDER_ADDONS_BASE = (
    r"C:\Users\12069\AppData\Roaming\Blender Foundation\Blender"
)
# 历史错误路径（不在搜索路径里）与误装单文件，安装时清理
OLD_BLENDER_DST = r"D:\DCC\Blender5.0.1\5.0\scripts\addons\asset_bridge_blender"
MISINIT_BLENDER = (
    r"C:\Users\12069\AppData\Roaming\Blender Foundation\Blender\5.0"
    r"\scripts\addons\__init__.py"
)

MAYA_PKG_SRC = os.path.join(PROJECT_ROOT, "plugins", "maya", "asset_bridge_maya")
MAYA_PKG_DST = r"C:\Users\12069\Documents\maya\2023\zh_CN\scripts\asset_bridge_maya"
# userSetup 仅注入「建展架」代码（ensure_shelf 只建 UI、不联网，不会卡 Maya 启动）。
# 桥接自启改为「用户点开 AssetBridge 面板时」触发，刻意不写进 userSetup（历史教训：
# 在 userSetup 里直接 start() 会触发 Maya 启动期设置校验弹窗，导致卡在 quatnodes 界面）。
MAYA_USERSETUP_CANDIDATES = [
    r"C:\Users\12069\Documents\maya\2023\zh_CN\scripts\userSetup.py",
    r"C:\Users\12069\Documents\maya\scripts\userSetup.py",
]
MAYA_STARTUP_CODE = (
    "\n"
    "# >>> asset-bridge shelf >>>\n"
    "try:\n"
    "    import maya.utils\n"
    "    maya.utils.executeDeferred("
    "lambda: __import__('asset_bridge_maya').ensure_shelf())\n"
    "except Exception as _e:\n"
    "    print('[bridge] 创建展架失败:', _e)\n"
    "# <<< asset-bridge shelf <<<\n"
)
MAYA_MARKERS = ["# >>> asset-bridge shelf >>>"]

SP_SRC = os.path.join(PROJECT_ROOT, "plugins", "substance_painter", "asset-bridge")
SP_DST = r"C:\Users\12069\Documents\Adobe\Adobe Substance 3D Painter\plugins\asset-bridge"

# Toolbag 5：复制插件到 <安装目录>\data\plugin\asset_bridge（__main__.py 自动加载）
TOOLBAG_SRC = os.path.join(PROJECT_ROOT, "plugins", "toolbag", "asset_bridge")
# ZBrush 2026+：复制插件到 %APPDATA%\Maxon\<ver>\ZStartup\ZPlugs64\asset_bridge_zbrush
# （ZBrush 启动时扫描 ZPlugs64 自动加载；Python 插件需放此目录，与 ZData/Python
# 的 init.py 钩子配合）。安装器写入 ab_config.json，路径对齐 daemon 的 runtime\payloads。
ZBRUSH_SRC = os.path.join(PROJECT_ROOT, "plugins", "zbrush", "asset_bridge_zbrush")
TOOLBAG_EXE_CANDIDATES = [
    r"D:\DCC\Toolbag 5\toolbag.exe",
    r"G:\DCC\Toolbag 5\toolbag.exe",
    r"E:\DCC\Toolbag 5\toolbag.exe",
]

# Photoshop / SAI2 不装插件：仅把 exe 路径同步进守护配置（守护进程据此调用打开）
EDITOR_DAEMON_CFG = os.path.join(PROJECT_ROOT, "tools", "editor_daemon.json")
DCC_ROOTS = [r"D:\DCC", r"G:\DCC", r"E:\DCC", r"C:\DCC", r"C:\Program Files\Adobe"]
EDITOR_EXE_CANDIDATES = {
    "photoshop": [
        r"C:\Program Files\Adobe\Adobe Photoshop 2023\Photoshop.exe",
        r"D:\DCC\Adobe Photoshop 2023\Photoshop.exe",
    ],
    "sai2": [
        r"D:\DCC\[Saier]SAI Ver.2\sai2.exe",
        r"G:\DCC\[Saier]SAI Ver.2\sai2.exe",
    ],
}

# UE：引擎级插件（装到 <UE安装目录>/Engine/Plugins/AssetBridge，对所有工程通用）
UE_PLUGIN_SRC = os.path.join(PROJECT_ROOT, "plugins", "unreal", "AssetBridge")
# 候选引擎安装根目录（detect_ue_engines 会扫描这些位置）
UE_ENGINE_ROOTS = [
    r"G:\Engine",
    r"C:\Program Files\Epic Games",
]

# 复制目录时排除的内容
EXCLUDES = ("__pycache__",)


class Installer:
    """执行安装/预览，并负责日志与备份。"""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.backup_root = os.path.join(PROJECT_ROOT, "backups", "install_" + self.ts)
        self.log_path = os.path.join(PROJECT_ROOT, "logs", "install_" + self.ts + ".log")
        self.log_file = None
        self.changes = []   # 实际改动/新增的目标路径
        self.backups = []   # 备份存放路径
        if not dry_run:
            os.makedirs(self.backup_root, exist_ok=True)
            self.log_file = open(self.log_path, "w", encoding="utf-8")

    # -- 日志 ---------------------------------------------------------------
    def log(self, level, msg):
        line = "[%s] %s" % (level, msg)
        print(line)
        if self.log_file is not None:
            self.log_file.write(line + "\n")
            self.log_file.flush()

    # -- 工具 ---------------------------------------------------------------
    @staticmethod
    def _backup_name(target_path):
        """把任意绝对路径映射为备份目录下的唯一扁平文件名。"""
        return target_path.replace(":", "").replace("\\", "_").replace("/", "_")

    def _backup_if_exists(self, target_path):
        """若目标已存在，先备份（文件或目录）到备份目录；dry-run 仅打印。"""
        if not os.path.exists(target_path):
            return False
        dest = os.path.join(self.backup_root, self._backup_name(target_path))
        if self.dry_run:
            self.log("PLAN", "备份: %s -> %s" % (target_path, dest))
            return True
        if os.path.isdir(target_path):
            shutil.copytree(target_path, dest)
        else:
            shutil.copy2(target_path, dest)
        self.backups.append(dest)
        self.log("BACKUP", "%s -> %s" % (target_path, dest))
        return True

    @staticmethod
    def _read_file_text(path):
        """健壮读取文本：优先 utf-8 / utf-8-sig，回退 gbk，兼容 UTF-16 BOM。"""
        with open(path, "rb") as f:
            data = f.read()
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        if data[:3] == b"\xef\xbb\xbf":
            return data.decode("utf-8-sig")
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _write_text(path, text):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def _resolve_python_cmd(self):
        """决定 bat 里用 python 还是绝对路径。"""
        try:
            subprocess.run(
                ["python", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            return "python"
        except Exception:
            return '"' + SYSTEM_PYTHON + '"'

    # -- 复制 ---------------------------------------------------------------
    def safe_copy_dir(self, src, dst):
        """备份已有目标后，合并复制整个目录（排除 __pycache__）。"""
        if not os.path.isdir(src):
            self.log("FAIL", "源目录不存在: " + src)
            return
        self._backup_if_exists(dst)
        if self.dry_run:
            self.log("PLAN", "复制目录: %s -> %s" % (src, dst))
            return
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        ignore = shutil.ignore_patterns(*EXCLUDES)
        # dirs_exist_ok=True => 仅覆盖同名文件、新增文件，绝不删除目标原有文件
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
        self.changes.append(dst)
        self.log("OK", "复制目录完成: " + dst)

    def safe_copy_file(self, src, dst):
        """备份已有目标后，复制单个文件。"""
        if not os.path.isfile(src):
            self.log("FAIL", "源文件不存在: " + src)
            return
        self._backup_if_exists(dst)
        if self.dry_run:
            self.log("PLAN", "复制文件: %s -> %s" % (src, dst))
            return
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dst)
        self.changes.append(dst)
        self.log("OK", "复制文件完成: " + dst)

    # -- 步骤 ---------------------------------------------------------------
    def make_bat(self):
        bat_path = os.path.join(PROJECT_ROOT, "start_bridge_server.bat")
        python_cmd = self._resolve_python_cmd()
        content = BAT_TEMPLATE.format(python_cmd=python_cmd)
        self._backup_if_exists(bat_path)
        if self.dry_run:
            self.log("PLAN", "生成启动脚本: %s (python=%s)" % (bat_path, python_cmd))
            return
        with open(bat_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self.changes.append(bat_path)
        self.log("OK", "生成启动脚本: " + bat_path)

    # make_toolbag_bat 已移除（2026-10-16 用户纠正：Toolbag 5 自动扫描 Default
    # Plugin Folder <安装目录>\data\plugin\，双击图标即加载，无需任何启动器 bat；
    # 用户已确认磁盘上不存在该产物，纯代码清理）。

    def _cleanup_blender_mistakes(self):
        """清理历史错误安装，避免插件在列表里找不到 / 重复。

        1) 旧错误路径 D:\\DCC\\Blender5.0.1\\5.0\\scripts\\addons\\asset_bridge_blender
           ——该目录不在 Blender 5.0 搜索路径里，纯属无效残留；
        2) 用户误装的单个 __init__.py（addons 根目录下的单文件，Blender 不认）。
        两者都先备份再删除，绝不删除用户其它文件。
        """
        if os.path.isdir(OLD_BLENDER_DST):
            self._backup_if_exists(OLD_BLENDER_DST)
            if not self.dry_run:
                shutil.rmtree(OLD_BLENDER_DST)
            self.log("OK", "已清理旧错误路径: " + OLD_BLENDER_DST)
        if os.path.isfile(MISINIT_BLENDER):
            # 仅当它是我们误装的 asset_bridge 单文件才删（内容含 bl_info 或特征串）
            try:
                txt = self._read_file_text(MISINIT_BLENDER)
            except Exception:
                txt = ""
            if "Asset Bridge" in txt or "bl_info" in txt:
                self._backup_if_exists(MISINIT_BLENDER)
                if not self.dry_run:
                    os.remove(MISINIT_BLENDER)
                self.log("OK", "已清理误装单文件: " + MISINIT_BLENDER)
            else:
                self.log("SKIP", "根 addons/__init__.py 不像本插件误装文件，保留: " + MISINIT_BLENDER)

    def install_blender(self):
        self._cleanup_blender_mistakes()
        if not os.path.isdir(BLENDER_ADDONS_BASE):
            self.log("FAIL", "未找到 Blender 用户级插件目录: " + BLENDER_ADDONS_BASE)
            return
        installed = False
        for ver in sorted(os.listdir(BLENDER_ADDONS_BASE)):
            addons = os.path.join(BLENDER_ADDONS_BASE, ver, "scripts", "addons")
            if os.path.isdir(addons):
                self.safe_copy_dir(BLENDER_SRC, os.path.join(addons, "asset_bridge_blender"))
                installed = True
        if not installed:
            # 兜底：装到 5.0
            self.safe_copy_dir(
                BLENDER_SRC,
                os.path.join(BLENDER_ADDONS_BASE, "5.0", "scripts", "addons", "asset_bridge_blender"),
            )

    def install_watchdog(self):
        """部署守护脚本（已与项目同目录，确保存在并纳入部署清单）。"""
        src = os.path.join(PROJECT_ROOT, "server", "bridge_watchdog.ps1")
        if not os.path.isfile(src):
            self.log("SKIP", "守护脚本源文件不存在: " + src)
            return
        self.log("OK", "守护脚本已就位: " + src)

    def install_maya(self):
        self.safe_copy_dir(MAYA_PKG_SRC, MAYA_PKG_DST)
        target = None
        for cand in MAYA_USERSETUP_CANDIDATES:
            if os.path.isfile(cand):
                target = cand
                break
        if target is None:
            target = MAYA_USERSETUP_CANDIDATES[0]
        self._patch_usersetup(target, MAYA_STARTUP_CODE, MAYA_MARKERS)

    def _patch_usersetup(self, path, code, markers):
        """幂等地把启动代码追加到 userSetup.py（已含标记则跳过）。"""
        parent = os.path.dirname(path)
        if not self.dry_run:
            os.makedirs(parent, exist_ok=True)
        content = ""
        if os.path.isfile(path):
            content = self._read_file_text(path)
        if any(m in content for m in markers):
            self.log("SKIP", "userSetup 已含启动代码，跳过: " + path)
            return
        self._backup_if_exists(path)
        if self.dry_run:
            self.log("PLAN", "写入 userSetup 启动代码: " + path)
            return
        mode = "a" if content else "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write(code)
        self.changes.append(path)
        self.log("OK", "已写入 userSetup 启动代码: " + path)

    def install_substance_painter(self):
        self.safe_copy_dir(SP_SRC, SP_DST)

    # -- 检测器（限深扫描，避免在大目录上拖慢安装器） ------------------------
    @staticmethod
    def _scan_for_exe(name, roots, max_depth=4):
        name_l = name.lower()
        for r in roots:
            if not os.path.isdir(r):
                continue
            for dp, dirs, fns in os.walk(r):
                depth = dp[len(r):].count(os.sep)
                if depth > max_depth:
                    del dirs[:]
                    continue
                for f in fns:
                    if f.lower() == name_l:
                        return os.path.join(dp, f)
        return None

    def _detect_toolbag_exe(self):
        for c in TOOLBAG_EXE_CANDIDATES:
            if os.path.isfile(c):
                return c
        return self._scan_for_exe("toolbag.exe", DCC_ROOTS)

    def _detect_editor_exe(self, key):
        for c in EDITOR_EXE_CANDIDATES.get(key, []):
            if os.path.isfile(c):
                return c
        subdir = r"C:\Program Files\Adobe" if key == "photoshop" else None
        roots = ([subdir] if subdir else []) + DCC_ROOTS
        return self._scan_for_exe(
            "Photoshop.exe" if key == "photoshop" else "sai2.exe", roots
        )

    def install_toolbag(self):
        """复制 Toolbag 适配器到 <安装目录>\\data\\plugin\\asset_bridge。"""
        exe = self._detect_toolbag_exe()
        if not exe:
            self.log("SKIP", "未检测到 Marmoset Toolbag 5（可手动指定后由托盘安装器部署）。")
            return
        target = os.path.join(os.path.dirname(exe), "data", "plugin", "asset_bridge")
        self.safe_copy_dir(TOOLBAG_SRC, target)

    @staticmethod
    def _detect_zbrush_zstartup():
        """扫描 %APPDATA%\\Maxon\\<ver>\\ZStartup\\ZPlugs64 作为插件部署目录。"""
        maxon = os.path.join(os.environ.get("APPDATA", ""), "Maxon")
        if not os.path.isdir(maxon):
            return None
        for name in sorted(os.listdir(maxon)):
            cand = os.path.join(maxon, name, "ZStartup", "ZPlugs64")
            if os.path.isdir(cand):
                return cand
        return None

    def install_zbrush(self):
        """部署 ZBrush 适配器到 ZStartup\\ZPlugs64\\asset_bridge_zbrush，并写入
        ab_config.json（路径对齐 daemon 的 runtime\\payloads，统一为复数 payloads）。

        根因修复（2026-10-16 P0）：此前 install.py 没有 ZBrush 步骤，插件 .py 从没被
        同步到 ZStartup，用户只能手动改 ab_config.json（且改了 .py 也没更新，停在旧版）。
        本步骤一次性解决「代码不部署 / 配置手动改 / 单复数路径错乱」三件事：
          - 复制 plugins/zbrush/asset_bridge_zbrush 整个目录到 ZPlugs64；
          - 写/合并 ab_config.json：payload_dir 用绝对路径指向本仓库 runtime\\payloads
            （与 daemon 的 GET /config 返回的 payload_dir 一致，ZBrush inbox/outbox
            才能和 daemon 对接），inbox_dir/outbox_dir 随之对齐；其余键保留用户/默认。
        """
        dst = self._detect_zbrush_zstartup()
        if not dst:
            self.log("SKIP", "未检测到 ZBrush ZStartup 目录（%APPDATA%\\Maxon\\*\\ZStartup\\"
                             "ZPlugs64）。请先安装并运行一次 ZBrush 让目录生成，"
                             "或手动指定 ZPlugs64 路径后重跑本安装器。")
            return
        target = os.path.join(dst, "asset_bridge_zbrush")
        self.safe_copy_dir(ZBRUSH_SRC, target)
        self._write_zbrush_config(target)

    def _write_zbrush_config(self, plugin_dir):
        """写/合并 ZBrush 插件的 ab_config.json。

        路径类键（server_url/payload_dir/inbox_dir/outbox_dir）强制对齐本仓库的
        runtime\\payloads（与 daemon 一致）；其余键（poll_interval/default_to/超时等）
        若已存在则保留，避免覆盖用户自定义。绝对路径是因为插件装在 ZStartup，
        无法用相对路径引用仓库 runtime。
        """
        payload = os.path.join(RUNTIME_ROOT, "payloads")
        inbox = os.path.join(payload, "zbrush_inbox")
        outbox = os.path.join(payload, "zbrush_outbox")
        cfg_path = os.path.join(plugin_dir, "ab_config.json")
        data = {}
        if os.path.isfile(cfg_path):
            try:
                data = json.loads(self._read_file_text(cfg_path))
            except Exception:
                data = {}
        data["server_url"] = data.get("server_url") or "http://127.0.0.1:27631"
        data["payload_dir"] = payload
        data["inbox_dir"] = inbox
        data["outbox_dir"] = outbox
        for k, v in {
            "poll_interval": 2.0, "default_to": "blender",
            "import_settle_sec": 1.0, "import_wait_sec": 15.0,
            "upload_timeout": 300.0, "auto_max_sec": 1800.0,
        }.items():
            if k not in data:
                data[k] = v
        self._backup_if_exists(cfg_path)
        if self.dry_run:
            self.log("PLAN", "生成/更新 ZBrush 配置(路径对齐 runtime\\payloads): " + cfg_path)
            return
        self._write_text(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
        self.changes.append(cfg_path)
        self.log("OK", "已生成/更新 ZBrush 配置(路径对齐 runtime\\payloads): " + cfg_path)

    def sync_editor_daemon_config(self):
        """把检测到的 PS / SAI2 路径同步进 tools\\editor_daemon.json（守护进程读取）。

        仅更新 apps 下 photoshop / sai2 两个键，保留 server_url 与其它已有键，绝不整文件覆盖。
        """
        cfg_path = EDITOR_DAEMON_CFG
        data = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        apps = dict(data.get("apps", {}))
        changed = False
        for key in ("photoshop", "sai2"):
            exe = self._detect_editor_exe(key)
            if exe and os.path.isfile(exe) and apps.get(key) != exe:
                apps[key] = exe
                changed = True
        if not changed:
            self.log("SKIP", "editor_daemon.json 无需更新。")
            return
        data["server_url"] = data.get("server_url", "http://127.0.0.1:27631")
        data["apps"] = apps
        self._backup_if_exists(cfg_path)
        if self.dry_run:
            self.log("PLAN", "更新编辑器守护配置: " + cfg_path)
            return
        self._write_text(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
        self.changes.append(cfg_path)
        self.log("OK", "已同步编辑器守护配置: " + cfg_path)

    def install_unreal(self):
        """把 AssetBridge 作为引擎级插件装到每个检测到的 UE 引擎：

        <UE安装目录>/Engine/Plugins/AssetBridge

        引擎级插件对所有使用该版本引擎的工程通用，无需再改工程的 DefaultEngine.ini。
        安装后重启编辑器（或在 Plugins 浏览器里启用 Asset Bridge）即可。
        """
        engines = self.detect_ue_engines()
        if not engines:
            self.log("FAIL", "未检测到任何 UE 引擎（扫描了 %s 与注册表）。" % ", ".join(UE_ENGINE_ROOTS))
            return
        for eng in engines:
            dst = os.path.join(eng, "Engine", "Plugins", "AssetBridge")
            self.safe_copy_dir(UE_PLUGIN_SRC, dst)

    def detect_ue_engines(self):
        """扫描本机已安装的 UE 引擎目录（注册表 + 常见安装根）。"""
        found = []
        # 注册表：HKLM\SOFTWARE\EpicGames\Unreal Engine\<ver>\InstalledDirectory
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\Unreal Engine")
            i = 0
            while True:
                sub = winreg.EnumKey(key, i)
                i += 1
                try:
                    sk = winreg.OpenKey(key, sub)
                    d = winreg.QueryValueEx(sk, "InstalledDirectory")[0]
                    if d and os.path.isdir(d):
                        found.append(d)
                except OSError:
                    pass
        except OSError:
            pass
        # 常见安装根目录
        for root in UE_ENGINE_ROOTS:
            if os.path.isdir(root):
                for name in os.listdir(root):
                    if re.match(r"^UE[_ ]?\d", name, re.IGNORECASE):
                        d = os.path.join(root, name)
                        if os.path.isdir(os.path.join(d, "Engine", "Binaries", "Win64")):
                            found.append(d)
        # 去重并排序
        return sorted(set(found))

    def ensure_ini_section(self, ini_path, section, required_lines):
        """确保 ini 含指定段；段已存在则补齐缺失行，不存在则追加整段。"""
        self._backup_if_exists(ini_path)
        if self.dry_run:
            self.log("PLAN", "配置段: %s -> %s" % (section, ini_path))
            return
        raw = self._read_file_text(ini_path) if os.path.isfile(ini_path) else ""
        sep = "\r\n" if "\r\n" in raw else "\n"
        if raw == "":
            lines = []
        else:
            lines = raw.split("\r\n") if "\r\n" in raw else raw.split("\n")
        idx = -1
        for i, ln in enumerate(lines):
            if ln.strip() == section.strip():
                idx = i
                break
        if idx == -1:
            new_block = [section] + list(required_lines)
            body = raw if raw else ""
            if body and not body.endswith("\n"):
                body += "\n"
            body += sep.join(new_block) + sep
            self._write_text(ini_path, body)
            self.changes.append(ini_path)
            self.log("OK", "已追加配置段 %s: %s" % (section, ini_path))
            return
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            if lines[j].lstrip().startswith("["):
                end = j
                break
        section_lines = lines[idx:end]
        missing = [
            rl for rl in required_lines
            if not any(rl.strip() == sl.strip() for sl in section_lines)
        ]
        if not missing:
            self.log("SKIP", "配置段 %s 已含所需行，跳过: %s" % (section, ini_path))
            return
        insert_at = idx + 1
        for ml in missing:
            lines.insert(insert_at, ml)
            insert_at += 1
        body = sep.join(lines)
        if not body.endswith(sep):
            body += sep
        self._write_text(ini_path, body)
        self.changes.append(ini_path)
        self.log("OK", "已补入缺失配置行 %s: %s" % (missing, ini_path))

    # -- 主流程 -------------------------------------------------------------
    def run(self):
        self.log("INFO", "=== Asset Bridge 安装器 (dry_run=%s) ===" % self.dry_run)
        self.log("INFO", "PROJECT_ROOT = " + PROJECT_ROOT)
        steps = [
            ("生成中转服务启动脚本", self.make_bat),
            ("Blender 适配器", self.install_blender),
            ("Maya 适配器", self.install_maya),
            ("Substance Painter 适配器", self.install_substance_painter),
            ("Marmoset Toolbag 适配器", self.install_toolbag),
            ("ZBrush 适配器", self.install_zbrush),
            ("Unreal 适配器", self.install_unreal),
            ("同步编辑器守护配置(PS/SAI2)", self.sync_editor_daemon_config),
            ("部署守护脚本(watchdog)", self.install_watchdog),
        ]
        for name, fn in steps:
            try:
                self.log("STEP", "开始: " + name)
                fn()
            except Exception as exc:  # noqa: BLE001 - 单步失败不影响其它步骤
                self.log("FAIL", "%s 失败: %s" % (name, exc))
                self.log("FAIL", traceback.format_exc())
        self._summary()

    def _summary(self):
        self.log("INFO", "---- 汇总 ----")
        self.log("INFO", "改动/新增目标 (%d):" % len(self.changes))
        for p in self.changes:
            self.log("INFO", "  + " + p)
        self.log("INFO", "备份 (%d):" % len(self.backups))
        for p in self.backups:
            self.log("INFO", "  * " + p)
        if not self.dry_run:
            self.log("INFO", "安装日志: " + self.log_path)
            self.log("INFO", "备份目录: " + self.backup_root)
        self.log("INFO", "完成。")


def main():
    parser = argparse.ArgumentParser(description="Asset Bridge 一键安装器")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将改动的路径清单，不写任何文件、不备份",
    )
    args = parser.parse_args()
    installer = Installer(dry_run=args.dry_run)
    try:
        installer.run()
    finally:
        if installer.log_file is not None:
            installer.log_file.close()


if __name__ == "__main__":
    main()
