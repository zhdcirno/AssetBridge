# Asset Bridge 中转服务守护脚本 (watchdog)
# ---------------------------------------------------------------------------
# 循环检测 127.0.0.1:27631 是否在监听；
# 不通则用系统 Python 拉起 server\server.py --root runtime；
# 每 10 秒检测一次；带时间戳的日志写入 logs\watchdog.log。
#
# 用法：
#   start_bridge_server.bat --watch      （推荐，进入守护模式）
#   或直接： powershell -ExecutionPolicy Bypass -File bridge_watchdog.ps1
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'SilentlyContinue'

# 系统 Python（与 install.py / 中转服务一致）
$PYTHON = 'C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe'
$PORT = 27631

# 脚本所在目录即项目根
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SERVER = Join-Path $ROOT 'server\server.py'
$RUNTIME = Join-Path $ROOT 'runtime'
$LOGDIR = Join-Path $ROOT 'logs'
$LOG = Join-Path $LOGDIR 'watchdog.log'

if (-not (Test-Path $LOGDIR)) { New-Item -ItemType Directory -Path $LOGDIR | Out-Null }

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts [watchdog] $msg" | Out-File -Append -Encoding utf8 $LOG
    Write-Host "$ts [watchdog] $msg"
}

# 探测端口是否在监听（带 1.5s 超时，避免卡住）
function Test-PortListening($port) {
    $tcp = $null
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $ar = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(1500, $false)
        if ($ok -and $tcp.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        if ($tcp -ne $null) {
            try { $tcp.Close() } catch { }
        }
    }
}

Log "守护脚本启动（检测端口 $PORT，间隔 10s）"

while ($true) {
    if (Test-PortListening $PORT) {
        # 已在监听（可能是本脚本启动的，也可能是用户手动启动的主实例）
        Start-Sleep -Seconds 10
        continue
    }

    Log "端口 $PORT 未监听，尝试启动中转服务..."
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $PYTHON
        $psi.Arguments = "`"$SERVER`" --root `"$RUNTIME`""
        $psi.WorkingDirectory = $ROOT
        $psi.WindowStyle = 'Hidden'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $false
        $psi.RedirectStandardError = $false
        $p = [System.Diagnostics.Process]::Start($psi)
        if ($p -ne $null) {
            Log "已启动中转服务 (PID=$($p.Id))"
        } else {
            Log "启动失败：Process 为 null"
        }
    } catch {
        Log "启动失败：$_"
    }
    # 给服务一点启动时间，再进入下一轮检测
    Start-Sleep -Seconds 10
}

