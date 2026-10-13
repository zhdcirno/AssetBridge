# -*- coding: utf-8 -*-
"""托盘新增功能无头自检：Find-Toolbag / Find-Photoshop / Find-Sai2 / Find-ZBrush /
Resolve-ManualApp（新 key）/ Update-EditorDaemonConfig / 菜单结构 / 守护状态
+ **启动路径运行时冒烟**（抓 ParseFile 抓不到的 MethodNotFound 类错误）。

切片自检做法：把 assetbridge_tray.ps1 切两段再拼接成可 dot-source 的切片：
   head：常量 + Add-Type + 辅助函数 + trap（改为 continue，非致命）
   tail：从「function Exit-Tray」起到「# ---- 启动」之前的所有函数 + 顶层 GUI 对象
中间的单实例检查（互斥量 + exit）必须跳过——真实托盘若正在运行，它会直接 exit，
后面所有函数都不会定义。不进消息循环、不弹窗。

运行时冒烟（2026-10-13 教训：add_Popup 事件在 ContextMenuStrip 上不存在，
ParseFile 0 错误但托盘一启动就崩）：原样执行托盘脚本到 Application.Run() 之前的
全部顶层语句，把 Run() 换成有界 DoEvents pump，再执行原收尾语句。判定：
  - 子进程正常结束；
  - tray.log 期间没有新增「未处理异常」；
  - 至少出现一条新的「托盘启动」。
副作用说明：若中转服务没在运行，冒烟会把服务拉起来（与真实托盘行为一致）。
"""
import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PS1 = os.path.join(ROOT, "tray", "assetbridge_tray.ps1")
CFG = os.path.join(ROOT, "tools", "editor_daemon.json")

SRC = io.open(PS1, encoding="utf-8-sig").read()

# head：从开头到单实例检查之前（不含 $script:ReallyExit 那一段）
head_end = SRC.index("$script:ReallyExit = $false")
# tail：从 Exit-Tray 函数起，到「# ---- 启动」之前
tail_start = SRC.index("function Exit-Tray {")
end_mark = "# ---- 启动 "
tail_end = SRC.index(end_mark)

head = SRC[:head_end]
# trap 改为非致命：顶层 GUI 语句若在无头环境失败，继续定义后续函数
head = head.replace("trap {\n    try { Log ('未处理异常: {0}' -f $_.Exception.Message) } catch { }\n    break\n}",
                    "trap {\n    continue\n}")
if "trap {" in head and "break" in head:
    # 兜底：正则替换任何 trap { ... break } 形式
    import re
    head = re.sub(r"trap\s*\{.*?break\s*\}", "trap {\n    continue\n}", head, flags=re.S)

tail = SRC[tail_start:tail_end]
slice_path = os.path.join(HERE, "_tray_slice.ps1")
# .ps1 必须带 BOM（PS 5.1 无 BOM 按 GBK 读，中文注释会乱码/报错）
with io.open(slice_path, "w", encoding="utf-8-sig") as fh:
    fh.write(head + "\n" + tail)

# 测试脚本：调用各函数并把结果 JSON 化输出
TEST = r'''
$ErrorActionPreference = 'Stop'
$script:ManualPaths = @{}          # Load-Config 没跑，手动补默认值
$script:IApps = @()

$out = [ordered]@{}

# ---- 三个探测器（本机三个软件都在 D 盘，应都能探测到） ----
$out.find_toolbag    = $(try { $t = Find-Toolbag;    if ($t) { @{ ok=$t.ok; ver=$t.version; path=$t.path; targets=@($t.targets) } } else { $null } } catch { @{ err = "" + $_ } })
$out.find_photoshop  = $(try { $t = Find-Photoshop;  if ($t) { @{ ok=$t.ok; ver=$t.version; path=$t.path; noinstall=$t.noinstall } } else { $null } } catch { @{ err = "" + $_ } })
$out.find_sai2       = $(try { $t = Find-Sai2;       if ($t) { @{ ok=$t.ok; ver=$t.version; path=$t.path; noinstall=$t.noinstall } } else { $null } } catch { @{ err = "" + $_ } })

# ---- ZBrush 探测 / 解析（本机 D:\DCC\Zbrush2026.2.1，用户资产目录在 AppData\Maxon） ----
$out.find_zbrush = $(try { $t = Find-ZBrush; if ($t) { @{ ok=$t.ok; ver=$t.version; path=$t.path; targets=@($t.targets); note=[string]$t.note } } else { $null } } catch { @{ err = "" + $_ } })
$zb = 'D:\DCC\Zbrush2026.2.1\ZBrush.exe'
if (Test-Path -LiteralPath $zb) {
    $r = Resolve-ManualApp 'zbrush' $zb
    $out.resolve_zbrush = @{ ok = [bool]$r.ok; ver = $r.version; src = $r.src; min = $r.min; target0 = @($r.targets)[0] }
} else { $out.resolve_zbrush = 'NOT-INSTALLED' }
$out.resolve_zbrush_bad = $($null -eq (Resolve-ManualApp 'zbrush' 'C:\Windows\notepad.exe'))
# 用户目录版本过滤：2024 旧版用户目录不应被匹配（大小写 [Bb]rush 正则 + 版本门槛）
$out.zb_userdir_2026 = $(try { [string](Get-ZBrushUserDirByVersion '2026.2.1') } catch { '' })
$out.zb_userdir_2024 = $(try { if (Get-ZBrushUserDirByVersion '2024.0.4') { 'FOUND(不该)' } else { 'correctly-skipped' } } catch { '' })

# ---- 菜单结构：编辑器守护三项应在 ----
$out.menu_has_editor = $($miEdState -ne $null -and $miEdStart -ne $null -and $miEdStop -ne $null)
$out.menu_item_count = $($menu.Items.Count)

# ---- Resolve-ManualApp：无效路径返回 $null ----
$out.resolve_bad     = $($null -eq (Resolve-ManualApp 'toolbag' 'C:\Windows\notepad.exe'))
$out.resolve_bad2    = $($null -eq (Resolve-ManualApp 'photoshop' 'C:\Windows\notepad.exe'))

# ---- 真实路径解析（本机三个软件都在 D 盘） ----
$tb = 'D:\DCC\Toolbag 5\toolbag.exe'
if (Test-Path -LiteralPath $tb) {
    $r = Resolve-ManualApp 'toolbag' $tb
    $out.resolve_toolbag = @{ ok = [bool]$r.ok; ver = $r.version; target0 = @($r.targets)[0]; src = $r.src }
} else { $out.resolve_toolbag = 'NOT-INSTALLED' }

$ps = 'D:\DCC\Adobe Photoshop 2023\Photoshop.exe'
if (Test-Path -LiteralPath $ps) {
    $r = Resolve-ManualApp 'photoshop' $ps
    $out.resolve_photoshop = @{ ok = [bool]$r.ok; ver = $r.version; noinstall = [bool]$r.noinstall; targets = @($r.targets).Count }
} else { $out.resolve_photoshop = 'NOT-INSTALLED' }

$sai = 'D:\DCC\[Saier]SAI Ver.2\sai2.exe'
if (Test-Path -LiteralPath $sai) {
    $r = Resolve-ManualApp 'sai2' $sai
    $out.resolve_sai2 = @{ ok = [bool]$r.ok; ver = $r.version; noinstall = [bool]$r.noinstall }
} else { $out.resolve_sai2 = 'NOT-INSTALLED' }

# ---- 目录形式的手动指定（给文件夹也行） ----
if (Test-Path -LiteralPath 'D:\DCC\Toolbag 5') {
    $r = Resolve-ManualApp 'toolbag' 'D:\DCC\Toolbag 5'
    $out.resolve_toolbag_dir = $($null -ne $r -and $r.ok)
}

# ---- Update-EditorDaemonConfig：手动路径优先写进 editor_daemon.json ----
$script:ManualPaths['photoshop'] = $ps
$script:ManualPaths['sai2'] = $sai
if (Test-Path -LiteralPath 'D:\DCC\Zbrush2026.2.1\ZBrush.exe') { $script:ManualPaths['zbrush'] = 'D:\DCC\Zbrush2026.2.1\ZBrush.exe' }
Update-EditorDaemonConfig
$cfgPath = Join-Path $ROOT 'tools\editor_daemon.json'
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$out.daemon_cfg_photoshop = [string]$cfg.apps.photoshop
$out.daemon_cfg_sai2 = [string]$cfg.apps.sai2
$out.daemon_cfg_zbrush = [string]$cfg.apps.zbrush
$out.daemon_cfg_server = [string]$cfg.server_url

# ---- Get-EditorDaemonStatusText：未运行时应给明确文案 ----
$out.ed_status_text = Get-EditorDaemonStatusText
$out.ed_pid = Get-EditorDaemonPid

$out | ConvertTo-Json -Depth 6
'''

test_path = os.path.join(HERE, "_tray_slice_test.ps1")
with io.open(test_path, "w", encoding="utf-8-sig") as fh:
    fh.write(TEST)

runner = os.path.join(HERE, "_tray_slice_run.ps1")
with io.open(runner, "w", encoding="utf-8-sig") as fh:
    fh.write(". '{0}'\n. '{1}'\n".format(slice_path, test_path))

# 备份 editor_daemon.json，避免测试写入污染真实配置
backup = None
if os.path.exists(CFG):
    backup = CFG + ".bak"
    shutil.copy2(CFG, backup)

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
proc = subprocess.run(
    [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
     "-File", runner],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
out = proc.stdout.decode("utf-8", "replace")

# 还原 editor_daemon.json
if backup and os.path.exists(backup):
    shutil.move(backup, CFG)

for p in (runner, test_path, slice_path):
    try:
        os.remove(p)
    except OSError:
        pass
print(out)
slice_rc = proc.returncode


# ---------------------------------------------------------------------------
# 启动路径运行时冒烟：真跑一遍（有界消息泵），抓 MethodNotFound 类运行时错误
# ---------------------------------------------------------------------------

TRAY_LOG = os.path.join(ROOT, "logs", "tray.log")


def _log_size():
    try:
        return os.path.getsize(TRAY_LOG)
    except OSError:
        return 0


def _log_delta(before_size):
    try:
        with open(TRAY_LOG, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(before_size)
            return fh.read()
    except OSError:
        return ""


smoke_wrapper = os.path.join(HERE, "_tray_smoke_run.ps1")
smoke_src = io.open(PS1, encoding="utf-8-sig").read()
run_mark = "[System.Windows.Forms.Application]::Run()"
ri = smoke_src.index(run_mark)
pump = ("\n# ==== 运行时冒烟：有界消息泵（替代 Application.Run）====\n"
        "$smokeStart = Get-Date\n"
        "while (((Get-Date) - $smokeStart).TotalSeconds -lt 12) {\n"
        "    [System.Windows.Forms.Application]::DoEvents()\n"
        "    Start-Sleep -Milliseconds 100\n"
        "}\n")
smoke_body = smoke_src[:ri] + pump + smoke_src[ri + len(run_mark):]
with io.open(smoke_wrapper, "w", encoding="utf-8-sig") as fh:
    fh.write(smoke_body)

log_before = _log_size()
smoke_proc = subprocess.run(
    [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
     "-File", smoke_wrapper],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150)
delta = _log_delta(log_before)
try:
    os.remove(smoke_wrapper)
except OSError:
    pass

problems = []
if smoke_proc.returncode != 0:
    problems.append("冒烟子进程退出码 {0}: {1}".format(
        smoke_proc.returncode,
        smoke_proc.stdout.decode("utf-8", "replace")[-800:]))
if "未处理异常" in delta:
    problems.append("启动路径出现未处理异常:\n" + delta[-800:])
if "托盘启动 port=" not in delta:
    problems.append("未见到新的「托盘启动」日志，启动路径可能没跑到底")

print("=== runtime smoke ===")
print("subprocess_rc={0} new_log_bytes={1}".format(smoke_proc.returncode, len(delta)))
if delta:
    print("--- new tray.log lines ---")
    print(delta[-600:])
if problems:
    print("RUNTIME_SMOKE_FAIL:")
    for p in problems:
        print("  " + p)
    sys.exit(1)
print("RUNTIME_SMOKE_OK")
sys.exit(slice_rc)
