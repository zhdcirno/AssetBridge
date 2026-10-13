# ============================================================================
#  Asset Bridge - 托盘守护程序 (tray supervisor)
#
#  * 常驻系统托盘，没有任何窗口
#  * 图标四态：绿=运行中 / 琥珀=掉线(守护会自动拉起) / 灰=运行中但守护暂停 / 红=已停止
#  * 右键菜单：启动 / 停止 / 重启 / 暂停守护 / 编辑器守护（PS/SAI2 回传）/
#    打开日志 / 打开项目文件夹 / 设置 / 安装插件 / 退出
#  * 双击图标 = 打开状态小窗（关闭后缩回托盘，不退出）
#  * 每隔 N 秒自检，服务掉了自动拉起（可用"暂停守护"关掉）
#  * 由 assetbridge_tray_launch.vbs 以「窗口样式 0」启动（零窗口，无 cmd 常驻）
#
#  日志：E:\CirnoWorkSpace\AssetBridge\logs\tray.log
#  服务日志：E:\CirnoWorkSpace\AssetBridge\logs\server_console.log
#  配置：E:\CirnoWorkSpace\AssetBridge\tray\tray_config.json
# ============================================================================
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ---- 藏掉自己的控制台窗口（双保险）----------------------------------------
# 正常路径：由 assetbridge_tray_launch.vbs 以「窗口样式 0」启动，本来就没有窗口。
# 万一有人直接双击本 .ps1，这一段会把控制台立刻藏起来。
try {
    $sig = '[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();' +
           '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
    $w32 = Add-Type -MemberDefinition $sig -Name 'AbTrayCon' -Namespace 'AbTray' -PassThru
    $h = $w32::GetConsoleWindow()
    if ($h -ne [IntPtr]::Zero) { [void]$w32::ShowWindow($h, 0) }
} catch { }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ROOT      = Split-Path -Parent $ScriptDir
$PYTHON    = 'C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe'
$PYTHONW   = Join-Path (Split-Path -Parent $PYTHON) 'pythonw.exe'
$SERVER    = Join-Path $ROOT 'server\server.py'
$RUNSERVER = Join-Path $ScriptDir 'run_server.py'
$RUNTIME   = Join-Path $ROOT 'runtime'
# PS / SAI2 外部编辑器守护进程（保存贴图后自动回传给发送端）
$EDITOR_DAEMON = Join-Path $ROOT 'tools\editor_daemon.py'
$EDITOR_CFG    = Join-Path $ROOT 'tools\editor_daemon.json'
$EDITOR_STATE  = Join-Path $RUNTIME 'editor_daemon'   # pid / stop.flag / status.json
$LOGDIR    = Join-Path $ROOT 'logs'
$LOG       = Join-Path $LOGDIR 'tray.log'
$SRVLOG    = Join-Path $LOGDIR 'server_console.log'
$CONFIG    = Join-Path $ScriptDir 'tray_config.json'
$LOCKFILE  = Join-Path $LOGDIR 'tray.lock'
$ICONSRC   = Join-Path $ROOT 'assets\icons\png\16'   # 状态窗按钮用的小图标（16px）
$APPICON   = Join-Path $ROOT 'assets\icons\ico\app_logo.ico'

# ⚠️ 图标实际位于 tray\icons\。之前只写 'icon_ok.ico' 会去找 tray\icon_ok.ico ->
#    找不到 -> 退回系统图标，表现为「运行中(绿)显示成蓝色 i / 掉线显示成琥珀感叹号」。
$ICON_OK     = Join-Path $ScriptDir 'icons\icon_ok.ico'       # 绿：运行中
$ICON_DOWN   = Join-Path $ScriptDir 'icons\icon_down.ico'     # 红：已停止（守护已暂停，不会自动拉起）
$ICON_WARN   = Join-Path $ScriptDir 'icons\icon_warn.ico'     # 琥珀：掉线，守护会自动拉起
$ICON_PAUSED = Join-Path $ScriptDir 'icons\icon_paused.ico'   # 灰：运行中但守护被暂停
# 图标来源：assets\icons\（由 tools\gen_icons.py 生成，可改配色后重新生成）

# ---- 窗口图标（标题栏 + 任务栏）--------------------------------------------
# 关键坑：WinForms 的 Form.Icon 只设置「大图标」(标题栏左上角)，
# 任务栏 / Alt-Tab 用的是窗口的「小图标」，不会自动从 Icon 派生。
# 不显式设置 -> 任务栏一律回退到 powershell.exe 的默认图标。
# 解决：用 WM_SETICON 显式设置 ICON_SMALL(0) 与 ICON_BIG(1)。
# 用 CopyIcon 复制一份句柄交给窗口，避免与 Icon 对象自身的句柄重复释放。
try {
    Add-Type -MemberDefinition @'
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr CopyIcon(IntPtr hIcon);
'@ -Name 'AbWinIcon' -Namespace 'AbTray' -ErrorAction SilentlyContinue
} catch { }

$script:AppLogo = $null
if (Test-Path $APPICON) {
    try { $script:AppLogo = New-Object System.Drawing.Icon -ArgumentList $APPICON } catch { }
}

function Set-FormIcon([System.Windows.Forms.Form]$form) {
    if ($script:AppLogo -eq $null) { return }
    try {
        if ($form.IsHandleCreated -eq $false) { $form.CreateControl() }
        $hBig   = [AbTray.AbWinIcon]::CopyIcon($script:AppLogo.Handle)
        $hSmall = [AbTray.AbWinIcon]::CopyIcon($script:AppLogo.Handle)
        if ($hBig   -ne [IntPtr]::Zero) { [void][AbTray.AbWinIcon]::SendMessage($form.Handle, 0x0080, [IntPtr]::new(1), $hBig) }
        if ($hSmall -ne [IntPtr]::Zero) { [void][AbTray.AbWinIcon]::SendMessage($form.Handle, 0x0080, [IntPtr]::Zero, $hSmall) }
    } catch { }
}

function Log([string]$m) {
    $line = ('{0} [tray] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
    try {
        if (-not (Test-Path $LOGDIR)) { New-Item -ItemType Directory -Path $LOGDIR -Force | Out-Null }
        $line | Out-File -Append -Encoding utf8 $LOG
    } catch { }
}

function Get-AbImage([string]$file, [string]$src = $ICONSRC) {
    # 状态窗按钮上的小图标。读不到就返回 $null（按钮不显示图标，不影响功能）。
    # $src 默认 $ICONSRC（assets\icons\png\16）；安装器行内 20px 图标传 tray\icons。
    #
    # 注意：不能用 Image.FromFile —— 它会一直**锁住文件**，导致生成脚本无法覆盖重写。
    # 这里读成字节流再拷一份 Bitmap，既不锁文件也不受流生命周期影响。
    try {
        $p = Join-Path $src $file
        if (-not (Test-Path $p)) { return $null }
        $bytes = [System.IO.File]::ReadAllBytes($p)
        $ms = New-Object System.IO.MemoryStream(,$bytes)
        try {
            $tmp = [System.Drawing.Image]::FromStream($ms)
            $img = New-Object System.Drawing.Bitmap($tmp)   # 独立副本
            $tmp.Dispose()
            return $img
        } finally { $ms.Dispose() }
    } catch { }
    return $null
}

function New-AbButton($parent, [string]$text, [string]$icon, [int]$x, [int]$w) {
    # 带图标的状态窗按钮（图标放左侧、文字靠右）
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.SetBounds($x, 274, $w, 32)
    $img = Get-AbImage $icon
    if ($img -ne $null) {
        $b.Image      = $img
        $b.ImageAlign = 'MiddleLeft'
        $b.TextAlign  = 'MiddleRight'
    }
    [void]$parent.Controls.Add($b)
    return $b
}

# 隐藏窗口下如果发生未处理异常是「静默无声」的（没有控制台可看），
# 所以兜底写进日志，便于事后定位。
trap {
    try { Log ('未处理异常: {0}' -f $_.Exception.Message) } catch { }
    break
}

# ---- 单实例保护 -----------------------------------------------------------
# 互斥量 + PID 锁文件双保险：
# 互斥量只说明"曾经有人持有着"，但旧实例若没正常退出（比如被 FormClosing 卡住），
# 它会一直存在，于是新实例永远起不来。所以还要用锁文件里的 PID 复核一下：
# 那个进程已经不在了 -> 判定为残留，本次接管。
$script:ReallyExit = $false
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\AssetBridgeTraySingleton', [ref]$createdNew)
if (-not $createdNew) {
    $stale = $true
    if (Test-Path $LOCKFILE) {
        try {
            $oldPid = [int]((Get-Content $LOCKFILE -Raw).Trim())
            $null = Get-Process -Id $oldPid -ErrorAction Stop
            $stale = $false          # 进程还活着 -> 确实是重复启动
        } catch { $stale = $true }
    }
    if (-not $stale) {
        Log '托盘已在运行，退出本次启动'
        exit
    }
    Log ('发现残留的互斥量（旧 pid 已不存在）-> 本次接管')
}
try { Set-Content -Path $LOCKFILE -Value $PID -Encoding ascii } catch { }

function Exit-Tray {
    # 真正退出：必须先置位，否则状态窗的 FormClosing 会把关闭动作取消掉，
    # 导致 Application.Exit() 无效、进程永远退不出去（图标已隐藏 => 看起来像退了）。
    $script:ReallyExit = $true
    try { if ($script:Form -ne $null) { $script:Form.Dispose() } } catch { }
    try { $ni.Visible = $false } catch { }
    try { $mutex.ReleaseMutex() } catch { }
    try { $mutex.Dispose() } catch { }
    try {
        foreach ($k in @($script:IconCache.Keys)) {
            try { $script:IconCache[$k].icon.Dispose() } catch { }
        }
        $script:IconCache = @{}
    } catch { }
    try { if (Test-Path $LOCKFILE) { Remove-Item $LOCKFILE -Force } } catch { }
    [System.Windows.Forms.Application]::Exit()
}

$script:Port     = 27631
$script:Interval = 10
$script:Guard    = $true
$script:LastUp   = $false
$script:PayloadDir = ''    # 中转文件路径（负载目录），自动从服务端 /api/v1/config 取
$script:AbNotifyPath = ''  # D3：最近一次 ZBrush 接收的文件路径（气泡点击/菜单复制用）
$script:PidCache   = @{ pid = 0; ts = 0 }   # Get-ServerPid 的缓存（见该函数内的性能说明）
# 手动指定的软件路径 { key -> exe / 引擎根目录 }，持久化在 tray_config.json 的 manual_paths。
# 自动检测漏掉的软件（如装在非标准位置的 Maya）指定一次后就记住，刷新/重开安装器仍然认。
$script:ManualPaths = @{}

function Load-Config {
    if (Test-Path $CONFIG) {
        try {
            $c = Get-Content $CONFIG -Raw -Encoding utf8 | ConvertFrom-Json
            if ($c.port)     { $script:Port = [int]$c.port }
            if ($c.interval) { $script:Interval = [int]$c.interval }
            if ($c.payload_dir) { $script:PayloadDir = [string]$c.payload_dir }
            if ($c.manual_paths) {
                # 只认四个已知 key，脏配置不会带崩加载
                foreach ($p in @($c.manual_paths.PSObject.Properties)) {
                    $k = [string]$p.Name
                    $v = [string]$p.Value
                    if ($v -and ($AB_APPS_META | Where-Object { $_.k -eq $k })) { $script:ManualPaths[$k] = $v }
                }
            }
        } catch { Log ('读取配置失败: {0}' -f $_) }
    }
}
function Save-Config {
    try {
        # payload_dir 为空时先问服务端要一个，别把空值写进配置
        if (-not $script:PayloadDir) { $script:PayloadDir = Get-PayloadDir }
        $mp = [ordered]@{}
        foreach ($k in @($script:ManualPaths.Keys)) {
            if ($AB_APPS_META | Where-Object { $_.k -eq $k }) { $mp[$k] = [string]$script:ManualPaths[$k] }
        }
        ([ordered]@{ port = $script:Port; interval = $script:Interval;
                     payload_dir = $script:PayloadDir; manual_paths = $mp } | ConvertTo-Json) |
            Out-File -Encoding utf8 $CONFIG
    } catch { Log ('写入配置失败: {0}' -f $_) }
}

# 取中转文件路径（服务端负载目录）。优先问服务端 /api/v1/config，
# 服务端没开就退回上次已知值，再退回默认 runtime\payloads。
function Get-PayloadDir {
    try {
        $req = [System.Net.WebRequest]::Create(('http://127.0.0.1:{0}/api/v1/config' -f $script:Port))
        $req.Method  = 'GET'
        $req.Timeout = 1500
        $req.Proxy   = $null      # 本机回环不走系统代理
        $resp = $req.GetResponse()
        $sr   = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $json = $sr.ReadToEnd()
        $sr.Close(); $resp.Close()
        $o = $json | ConvertFrom-Json
        if ($o.payload_dir) {
            $script:PayloadDir = [string]$o.payload_dir
            return $script:PayloadDir
        }
    } catch { }
    if ($script:PayloadDir) { return $script:PayloadDir }
    return (Join-Path $ROOT 'runtime\payloads')
}

# 用系统资源管理器打开目录（不存在则弹提示）
function Open-Folder([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p) -or -not (Test-Path $p)) {
        [System.Windows.Forms.MessageBox]::Show(('路径不存在：{0}' -f $p), 'Asset Bridge') | Out-Null
        return
    }
    Start-Process -FilePath 'explorer.exe' -ArgumentList ('"{0}"' -f $p)
    Log ('已打开目录 {0}' -f $p)
}

# 清空中转文件夹（payload dir）里的全部内容，保留目录本身。
# 直接删、不停服务：服务只在收发瞬间短暂占用文件，删不掉的项目会计入失败数并进日志，
# 避免了「停服->清理->拉起」把设置窗卡住好几秒、还可能把别处起的服务换成的尴尬。
function Clear-PayloadDir([string]$dir) {
    if ([string]::IsNullOrWhiteSpace($dir)) { $dir = Get-PayloadDir }
    if ([string]::IsNullOrWhiteSpace($dir)) { return '未找到中转目录' }
    if (-not (Test-Path -LiteralPath $dir)) { return '中转目录不存在，无需清理' }
    $items = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)
    if ($items.Count -eq 0) { return '中转目录已经是空的' }
    # 先统计体积，好在结果里告诉用户释放了多少空间
    $bytes = 0L
    foreach ($i in $items) {
        try {
            if ($i.PSIsContainer) {
                $bytes += [int64](Get-ChildItem -LiteralPath $i.FullName -Recurse -Force -File -ErrorAction SilentlyContinue |
                                  Measure-Object -Property Length -Sum).Sum
            } else { $bytes += [int64]$i.Length }
        } catch { }
    }
    $n = 0; $fail = 0
    foreach ($i in $items) {
        try {
            Remove-Item -LiteralPath $i.FullName -Recurse -Force -ErrorAction Stop
            $n++
        } catch {
            $fail++
            Log ('清空中转目录: 删除失败 {0} -> {1}' -f $i.FullName, $_.Exception.Message)
        }
    }
    $msg = ('已清空中转目录：删除 {0} 项，释放 {1} MB（{2}）' -f $n, [math]::Round($bytes / 1MB, 1), $dir)
    if ($fail -gt 0) { $msg += ('；{0} 项删除失败（可能被占用），详见日志' -f $fail) }
    return $msg
}

# ---- 服务控制 -------------------------------------------------------------
function Test-Server {
    # 1) 首选 HTTP 健康检查（最权威：端口在监听且服务真的在响应 API）。
    #    必须显式绕过系统代理——连 127.0.0.1 也会被代理劫成 502，导致误判「服务挂了」。
    #    （之前只做裸 TCP BeginConnect，回调为 $null 时在某些环境下会一直超时 -> 永远判为掉线 -> 图标常驻感叹号）
    try {
        $req = [System.Net.WebRequest]::Create(('http://127.0.0.1:{0}/api/v1/config' -f $script:Port))
        $req.Method   = 'GET'
        $req.Timeout  = 1000
        $req.Proxy    = $null
        $resp = $req.GetResponse()
        $resp.Close()
        return $true
    } catch { }

    # 2) 兜底：裸 TCP 端口探测（服务可能没 HTTP 但端口在监听）
    $tcp = $null
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $ar = $tcp.BeginConnect('127.0.0.1', $script:Port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(1200, $false)
        if ($ok -and $tcp.Connected) { return $true }
        return $false
    } catch { return $false }
    finally { if ($tcp -ne $null) { try { $tcp.Close() } catch { } } }
}

function Get-ServerPid {
    # ⚠️ 性能：Get-NetTCPConnection 每次都要枚举系统全部 TCP 连接，单次可达 0.5~2 秒；
    # 而 Refresh-State 会连调本函数两次（托盘提示 + 状态窗）——
    # 这正是「每次打开窗口/点确定都卡 1 秒」的根源。所以这里加缓存：
    #   查到过 -> 30 秒缓存；没查到 -> 8 秒短缓存（尽快发现服务起/停）。
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $ttl = 30000
    if ([int]$script:PidCache.pid -le 0) { $ttl = 8000 }
    if (($now - [long]$script:PidCache.ts) -lt $ttl) { return [int]$script:PidCache.pid }

    $p = 0
    try {
        $c = Get-NetTCPConnection -LocalPort $script:Port -State Listen -ErrorAction Stop
        if ($c) { $p = [int]($c | Select-Object -First 1).OwningProcess }
    } catch { }
    if ($p -le 0) {
        # 兜底：解析 netstat 输出（老系统或 cmdlet 不可用时）
        try {
            foreach ($ln in (netstat -ano)) {
                if ($ln -match ('{0}\s' -f $script:Port) -and $ln -match 'LISTENING') {
                    $f = @(($ln -split '\s+') | Where-Object { $_ -ne '' })
                    if ($f.Count -ge 5) { $p = [int]$f[$f.Count - 1]; break }
                }
            }
        } catch { }
    }
    $script:PidCache = @{ pid = $p; ts = $now }
    return $p
}

function Reset-ServerPidCache {
    $script:PidCache = @{ pid = 0; ts = 0 }
}

function Start-Server {
    if (Test-Server) { Log '启动跳过：已在运行'; return }
    try {
        # pythonw + run_server.py -> 完全没有控制台窗口，且与托盘进程解耦：
        # 退出托盘不会连带杀掉中转服务。
        $exe = $PYTHONW
        if (-not (Test-Path $exe)) { $exe = $PYTHON }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = $exe
        $psi.Arguments        = ('-u "{0}"' -f $RUNSERVER)
        $psi.WorkingDirectory = $ROOT
        $psi.WindowStyle      = 'Hidden'
        $psi.UseShellExecute  = $false
        $psi.CreateNoWindow   = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        Log ('已启动服务 pid={0} ({1}) log={2}' -f $p.Id, (Split-Path -Leaf $exe), $SRVLOG)
        # 直接把新 pid 写进缓存：不用再走一次慢查询，tooltip 也能立刻显示正确 pid
        $script:PidCache = @{ pid = [int]$p.Id; ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
    } catch { Log ('启动服务失败: {0}' -f $_) }
}

function Stop-Server {
    $p = Get-ServerPid
    if ($p -le 0) { Log '停止跳过：服务未运行'; return }

    # ---- 1) 优雅关闭：POST /api/v1/shutdown --------------------------------
    # 不需要任何进程权限，跨会话同样有效。杀进程只是兜底。
    # 注意：必须绕过系统代理（连 127.0.0.1 也会被劫成 502）。
    try {
        $req = [System.Net.WebRequest]::Create(('http://127.0.0.1:{0}/api/v1/shutdown' -f $script:Port))
        $req.Method        = 'POST'
        $req.Proxy         = $null
        $req.Timeout       = 2000
        $req.ContentLength = 0
        $resp = $req.GetResponse()
        if ($resp -ne $null) { $resp.Close() }
        Log ('已请求优雅关闭 pid={0}' -f $p)
    } catch {
        Log ('优雅关闭请求失败 pid={0}: {1}' -f $p, $_.Exception.Message)
    }

    # ---- 2) 等它自己退出（最多 5 秒）--------------------------------------
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 200
        if (-not (Test-Server)) { Log ('服务已停止 pid={0}（优雅关闭）' -f $p); return }
    }

    # ---- 3) 兜底：强杀进程 ------------------------------------------------
    # 只有当服务是「本托盘拉起的」（同会话）才杀得动；别处起的服务会拒绝访问。
    try {
        Stop-Process -Id $p -Force -ErrorAction Stop
        Log ('已强制停止服务 pid={0}' -f $p)
    } catch {
        Log ('强杀失败 pid={0}: {1}' -f $p, $_.Exception.Message)
        Log ('  -> 该进程不是本托盘启动的（多半是别的终端/会话起的）。可手动结束：taskkill /F /PID {0}' -f $p)
    }
}

# ---- 编辑器守护进程（PS / SAI2：打开贴图 -> 监听保存 -> 自动回传）----------
# 与中转服务解耦：服务挂了不影响它，它挂了也不影响各 DCC 正常收发。
# 状态来源：runtime\editor_daemon\ 下的 daemon.pid / status.json。
function Get-EditorDaemonPid {
    $pf = Join-Path $EDITOR_STATE 'daemon.pid'
    if (-not (Test-Path $pf)) { return 0 }
    $dv = 0
    try { $dv = [int]((Get-Content $pf -Raw -Encoding UTF8).Trim()) } catch { return 0 }
    if ($dv -le 0) { return 0 }
    try {
        $proc = Get-Process -Id $dv -ErrorAction Stop
        if ($proc.ProcessName -match 'python') { return $dv }
    } catch { }
    return 0
}
function Test-EditorDaemon { return ((Get-EditorDaemonPid) -gt 0) }
function Start-EditorDaemon {
    if (Test-EditorDaemon) { Log '编辑器守护启动跳过：已在运行'; return }
    if (-not (Test-Path $EDITOR_DAEMON)) { Log ('编辑器守护启动失败：缺 ' + $EDITOR_DAEMON); return }
    try {
        # 与中转服务同样的拉法：pythonw 零窗口、与托盘进程解耦
        $exe = $PYTHONW
        if (-not (Test-Path $exe)) { $exe = $PYTHON }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = $exe
        $psi.Arguments        = ('-u "{0}"' -f $EDITOR_DAEMON)
        $psi.WorkingDirectory = $ROOT
        $psi.WindowStyle      = 'Hidden'
        $psi.UseShellExecute  = $false
        $psi.CreateNoWindow   = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        Log ('已启动编辑器守护 pid={0}（PS/SAI2 贴图自动回传）' -f $p.Id)
    } catch { Log ('启动编辑器守护失败: {0}' -f $_) }
}
function Stop-EditorDaemon {
    $p = Get-EditorDaemonPid
    if ($p -le 0) {
        # 清掉可能残留的 pid 文件（进程已死但文件还在）
        $pf = Join-Path $EDITOR_STATE 'daemon.pid'
        if (Test-Path $pf) { try { Remove-Item $pf -Force -ErrorAction SilentlyContinue } catch { } }
        Log '编辑器守护停止跳过：未在运行'
        return
    }
    # 写 stop.flag：守护进程主循环每轮检查，干净退出（绝不杀用户的 PS/SAI2）
    try {
        if (-not (Test-Path $EDITOR_STATE)) { New-Item -ItemType Directory -Path $EDITOR_STATE -Force | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $EDITOR_STATE 'stop.flag'), (Get-Date).ToString('s'))
        Log ('已向编辑器守护发送停止指令 pid={0}' -f $p)
    } catch { Log ('写 stop.flag 失败: {0}' -f $_) }
    # 等最多 10 秒（保存回传中的 watch 需要时间收尾）；没退成再强杀兜底
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 400
        if (-not (Test-EditorDaemon)) { Log '编辑器守护已停止'; return }
    }
    try {
        Stop-Process -Id $p -Force -ErrorAction Stop
        Log ('已强制停止编辑器守护 pid={0}' -f $p)
    } catch {
        Log ('强杀编辑器守护失败 pid={0}: {1}（可能不是本会话拉起，可 taskkill /F /PID {0}）' -f $p, $_.Exception.Message)
    }
}
function Get-EditorDaemonStatusText {
    # 菜单 / 状态窗共用的一句话状态
    $p = Get-EditorDaemonPid
    if ($p -le 0) { return '编辑器守护：未运行' }
    $extra = ''
    $sf = Join-Path $EDITOR_STATE 'status.json'
    if (Test-Path $sf) {
        try {
            $st = Get-Content $sf -Raw -Encoding UTF8 | ConvertFrom-Json
            $w = @($st.watching).Count
            if ($w -gt 0) { $extra = ('，监听中 {0} 个文件' -f $w) }
        } catch { }
    }
    return ('编辑器守护：运行中 pid={0}{1}' -f $p, $extra)
}

# ---- 界面 -----------------------------------------------------------------
[System.Windows.Forms.Application]::EnableVisualStyles()

$ni = New-Object System.Windows.Forms.NotifyIcon
# D3：气泡被点击 -> 复制 ZBrush 接收路径（Win11 toast 下可能不触发，故有菜单兜底）
$ni.add_BalloonTipClicked({
    if ($script:AbNotifyPath) {
        try { Set-Clipboard -Value $script:AbNotifyPath } catch { }
        try { $ni.ShowBalloonTip(3000, 'AssetBridge', '路径已复制到剪贴板', 'Info') } catch { }
        $script:AbNotifyPath = ''
    }
})
$script:IconCache = @{}          # 状态 -> @{ ts=文件时间戳; icon=Icon 对象 }
function Apply-Icon([string]$state) {
    # 四态：ok=绿 / warn=琥珀 / paused=灰 / down=红（读不到文件时退回系统图标）
    switch ($state) {
        'ok'     { $path = $ICON_OK;     $alt = 'state_running.ico'; $fb = [System.Drawing.SystemIcons]::Information }
        'warn'   { $path = $ICON_WARN;   $alt = 'state_recover.ico'; $fb = [System.Drawing.SystemIcons]::Warning }
        'paused' { $path = $ICON_PAUSED; $alt = 'state_paused.ico';  $fb = [System.Drawing.SystemIcons]::Shield }
        default  { $path = $ICON_DOWN;   $alt = 'state_stopped.ico'; $fb = [System.Drawing.SystemIcons]::Error }
    }
    # 回退：tray\icons\ 缺失时用 assets\icons\ico\state_*.ico；两处都没有才退回系统图标
    if (-not (Test-Path $path)) {
        $altPath = Join-Path (Join-Path $ROOT 'assets\icons\ico') $alt
        if (Test-Path $altPath) { $path = $altPath }
    }
    # 按「文件修改时间」缓存：既不会每 10 秒重建 Icon（会泄漏 GDI 句柄），
    # 又能在磁盘上的图标被替换后自动换新的 —— 不用重启托盘。
    if (Test-Path $path) {
        try {
            $ts = (Get-Item $path).LastWriteTimeUtc.Ticks
            $e  = $script:IconCache[$state]
            if ($e -eq $null -or $e.ts -ne $ts) {
                $ico = New-Object System.Drawing.Icon -ArgumentList $path
                if ($e -ne $null -and $e.icon -ne $null) { try { $e.icon.Dispose() } catch { } }
                $script:IconCache[$state] = @{ ts = $ts; icon = $ico }
            }
            $ni.Icon = $script:IconCache[$state].icon
            return
        } catch { }
    }
    $ni.Icon = $fb
}

$script:StatusLabel = $null
$script:LogBox      = $null

function Refresh-State {
    $up = Test-Server
    $script:LastUp = $up

    # 图标四态：运行/掉线待自愈/运行但守护暂停/已停止
    $state = 'down'
    if ($up) {
        if ($script:Guard) { $state = 'ok' } else { $state = 'paused' }
    } else {
        if ($script:Guard) { $state = 'warn' } else { $state = 'down' }
    }
    Apply-Icon $state

    if ($up) {
        $p = Get-ServerPid
        if ($script:Guard) {
            $ni.Text = ('AssetBridge 运行中 :{0} pid={1}' -f $script:Port, $p)
        } else {
            $ni.Text = ('AssetBridge 运行中(守护已暂停) :{0}' -f $script:Port)
        }
    } else {
        if ($script:Guard) {
            $ni.Text = ('AssetBridge 已停止 :{0} (等待自动拉起)' -f $script:Port)
        } else {
            $ni.Text = ('AssetBridge 已停止 :{0}' -f $script:Port)
        }
    }
    if ($script:StatusLabel -ne $null) { Update-StatusWindow }

    # 编辑器守护（PS/SAI2）状态：菜单文字 + 按钮可用性
    $script:LastEdUp = (Test-EditorDaemon)
    if ($miEdState -ne $null) { $miEdState.Text = (Get-EditorDaemonStatusText) }
    if ($miEdStart -ne $null) { $miEdStart.Enabled = (-not $script:LastEdUp) }
    if ($miEdStop  -ne $null) { $miEdStop.Enabled  = $script:LastEdUp }
}

function Update-StatusWindow {
    if ($script:StatusLabel -eq $null) { return }
    $up = $script:LastUp
    if ($up) {
        $p = Get-ServerPid
        $script:StatusLabel.Text = ('状态：运行中    端口 {0}    PID {1}    守护：{2}' -f `
            $script:Port, $p, (&{ if ($script:Guard) { '开启' } else { '已暂停' } }))
        $script:StatusLabel.ForeColor = [System.Drawing.Color]::FromArgb(30, 130, 60)
    } else {
        $script:StatusLabel.Text = ('状态：已停止    端口 {0}    守护：{1}' -f `
            $script:Port, (&{ if ($script:Guard) { '开启' } else { '已暂停' } }))
        $script:StatusLabel.ForeColor = [System.Drawing.Color]::FromArgb(180, 50, 40)
    }
    if ($script:LogBox -ne $null) {
        try {
            if (Test-Path $LOG) {
                $tail = Get-Content $LOG -Tail 14 -Encoding utf8
                $script:LogBox.Lines = $tail
                $script:LogBox.SelectionStart = $script:LogBox.Text.Length
                $script:LogBox.ScrollToCaret()
            }
        } catch { }
    }
    if ($script:EdLabel -ne $null) {
        $edUp = (Test-EditorDaemon)
        $script:EdLabel.Text = (Get-EditorDaemonStatusText)
        $script:EdLabel.ForeColor = if ($edUp) { [System.Drawing.Color]::FromArgb(30, 130, 60) } else { [System.Drawing.Color]::FromArgb(120, 120, 120) }
    }
}

# ---- 右键菜单 -------------------------------------------------------------
$menu = New-Object System.Windows.Forms.ContextMenuStrip

$miState = $menu.Items.Add('状态：检查中...')
$miState.Enabled = $false
[void]$menu.Items.Add('-')
$miStart   = $menu.Items.Add('启动服务')
$miStop    = $menu.Items.Add('停止服务')
$miRestart = $menu.Items.Add('重启服务')
[void]$menu.Items.Add('-')
$miGuard   = $menu.Items.Add('暂停守护')
[void]$menu.Items.Add('-')
$miEdState  = $menu.Items.Add('编辑器守护：检查中...')
$miEdState.Enabled = $false
$miEdStart  = $menu.Items.Add('启动编辑器守护（PS/SAI2 回传）')
$miEdStop   = $menu.Items.Add('停止编辑器守护')
# D3 兜底：ZBrush 落盘通知的气泡点击在部分 Win11 上不触发，
# 右键菜单常驻一项「复制最近一次 ZBrush 接收路径」（Popup 时读 notify.json 刷新）
$miZbRecv   = $menu.Items.Add('复制最近一次 ZBrush 接收路径')
$miZbRecv.Enabled = $false
$menu.add_Popup({
    $has = $false
    $nf = Join-Path $EDITOR_STATE 'notify.json'
    if (Test-Path $nf) {
        try {
            $nj = Get-Content $nf -Raw -Encoding UTF8 | ConvertFrom-Json
            $script:AbNotifyPath = [string]$nj.path
            if ($script:AbNotifyPath) {
                $miZbRecv.Text = ('复制 ZBrush 接收路径：' + (Split-Path $script:AbNotifyPath -Leaf))
                $miZbRecv.Enabled = $true
                $has = $true
            }
        } catch { }
    }
    if (-not $has) {
        $miZbRecv.Text = '复制最近一次 ZBrush 接收路径'
        $miZbRecv.Enabled = $false
    }
})
$miZbRecv.add_Click({
    if ($script:AbNotifyPath) {
        try { Set-Clipboard -Value $script:AbNotifyPath } catch { }
        try { $ni.ShowBalloonTip(3000, 'AssetBridge', '路径已复制到剪贴板', 'Info') } catch { }
    }
})
[void]$menu.Items.Add('-')
$miLog     = $menu.Items.Add('打开托盘日志')
$miSrvLog  = $menu.Items.Add('打开服务日志')
$miFolder  = $menu.Items.Add('打开项目文件夹')
$miPayload = $menu.Items.Add('打开中转文件目录')
[void]$menu.Items.Add('-')
$miSet     = $menu.Items.Add('设置...')
[void]$menu.Items.Add('-')
$miInstall = $menu.Items.Add('安装插件...')
[void]$menu.Items.Add('-')
$miExitAll = $menu.Items.Add('退出并停止服务')
$miExit    = $menu.Items.Add('退出（服务保持运行）')

$miStart.add_Click({   Start-Server;  Start-Sleep -Milliseconds 400; Refresh-State })
$miStop.add_Click({    Stop-Server;   Start-Sleep -Milliseconds 400; Refresh-State })
$miRestart.add_Click({ Stop-Server;   Start-Sleep -Seconds 1; Start-Server; Start-Sleep -Milliseconds 400; Refresh-State })
$miGuard.add_Click({
    $script:Guard = -not $script:Guard
    if ($script:Guard) { $miGuard.Text = '暂停守护'; Log '守护已恢复' } else { $miGuard.Text = '恢复守护'; Log '守护已暂停' }
    Refresh-State
})
$miEdStart.add_Click({ Start-EditorDaemon; Start-Sleep -Milliseconds 600; Refresh-State })
$miEdStop.add_Click({  Stop-EditorDaemon;  Start-Sleep -Milliseconds 400; Refresh-State })
$miLog.add_Click({    Start-Process -FilePath 'notepad.exe' -ArgumentList $LOG })
$miSrvLog.add_Click({ Start-Process -FilePath 'notepad.exe' -ArgumentList $SRVLOG })
$miFolder.add_Click({ Start-Process -FilePath 'explorer.exe' -ArgumentList $ROOT })
$miPayload.add_Click({ Open-Folder (Get-PayloadDir) })
$miExitAll.add_Click({
    Stop-Server
    Log '托盘退出并停止服务'
    Exit-Tray
})
$miExit.add_Click({
    Log '托盘退出（服务保持运行）'
    Exit-Tray
})
# 右键「设置...」之前漏绑了点击事件（只有状态窗里的按钮绑了 Show-Settings），
# 导致右键点了没反应。这里补上。
$miSet.add_Click({ Show-Settings })
$miInstall.add_Click({ Show-Installer })

$ni.ContextMenuStrip = $menu

# ---- 状态小窗（双击托盘图标打开；关闭 = 缩回托盘）------------------------
$script:Form = $null
function Show-StatusWindow {
    if ($script:Form -eq $null) {
        $f = New-Object System.Windows.Forms.Form
        $f.Text            = 'Asset Bridge 状态'
        $f.ClientSize      = New-Object System.Drawing.Size(560, 330)
        $f.StartPosition   = 'CenterScreen'
        $f.MinimizeBox     = $true
        $f.ShowInTaskbar   = $true

        $script:StatusLabel = New-Object System.Windows.Forms.Label
        $script:StatusLabel.SetBounds(14, 12, 530, 24)
        $script:StatusLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
        $f.Controls.Add($script:StatusLabel)

        # 第二行：编辑器守护（PS/SAI2）状态
        $script:EdLabel = New-Object System.Windows.Forms.Label
        $script:EdLabel.SetBounds(14, 36, 530, 16)
        $script:EdLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
        $f.Controls.Add($script:EdLabel)

        $script:LogBox = New-Object System.Windows.Forms.TextBox
        $script:LogBox.SetBounds(14, 56, 530, 208)
        $script:LogBox.Multiline   = $true
        $script:LogBox.ReadOnly    = $true
        $script:LogBox.WordWrap    = $true    # 日志自动换行
        $script:LogBox.ScrollBars  = 'Vertical'
        $script:LogBox.BackColor   = [System.Drawing.Color]::White
        $script:LogBox.Font        = New-Object System.Drawing.Font('Consolas', 9)
        $f.Controls.Add($script:LogBox)

        # 窗口图标：标题栏 + 任务栏都设为 app_logo（任务栏小图标见 Set-FormIcon）
        Set-FormIcon $f

        # 按钮：左侧 16px 小图标 + 右侧文字（图标来自 assets\icons\png\16）
        $b1 = New-AbButton $f '启动'       'act_start.png'    14  96
        $b1.add_Click({ Start-Server; Start-Sleep -Milliseconds 400; Refresh-State })

        $b2 = New-AbButton $f '停止'       'act_stop.png'     118 96
        $b2.add_Click({ Stop-Server; Start-Sleep -Milliseconds 400; Refresh-State })

        $b3 = New-AbButton $f '重启'       'act_restart.png'  222 96
        $b3.add_Click({ Stop-Server; Start-Sleep -Seconds 1; Start-Server; Start-Sleep -Milliseconds 400; Refresh-State })

        $b4 = New-AbButton $f '设置...'    'act_settings.png' 326 96
        $b4.add_Click({ Show-Settings })

        $b5 = New-AbButton $f '隐藏到托盘' 'state_paused.png'  430 124
        $b5.add_Click({ $script:Form.Hide() })

        # 关闭 = 缩回托盘；但「真正退出」时不能拦截，否则 Application.Exit() 会被取消
        $f.add_FormClosing({ param($sender, $e)
            if ($script:ReallyExit) { return }
            $e.Cancel = $true
            $sender.Hide()
        })
        $script:Form = $f
    }
    Update-StatusWindow
    $script:Form.Show()
    $script:Form.Activate()
}

# ---- 设置窗 ---------------------------------------------------------------
function Show-Settings {
    $d = New-Object System.Windows.Forms.Form
    $d.Text          = 'Asset Bridge 设置'
    $d.ClientSize    = New-Object System.Drawing.Size(400, 244)
    $d.StartPosition = 'CenterParent'
    $d.FormBorderStyle = 'FixedDialog'
    $d.MaximizeBox   = $false
    $d.MinimizeBox   = $false

    $l1 = New-Object System.Windows.Forms.Label
    $l1.Text = '服务端口'; $l1.SetBounds(16, 20, 90, 22); $d.Controls.Add($l1)
    $t1 = New-Object System.Windows.Forms.TextBox
    $t1.Text = [string]$script:Port; $t1.SetBounds(112, 18, 274, 24); $d.Controls.Add($t1)

    $l2 = New-Object System.Windows.Forms.Label
    $l2.Text = '自检间隔(秒)'; $l2.SetBounds(16, 56, 90, 22); $d.Controls.Add($l2)
    $t2 = New-Object System.Windows.Forms.TextBox
    $t2.Text = [string]$script:Interval; $t2.SetBounds(112, 54, 274, 24); $d.Controls.Add($t2)

    # 中转文件路径：打开设置时自动填入服务端当前的负载目录
    $l3 = New-Object System.Windows.Forms.Label
    $l3.Text = '中转文件路径'; $l3.SetBounds(16, 92, 90, 22); $d.Controls.Add($l3)
    $t3 = New-Object System.Windows.Forms.TextBox
    $t3.Text = (Get-PayloadDir); $t3.SetBounds(112, 90, 184, 24); $d.Controls.Add($t3)
    $ob = New-Object System.Windows.Forms.Button
    $ob.Text = '打开文件夹'; $ob.SetBounds(302, 89, 84, 26)
    $ob.add_Click({ Open-Folder ([string]$t3.Text) })
    $d.Controls.Add($ob)

    # 清空中转文件夹：确认后删掉该目录下全部内容（保留目录本身）
    $cb = New-Object System.Windows.Forms.Button
    $cb.Text = '清空'; $cb.SetBounds(302, 117, 84, 26)
    $cb.add_Click({
        try {
            $dir = ([string]$t3.Text).Trim()
            if ([string]::IsNullOrWhiteSpace($dir)) { $dir = Get-PayloadDir; $t3.Text = $dir }
            if ([string]::IsNullOrWhiteSpace($dir) -or -not (Test-Path -LiteralPath $dir)) {
                [System.Windows.Forms.MessageBox]::Show(('中转目录不存在：{0}' -f $dir), 'Asset Bridge') | Out-Null
                return
            }
            $r = [System.Windows.Forms.MessageBox]::Show(
                ('确定清空中转文件夹吗？' + "`n`n" + $dir +
                 "`n`n该目录下所有互传中的模型/贴图文件都会被删除（传输历史记录仍保留，不影响已安装的插件）。" +
                 "`n建议确认没有正在进行的收发后再操作。"),
                '确认清空', 'YesNo', 'Warning')
            if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            $msg = Clear-PayloadDir $dir
            Log ('设置: ' + $msg)
            [System.Windows.Forms.MessageBox]::Show($msg, 'Asset Bridge') | Out-Null
        } catch {
            # 单个按钮出错不该把整个托盘崩掉（全局 ThreadException 之外的近距离兜底）
            try { Log ('设置: 清空中转目录出错 -> ' + $_.Exception.Message) } catch { }
            [System.Windows.Forms.MessageBox]::Show(('清空失败：{0}' -f $_.Exception.Message), 'Asset Bridge') | Out-Null
        }
    })
    $d.Controls.Add($cb)

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = '该目录存放各软件互传的模型/贴图，由中转服务管理'
    $hint.ForeColor = [System.Drawing.Color]::Gray
    $hint.SetBounds(112, 146, 274, 18); $d.Controls.Add($hint)

    $hint2 = New-Object System.Windows.Forms.Label
    $hint2.Text = '「清空」会删除目录内全部文件（不影响已安装的插件）'
    $hint2.ForeColor = [System.Drawing.Color]::Gray
    $hint2.SetBounds(112, 166, 274, 18); $d.Controls.Add($hint2)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = '确定'; $ok.SetBounds(112, 192, 88, 30)
    $ok.add_Click({
        try {
            $np = [int]$t1.Text
            $ni2 = [int]$t2.Text
            if ($np -lt 1 -or $np -gt 65535) { throw '端口范围应为 1-65535' }
            if ($ni2 -lt 3 -or $ni2 -gt 600) { throw '间隔范围应为 3-600 秒' }
            $script:Port = $np
            $script:Interval = $ni2
            $script:PayloadDir = ([string]$t3.Text).Trim()
            $timer.Interval = $script:Interval * 1000
            Save-Config
            Log ('设置已更新 port={0} interval={1} payload={2}' -f $np, $ni2, $script:PayloadDir)
            $d.Close()
            Refresh-State
        } catch {
            [System.Windows.Forms.MessageBox]::Show(('设置无效：{0}' -f $_), 'Asset Bridge') | Out-Null
        }
    })
    $d.Controls.Add($ok)

    $ca = New-Object System.Windows.Forms.Button
    $ca.Text = '取消'; $ca.SetBounds(214, 192, 88, 30)
    $ca.add_Click({ $d.Close() })
    $d.Controls.Add($ca)

    Set-FormIcon $d

    [void]$d.ShowDialog()
}

# ===========================================================================
#  插件安装器：自动识别本机 DCC 软件 + 版本校验 + 一键安装 + 从 GitHub 下载
# ===========================================================================
# 默认下载仓库（已推送 GitHub：github.com/zhdcirno/AssetBridge）
$script:GitHubRepo    = 'https://github.com/zhdcirno/AssetBridge'
$script:InstallSource = $null   # $null => 用本仓库 $ROOT/plugins；下载后指向解压目录

# 各软件最低版本（用户硬性要求）
$MIN_BLENDER  = '5.0'
$MIN_MAYA     = '2023'
$MIN_SP       = '10.0.1'
$MIN_SP_BUILD = 3757
$MIN_UE       = '5.8'
$MIN_TOOLBAG  = '5.0'   # 适配器用的是 Toolbag 5 的 mset API
$MIN_ZBRUSH   = '2026.0'  # Python API 是 2026.0 才有的（2025.x 只有 ZScript）

# 本机把 DCC 都装在各自盘的 DCC 目录下（用户习惯），一并纳入自动检测范围
$DCC_ROOTS = @('D:\DCC', 'G:\DCC', 'E:\DCC', 'C:\DCC')

# 安装器支持的软件（k=key / n=报告里显示名 / s=按钮短名 / src=本仓库插件源相对路径）
#   - blender / maya / sp / ue / toolbag：有插件要装（src 指向插件目录）
#   - photoshop / sai2：**不需要装插件**——贴图由「编辑器守护进程」直接调用 exe 打开，
#     保存后自动回传。这里纳入只是为了检测 + 手动指定路径（同步进 editor_daemon.json）。
$AB_APPS_META = @(
    @{ k='blender';    n='Blender';           s='Blender';    src='plugins/blender/asset_bridge_blender' },
    @{ k='maya';       n='Maya';              s='Maya';       src='plugins/maya/asset_bridge_maya' },
    @{ k='sp';         n='Substance Painter'; s='SP';         src='plugins/substance_painter/asset-bridge' },
    @{ k='ue';         n='Unreal Engine';     s='UE';         src='plugins/unreal/AssetBridge' },
    @{ k='toolbag';    n='Marmoset Toolbag';  s='Toolbag';    src='plugins/toolbag/asset_bridge' },
    @{ k='photoshop';  n='Photoshop';         s='PS';         src='' },
    @{ k='sai2';       n='SAI2';              s='SAI2';       src='' },
    @{ k='zbrush';     n='ZBrush';            s='ZBrush';     src='plugins/zbrush/asset_bridge_zbrush' }
)

function Parse-Version([string]$s) {
    $out = @()
    foreach ($p in ($s -split '[.\-]')) {
        $n = 0
        if ([int]::TryParse($p, [ref]$n)) { $out += $n }
    }
    return $out
}
function Ver-GE($a, $b) {
    $va = Parse-Version $a; $vb = Parse-Version $b
    $n = [Math]::Max($va.Count, $vb.Count)
    for ($i = 0; $i -lt $n; $i++) {
        $x = if ($i -lt $va.Count) { $va[$i] } else { 0 }
        $y = if ($i -lt $vb.Count) { $vb[$i] } else { 0 }
        if ($x -ne $y) { return ($x -gt $y) }
    }
    return $true
}
function Get-LnkTarget([string]$lnk) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes($lnk)
        $txt = [System.Text.Encoding]::Unicode.GetString($bytes)
        foreach ($tok in ($txt -split "`0")) {
            if ($tok -match '\.exe$') { return $tok.Trim() }
        }
    } catch { }
    return ''
}
function Get-AppSourceDir([string]$rel) {
    $base = if ($script:InstallSource) { $script:InstallSource } else { $ROOT }
    return Join-Path $base $rel
}
function Get-ExeVer([string]$exe) {
    try {
        $vi = (Get-Item $exe -ErrorAction Stop).VersionInfo
        if ($vi -and $vi.ProductVersion) { return $vi.ProductVersion }
        if ($vi -and $vi.FileVersion)   { return $vi.FileVersion }
    } catch { }
    return ''
}
function Get-ExeVerAll([string]$exe) {
    # 同时返回 ProductVersion / FileVersion（SP 的 build 号藏在 FileVersion 第 4 段）
    $r = @{ pv = ''; fv = '' }
    try {
        $vi = (Get-Item $exe -ErrorAction Stop).VersionInfo
        $r.pv = [string]$vi.ProductVersion
        $r.fv = [string]$vi.FileVersion
    } catch { }
    return $r
}
function Sort-VersionKeys($keys) {
    # 按 "主版本.次版本" 数值排序（字符串排序会把 10.0 排到 5.0 前面）
    return @($keys | Sort-Object {
        $v = @(Parse-Version $_)
        if ($v.Count -ge 2) { [int]$v[0] * 10000 + [int]$v[1] } else { [int]$v[0] * 10000 }
    })
}
function Find-ExeDeep([string[]]$roots, [string]$leaf, [int]$depth = 3) {
    # 在若干根目录下限制深度递归查找某个可执行文件（避免全盘扫描）
    $out = @()
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        foreach ($f in (Get-ChildItem $r -Recurse -Depth $depth -Filter $leaf -File -ErrorAction SilentlyContinue)) {
            $out += $f.FullName
        }
    }
    return @($out | Sort-Object -Unique)
}
function Find-ExeViaShortcut([string]$pattern) {
    # 兜底：从桌面 / 开始菜单快捷方式里解析真实 exe 路径
    $out = @()
    foreach ($ld in @((Join-Path $env:USERPROFILE 'Desktop'),
                      (Join-Path $env:PUBLIC 'Desktop'),
                      (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
        if (-not (Test-Path $ld)) { continue }
        foreach ($l in (Get-ChildItem $ld -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue)) {
            $t = Get-LnkTarget $l.FullName
            if ($t -match $pattern -and (Test-Path $t)) { $out += $t }
        }
    }
    return @($out | Sort-Object -Unique)
}
function Get-BackupDir([string]$tag) {
    $d = Join-Path (Join-Path $ROOT 'backups') ($tag + '_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}
function Move-ToBackup([string]$path, [string]$bkDir) {
    # 移动到备份目录；同名时自动加序号，绝不覆盖既有备份
    $leaf = Split-Path $path -Leaf
    $dest = Join-Path $bkDir $leaf
    $i = 1
    while (Test-Path $dest) { $dest = Join-Path $bkDir ($leaf + "_$i"); $i++ }
    Move-Item $path $dest -Force
    return $dest
}
function Get-PluginFingerprint([string]$dir) {
    # 源目录内容指纹（相对路径+大小+修改时间），用于判断"已安装的是否还是最新源码"
    if (-not $dir -or -not (Test-Path $dir)) { return '' }
    try {
        $items = Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Name -ne '.assetbridge.json' } |
                 Sort-Object FullName
        $sb = New-Object System.Text.StringBuilder
        foreach ($f in $items) {
            $rel = $f.FullName.Substring($dir.Length).Replace('\', '/')
            [void]$sb.Append($rel).Append('|').Append($f.Length).Append('|').Append($f.LastWriteTimeUtc.Ticks).Append("`n")
        }
        $sha = [System.Security.Cryptography.SHA1]::Create()
        $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($sb.ToString()))
        return ([System.BitConverter]::ToString($hash) -replace '-', '').Substring(0, 16)
    } catch { return '' }
}
function Write-Marker([string]$dst, $app) {
    # 装完后在目标目录里留一张"安装卡片"，之后就能判断 已安装 / 可更新
    try {
        $m = [ordered]@{
            tool        = 'AssetBridge'
            host        = $app.key
            hostVersion = $app.version
            installedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
            source      = $(if ($script:InstallSource) { 'github' } else { 'local' })
            fingerprint = (Get-PluginFingerprint (Get-AppSourceDir $app.src))
        }
        $json = $m | ConvertTo-Json
        [System.IO.File]::WriteAllText((Join-Path $dst '.assetbridge.json'), $json,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}
function Get-InstallState($app) {
    $st = @{ installed = $false; latest = $false; text = '未安装' }
    if (-not $app -or -not $app.targets -or @($app.targets).Count -eq 0) { return $st }
    $exist = @($app.targets | Where-Object { Test-Path $_ })
    if ($exist.Count -eq 0) { return $st }
    $st.installed = $true
    $srcFp = Get-PluginFingerprint (Get-AppSourceDir $app.src)
    $coveredAll = ($exist.Count -eq @($app.targets).Count)
    $hasMarker = $false; $fpMatch = $true
    foreach ($t in $exist) {
        $mf = Join-Path $t '.assetbridge.json'
        if (Test-Path $mf) {
            $hasMarker = $true
            $fp = ''
            try { $fp = [string]((Get-Content $mf -Raw -Encoding UTF8 | ConvertFrom-Json).fingerprint) } catch { }
            if (-not $fp -or -not $srcFp -or $fp -ne $srcFp) { $fpMatch = $false }
        } else {
            $fpMatch = $false
        }
    }
    if (-not $hasMarker)      { $st.text = '已安装（无版本记录）' }
    elseif (-not $coveredAll) { $st.text = '已安装（部分版本缺失）' }
    elseif ($fpMatch)         { $st.text = '已安装（最新）'; $st.latest = $true }
    else                      { $st.text = '已安装（源已更新，可重装）' }
    return $st
}
function Find-Blender {
    $exes = @()
    foreach ($r in ($DCC_ROOTS + @('C:\Program Files\Blender Foundation'))) {
        if (-not (Test-Path $r)) { continue }
        foreach ($d in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            $c = Join-Path $d.FullName 'blender.exe'
            if (Test-Path $c) { $exes += $c }
        }
    }
    $exes += Find-ExeViaShortcut 'blender\.exe$'

    $good = @{}; $low = @()      # good: "主.次版本" -> exe；Blender 每个大版本目录独立，故按版本聚合
    foreach ($e in @($exes | Sort-Object -Unique)) {
        $ver = Get-ExeVer $e
        if (-not $ver -and ($e -match '(\d+\.\d+(?:\.\d+)?)')) { $ver = $Matches[1] }
        if (-not $ver) { continue }
        if (-not (Ver-GE $ver $MIN_BLENDER)) { $low += $ver; continue }
        $mm = ($ver -split '\.')[0..1] -join '.'
        if (-not $good.ContainsKey($mm)) { $good[$mm] = $e }
    }
    if ($good.Count -eq 0) {
        if ($low.Count -eq 0) { return $null }
        return [PSCustomObject]@{
            key='blender'; name='Blender'; version=((Sort-VersionKeys ($low | Select-Object -Unique)) -join ',');
            path=''; min=$MIN_BLENDER; src='plugins/blender/asset_bridge_blender';
            targets=@(); ok=$false; note="检测到的 Blender 版本过低（需 >= $MIN_BLENDER），无法安装"
        }
    }
    # ⚠️ 必须用 @() 包住：PowerShell 函数返回单元素数组会被"拆包"成标量，
    # 那样 $keys[-1] 就变成取字符串最后一个字符（曾导致 path 变空）。
    $keys = @(Sort-VersionKeys $good.Keys)
    $targets = @()
    foreach ($k in $keys) {
        $targets += "$(Join-Path $env:APPDATA 'Blender Foundation')\Blender\$k\scripts\addons\asset_bridge_blender"
    }
    $note = ''
    if ($low.Count -gt 0) {
        $note = "另有低于 $MIN_BLENDER 的版本未安装：" + ((Sort-VersionKeys ($low | Select-Object -Unique)) -join ',')
    }
    return [PSCustomObject]@{
        key='blender'; name='Blender'; version=($keys -join ','); path=$good[$keys[-1]]; min=$MIN_BLENDER;
        src='plugins/blender/asset_bridge_blender'; targets=$targets; ok=$true; note=$note
    }
}
function Convert-MayaYear([string]$v) {
    # Maya 的 exe 版本是内部号（2023 -> 23.3.0.2072），没有年份；
    # 2016 起内部主版本号 = 年份 - 2000，用它反推年份。
    if (-not $v) { return '' }
    $p = @(Parse-Version $v)
    if ($p.Count -ge 1 -and $p[0] -ge 16 -and $p[0] -le 99) { return [string](2000 + [int]$p[0]) }
    return ''
}
function Find-Maya {
    [CmdletBinding()]
    param(
        [string[]]$ExtraRoots = @(),
        [int]$MaxDepth = 3
    )

    # 增强版：从 PATH / 环境变量 / App Paths 注册表 / 卸载注册表 / Autodesk 注册表 /
    # 文件关联 / 快捷方式 / 各盘常见目录 全维度探测 maya.exe，再把结果映射成安装器
    # 需要的统一对象（多版本聚合 targets、按 $MIN_MAYA 门槛过滤）。
    $cands = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )

    function Add-MayaCandidate {
        param(
            [string]$Path,
            [string]$Source
        )
        if ([string]::IsNullOrWhiteSpace($Path)) { return }
        $p = $Path.Trim()
        $p = $p.Trim('"')
        $p = $p -replace ',\d+$', ''      # DisplayIcon 常见 "...,0"
        $p = $p -replace '\s+/.*$', ''    # 去掉命令行参数
        $p = $p.Trim('"')
        $exe = $null
        # 情况 1：给的是目录。可能目录下直接有 maya.exe，也可能是安装根，bin\maya.exe
        if (Test-Path -LiteralPath $p -PathType Container) {
            foreach ($t in @((Join-Path $p 'maya.exe'), (Join-Path $p 'bin\maya.exe'))) {
                if (Test-Path -LiteralPath $t -PathType Leaf) { $exe = (Resolve-Path -LiteralPath $t).Path; break }
            }
        }
        # 情况 2：给的就是 maya.exe
        elseif (Test-Path -LiteralPath $p -PathType Leaf) {
            if ([IO.Path]::GetFileName($p) -ieq 'maya.exe') { $exe = (Resolve-Path -LiteralPath $p).Path }
        }
        if (-not $exe) { return }
        $full = [IO.Path]::GetFullPath($exe)
        if (-not $seen.Add($full)) { return }
        $ver = $null
        try {
            $vi = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($full)
            if ($vi.ProductVersion -match '\d{4}') { $ver = $Matches[0] }
            elseif ($vi.FileVersion -match '\d{4}') { $ver = $Matches[0] }
        } catch {}
        if (-not $ver -and $full -match 'Maya[ _-]?(\d{4})') { $ver = $Matches[1] }
        $cands.Add([pscustomobject]@{ VER=$ver; PATH=$full; SOURCE=$Source })
    }

    # 1. PATH / Get-Command / where
    if ($env:Path) {
        $env:Path -split ';' | Where-Object { $_ } | ForEach-Object { Add-MayaCandidate $_ 'PATH' }
    }
    Get-Command maya.exe -ErrorAction SilentlyContinue | ForEach-Object { Add-MayaCandidate $_.Source 'Get-Command' }
    where.exe maya.exe 2>$null | ForEach-Object { Add-MayaCandidate $_ 'where.exe' }

    # 2. 环境变量
    if ($env:MAYA_LOCATION) { Add-MayaCandidate $env:MAYA_LOCATION 'ENV:MAYA_LOCATION' }
    if ($env:DCC_ROOTS) { $env:DCC_ROOTS -split ';' | Where-Object { $_ } | ForEach-Object { Add-MayaCandidate $_ 'ENV:DCC_ROOTS' } }

    # 3. App Paths 注册表
    $appPathKeys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\maya.exe',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\maya.exe',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\maya.exe'
    )
    foreach ($k in $appPathKeys) {
        if (Test-Path $k) {
            $item = Get-Item -LiteralPath $k -ErrorAction SilentlyContinue
            if ($item) {
                Add-MayaCandidate $item.GetValue('') "AppPaths:$k"
                Add-MayaCandidate $item.GetValue('Path') "AppPathsPath:$k"
            }
        }
    }

    # 4. 卸载注册表
    $uninstallRoots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    Get-ItemProperty -Path $uninstallRoots -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'Autodesk Maya|^Maya\b' } |
        ForEach-Object {
            Add-MayaCandidate $_.InstallLocation "Uninstall:$($_.DisplayName)"
            Add-MayaCandidate $_.DisplayIcon "UninstallIcon:$($_.DisplayName)"
        }

    # 5. Autodesk Maya 注册表
    $autodeskBases = @(
        'HKLM:\SOFTWARE\Autodesk\Maya',
        'HKLM:\SOFTWARE\WOW6432Node\Autodesk\Maya',
        'HKCU:\SOFTWARE\Autodesk\Maya'
    )
    foreach ($base in $autodeskBases) {
        if (Test-Path $base) {
            Get-ChildItem -Path $base -ErrorAction SilentlyContinue | ForEach-Object {
                $item = Get-Item -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
                if ($item) {
                    foreach ($name in @('', 'InstallPath', 'MAYA_INSTALL_LOCATION', 'InstallLocation', 'Path')) {
                        Add-MayaCandidate $item.GetValue($name) "AutodeskReg:$($_.Name)"
                    }
                }
            }
        }
    }

    # 6. .ma / .mb 文件关联
    $assocKeys = @(
        'HKLM:\SOFTWARE\Classes\MayaSceneFile\shell\open\command',
        'HKCU:\SOFTWARE\Classes\MayaSceneFile\shell\open\command',
        'HKLM:\SOFTWARE\Classes\MayaBinaryFile\shell\open\command',
        'HKCU:\SOFTWARE\Classes\MayaBinaryFile\shell\open\command'
    )
    foreach ($k in $assocKeys) {
        if (Test-Path $k) {
            $item = Get-Item -LiteralPath $k -ErrorAction SilentlyContinue
            if ($item) {
                $cmd = $item.GetValue('')
                if ($cmd -match '"?([^"]*?maya\.exe)') { Add-MayaCandidate $Matches[1] "FileAssoc:$k" }
            }
        }
    }

    # 7. 快捷方式兜底（COM 不可用时跳过，不影响其它来源）
    $wsh = $null
    try { $wsh = New-Object -ComObject WScript.Shell } catch { $wsh = $null }
    if ($wsh) {
        $shortcutDirs = @(
            [Environment]::GetFolderPath('StartMenu'),
            [Environment]::GetFolderPath('CommonStartMenu'),
            [Environment]::GetFolderPath('Desktop'),
            [Environment]::GetFolderPath('CommonDesktopDirectory'),
            "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
        ) | Where-Object { $_ -and (Test-Path $_) }
        foreach ($dir in $shortcutDirs) {
            Get-ChildItem -LiteralPath $dir -Filter *.lnk -Recurse -ErrorAction SilentlyContinue |
                ForEach-Object {
                    try {
                        $lnk = $wsh.CreateShortcut($_.FullName)
                        Add-MayaCandidate $lnk.TargetPath "Shortcut:$($_.FullName)"
                    } catch {}
                }
        }
    }

    # 8. 候选根扫描（含脚本常量 $DCC_ROOTS + ExtraRoots + 各盘常见子目录）
    $candidateRoots = [System.Collections.Generic.List[string]]::new()
    foreach ($r in ($DCC_ROOTS + $ExtraRoots)) { if ($r) { $candidateRoots.Add($r) } }
    $commonSubDirs = @(
        'Program Files\Autodesk', 'Program Files (x86)\Autodesk', 'Autodesk',
        'DCC', 'DCC\Autodesk', 'Software\Autodesk', 'Apps\Autodesk'
    )
    try {
        Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | ForEach-Object {
            $driveRoot = $_.Root
            foreach ($sub in $commonSubDirs) {
                $p = Join-Path $driveRoot $sub
                if (Test-Path -LiteralPath $p) { $candidateRoots.Add((Resolve-Path -LiteralPath $p).Path) }
            }
            foreach ($sub in @('Program Files\Autodesk\Maya*', 'Program Files (x86)\Autodesk\Maya*')) {
                $p = Join-Path $driveRoot $sub
                Resolve-Path -Path $p -ErrorAction SilentlyContinue | ForEach-Object { $candidateRoots.Add($_.Path) }
            }
        }
        $candidateRoots = $candidateRoots | Select-Object -Unique
        foreach ($root in $candidateRoots) {
            if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
            Get-ChildItem -LiteralPath $root -Filter 'maya.exe' -Recurse -Depth $MaxDepth -File -ErrorAction SilentlyContinue |
                ForEach-Object { Add-MayaCandidate $_.FullName "Scan:$root" }
        }
    } catch {
        # 某个盘/路径触发终止性错误时，保留前面已找到的候选，不让整个 Maya 检测崩掉
        Log ("安装器: Find-Maya 全盘扫描出错 -> " + $_.Exception.Message)
    }

    # ---- 映射为安装器统一对象（多版本 -> 多 targets，按 $MIN_MAYA 门槛过滤）----
    $good = @{}; $low = @()
    foreach ($c in $cands) {
        $ver = if ($c.VER) { $c.VER } else { 'UNKNOWN' }
        if (-not (Ver-GE $ver $MIN_MAYA)) { $low += $ver; continue }
        if (-not $good.ContainsKey($ver)) { $good[$ver] = $c.PATH }
    }
    if ($good.Count -eq 0) {
        if ($low.Count -eq 0) { return $null }
        return [PSCustomObject]@{
            key='maya'; name='Maya'; version=((Sort-VersionKeys ($low | Select-Object -Unique)) -join ',');
            path=''; min=$MIN_MAYA; src='plugins/maya/asset_bridge_maya';
            targets=@(); ok=$false; note="检测到的 Maya 版本过低（需 >= $MIN_MAYA），无法安装"
        }
    }
    $keys = @(Sort-VersionKeys $good.Keys)
    $targets = @()
    foreach ($k in $keys) {
        $targets += "$(Join-Path $env:USERPROFILE 'Documents')\maya\$k\zh_CN\scripts\asset_bridge_maya"
    }
    return [PSCustomObject]@{
        key='maya'; name='Maya'; version=($keys -join ','); path=$good[$keys[-1]]; min=$MIN_MAYA;
        src='plugins/maya/asset_bridge_maya'; targets=$targets; ok=$true; note=''
    }
}
function Find-SubstancePainter {
    $exes = @()
    # 1) 各盘 DCC 目录 / Adobe 默认目录下的 <子目录>\Adobe Substance 3D Painter.exe
    foreach ($r in ($DCC_ROOTS + @('C:\Program Files\Adobe'))) {
        if (-not (Test-Path $r)) { continue }
        foreach ($d in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            $c = Join-Path $d.FullName 'Adobe Substance 3D Painter.exe'
            if (Test-Path $c) { $exes += $c }
        }
    }
    # 2) Adobe 目录再深一层（有些版本装在 Adobe\<版本目录>\ 下）
    $exes += @(Find-ExeDeep @('C:\Program Files\Adobe') 'Adobe Substance 3D Painter.exe' 2)
    # 3) 快捷方式兜底
    $exes += Find-ExeViaShortcut 'Substance 3D Painter\.exe$'

    $found = @()
    foreach ($e in @($exes | Sort-Object -Unique)) {
        $vi = Get-ExeVerAll $e
        $pv = $vi.pv
        $build = 0
        $parts = @($vi.fv -split '\.')
        if ($parts.Count -ge 4) { [void][int]::TryParse($parts[3], [ref]$build) }   # build 只在 4 段版本里

        $ok = $false; $note = ''
        if (-not $pv) { $note = '无法读取版本信息' }
        elseif (-not (Ver-GE $pv $MIN_SP)) { $note = "版本过低（需 >= $MIN_SP）" }
        elseif ($build -gt 0 -and $build -lt $MIN_SP_BUILD) { $note = "构建号过低（需 >= build $MIN_SP_BUILD，检测到 build $build）" }
        else {
            $ok = $true
            # 注意：SP 的 exe 通常只写 3 段版本（如 10.0.1），build 号拿不到，
            # 此时只校验到主版本，不再因为"读不到 build"就误判成过低。
            if ($build -eq 0) { $note = "该安装未在程序里写构建号，按版本 $pv 判定（要求 >= $MIN_SP build $MIN_SP_BUILD）" }
        }
        $verText = $pv
        if ($build -gt 0) { $verText = "$pv (build $build)" }
        $found += [PSCustomObject]@{
            key='sp'; name='Substance Painter'; version=$verText; path=$e; min="$MIN_SP (build $MIN_SP_BUILD)";
            src='plugins/substance_painter/asset-bridge';
            targets=@("$(Join-Path $env:USERPROFILE 'Documents')\Adobe\Adobe Substance 3D Painter\plugins\asset-bridge");
            ok=$ok; note=$note
        }
    }
    if ($found.Count -eq 0) { return $null }
    $best = @($found | Where-Object { $_.ok } | Select-Object -First 1)
    if ($best.Count -eq 0) { $best = @($found | Select-Object -First 1) }
    return $best[0]
}
function Find-Unreal {
    $roots = @('G:\Engine', 'D:\Engine', 'E:\Engine', 'C:\Program Files\Epic Games', 'D:\Epic Games')
    $engines = @()
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        foreach ($d in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            $ed = Join-Path $d.FullName 'Engine\Binaries\Win64\UnrealEditor.exe'
            if (Test-Path $ed) { $engines += [PSCustomObject]@{ root = $d.FullName; exe = $ed } }
        }
    }
    try {
        foreach ($k in (Get-ChildItem 'HKLM:\SOFTWARE\EpicGames\Unreal Engine' -ErrorAction SilentlyContinue)) {
            $inst = (Get-ItemProperty $k.PSPath).InstalledDirectory
            if ($inst -and (Test-Path $inst)) {
                $ed = Join-Path $inst 'Engine\Binaries\Win64\UnrealEditor.exe'
                if (Test-Path $ed) { $engines += [PSCustomObject]@{ root = $inst; exe = $ed } }
            }
        }
    } catch { }

    $good = @{}; $low = @()
    foreach ($g in $engines) {
        $ver = ''
        $bv = Join-Path $g.root 'Engine\Build\Build.version'      # 引擎自己写的版本文件最准
        if (Test-Path $bv) {
            try {
                $j = Get-Content $bv -Raw -Encoding UTF8 | ConvertFrom-Json
                $ver = "$($j.MajorVersion).$($j.MinorVersion)"
            } catch { }
        }
        if (-not $ver -and ($g.root -match '(\d+\.\d+(?:\.\d+)?)')) { $ver = $Matches[1] }
        if (-not $ver) { continue }
        if (-not (Ver-GE $ver $MIN_UE)) { $low += $ver; continue }
        if (-not $good.ContainsKey($ver)) { $good[$ver] = $g }
    }
    if ($good.Count -eq 0) {
        if ($low.Count -eq 0) { return $null }
        return [PSCustomObject]@{
            key='ue'; name='Unreal Engine'; version=((Sort-VersionKeys ($low | Select-Object -Unique)) -join ',');
            path=''; min=$MIN_UE; src='plugins/unreal/AssetBridge';
            targets=@(); ok=$false; note="检测到的 Unreal Engine 版本过低（需 >= $MIN_UE），无法安装"
        }
    }
    $keys = @(Sort-VersionKeys $good.Keys)
    $targets = @(); $paths = @()
    foreach ($k in $keys) {
        # 引擎级插件：<引擎根>\Engine\Plugins\AssetBridge（对该版本所有工程通用）
        $targets += (Join-Path $good[$k].root 'Engine\Plugins\AssetBridge')
        $paths   += $good[$k].exe
    }
    return [PSCustomObject]@{
        key='ue'; name='Unreal Engine'; version=($keys -join ','); path=$paths[-1]; min=$MIN_UE;
        src='plugins/unreal/AssetBridge'; targets=$targets; ok=$true; note=''
    }
}
function Find-ExeInRoots([string[]]$roots, [string]$leaf) {
    # 各根目录的一级子目录下找 $leaf（本机安装习惯：<盘>\DCC\<软件目录>\<exe>）
    $out = @()
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        foreach ($d in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            $c = Join-Path $d.FullName $leaf
            if (Test-Path $c) { $out += $c }
        }
    }
    return $out
}
function Find-ExeOnPath([string]$leaf) {
    $out = @()
    if ($env:Path) {
        $env:Path -split ';' | Where-Object { $_ } | ForEach-Object {
            $c = Join-Path $_ $leaf
            if (Test-Path $c) { $out += $c }
        }
    }
    return $out
}
function Find-Toolbag {
    # Toolbag 5：插件放 <安装目录>\data\plugin\asset_bridge\（__main__.py 自动加载）
    $exes  = @(Find-ExeInRoots $DCC_ROOTS 'toolbag.exe')
    $exes += Find-ExeDeep $DCC_ROOTS 'toolbag.exe' 3
    $exes += Find-ExeViaShortcut 'toolbag\.exe$'
    $exes += Find-ExeOnPath 'toolbag.exe'
    foreach ($e in @($exes | Sort-Object -Unique)) {
        $ver = Get-ExeVer $e
        if (-not $ver -and ($e -match '(\d+(?:\.\d+)+)'))   { $ver = $Matches[1] }
        if (-not $ver -and ($e -match 'Toolbag[ _-]?(\d+)')) { $ver = $Matches[1] }
        if (-not $ver) { $ver = '5.0' }   # 目录名/文件都读不到版本时按 5.x 兜底
        if (-not (Ver-GE $ver $MIN_TOOLBAG)) { continue }
        return [PSCustomObject]@{
            key='toolbag'; name='Marmoset Toolbag'; version=$ver; path=$e; min=$MIN_TOOLBAG;
            src='plugins/toolbag/asset_bridge';
            targets=@((Join-Path (Split-Path $e) 'data\plugin\asset_bridge'));
            ok=$true; note=''
        }
    }
    return $null
}
function Find-Photoshop {
    # PS 不装插件（编辑器守护外部调用），这里只负责检测 + 记录路径
    $exes  = @(Find-ExeInRoots ($DCC_ROOTS + @('C:\Program Files\Adobe')) 'Photoshop.exe')
    $exes += Find-ExeDeep @('C:\Program Files\Adobe') 'Photoshop.exe' 2
    $exes += Find-ExeViaShortcut 'Photoshop\.exe$'
    $exes += Find-ExeOnPath 'Photoshop.exe'
    foreach ($e in @($exes | Sort-Object -Unique)) {
        $ver = Get-ExeVer $e
        if (-not $ver -and ($e -match '(\d{4})')) { $ver = $Matches[1] }
        $note = ''
        if (-not $ver) { $ver = '未知'; $note = '未能读取版本信息（不影响使用）' }
        return [PSCustomObject]@{
            key='photoshop'; name='Photoshop'; version=$ver; path=$e; min='不限';
            src=''; targets=@(); ok=$true; note=$note; noinstall=$true
        }
    }
    return $null
}
function Find-Sai2 {
    # SAI2 同 PS：不装插件，编辑器守护外部调用
    $exes  = @(Find-ExeInRoots $DCC_ROOTS 'sai2.exe')
    $exes += Find-ExeDeep $DCC_ROOTS 'sai2.exe' 3
    $exes += Find-ExeViaShortcut 'sai2\.exe$'
    $exes += Find-ExeOnPath 'sai2.exe'
    foreach ($e in @($exes | Sort-Object -Unique)) {
        $ver = Get-ExeVer $e
        $note = ''
        if (-not $ver) { $ver = '2.x'; $note = '未能读取版本信息（SAI2 一般不写版本号），按可用处理' }
        return [PSCustomObject]@{
            key='sai2'; name='SAI2'; version=$ver; path=$e; min='不限';
            src=''; targets=@(); ok=$true; note=$note; noinstall=$true
        }
    }
    return $null
}
function Get-ZBrushUserDirByVersion([string]$ver) {
    # 在 %APPDATA%\Maxon 下按版本找 ZBrush 用户资产目录
    #（形如 Zbrush2026.2.1_01AC8E5C / ZBrush2024.0.4_8F683CC8，注意 b 大小写两版都有）。
    # 版本门槛由调用方判；这里只做「年.次」前缀匹配，天然区分新旧两套。
    if (-not $ver) { return $null }
    $base = Join-Path $env:APPDATA 'Maxon'
    if (-not (Test-Path $base)) { return $null }
    $mm = (($ver -split '\.')[0..1]) -join '.'
    foreach ($d in (Get-ChildItem $base -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -match '^Z[Bb]rush(\d+(?:\.\d+)*)_[0-9A-Fa-f]+$') {
            $dv = $Matches[1]
            $dmm = (($dv -split '\.')[0..1]) -join '.'
            # 版本门槛内建在函数里（防呆）：低于 $MIN_ZBRUSH 的旧版用户目录一律不返回
            if ($dmm -eq $mm -and (Ver-GE $dv $MIN_ZBRUSH)) { return $d.FullName }
        }
    }
    return $null
}
function Find-ZBrush {
    # 多源探测 ZBrush 2026.1+（适配器依赖官方 Python SDK，见 $MIN_ZBRUSH）：
    #   ① 卸载注册表（DisplayName 带版本，InstallLocation/UninstallString 反推 exe）
    #   ② 用户资产目录 glob（版本门槛过滤旧版；installpath.txt 交叉验证 exe）
    #   ③ 固定路径 + 各盘 DCC 目录扫描
    # targets = 各版本的用户 ZStartup\ZPlugs64\asset_bridge_zbrush（2026.1 起安装目录只读）
    $found = @{}      # "年.次" -> @{ exe; ver; userDir }
    $reg = {
        param($exe, $ver, $userDir)
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return }
        if (-not $ver -and ($exe -match 'Z[Bb]rush[ _]?(\d{4}(?:\.\d+)*)')) { $ver = $Matches[1] }
        if (-not $ver) { $ver = Get-ExeVer $exe }
        if (-not $ver) { return }
        if (-not (Ver-GE $ver $MIN_ZBRUSH)) { return }
        $mm = (($ver -split '\.')[0..1]) -join '.'
        if (-not $found.ContainsKey($mm)) { $found[$mm] = @{ exe = $exe; ver = $ver; userDir = $userDir } }
    }
    foreach ($rk in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                      'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
        foreach ($k in (Get-ItemProperty $rk -ErrorAction SilentlyContinue)) {
            $n = [string]$k.DisplayName
            if ($n -notmatch 'ZBrush\s*(\d{4}(?:\.\d+)*)') { continue }
            $ver = $Matches[1]
            $exe = ''
            $il = [string]$k.InstallLocation
            if ($il) {
                $c = Join-Path $il 'ZBrush.exe'
                if (Test-Path -LiteralPath $c) { $exe = $c }
            }
            if (-not $exe -and $k.UninstallString -match '^(.*?)\\Uninstall') {
                $root = Split-Path (Split-Path $Matches[1])
                $c = Join-Path $root 'ZBrush.exe'
                if (Test-Path -LiteralPath $c) { $exe = $c }
            }
            if (-not $exe) { continue }
            & $reg $exe $ver (Get-ZBrushUserDirByVersion $ver)
        }
    }
    $maxonBase = Join-Path $env:APPDATA 'Maxon'
    if (Test-Path $maxonBase) {
        foreach ($d in (Get-ChildItem $maxonBase -Directory -ErrorAction SilentlyContinue)) {
            if ($d.Name -match '^Z[Bb]rush(\d+(?:\.\d+)*)_[0-9A-Fa-f]+$') {
                $ver = $Matches[1]
                if (-not (Ver-GE $ver $MIN_ZBRUSH)) { continue }
                $ip = Join-Path $d.FullName 'installpath.txt'
                if (Test-Path $ip) {
                    $root = ''
                    try { $root = ([string](Get-Content $ip -Raw -Encoding UTF8)).Trim() } catch { }
                    if ($root) {
                        $c = Join-Path $root 'ZBrush.exe'
                        if (Test-Path -LiteralPath $c) { & $reg $c $ver $d.FullName }
                    }
                }
            }
        }
    }
    $roots = @('C:\Program Files\Maxon ZBrush 2026', 'C:\Program Files\Maxon ZBrush 2025') + $DCC_ROOTS
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $c = Join-Path $r 'ZBrush.exe'
        if (Test-Path -LiteralPath $c) { & $reg $c '' (Get-ZBrushUserDirByVersion (Get-ExeVer $c)) }
        foreach ($d in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            if ($d.Name -match '^Z[Bb]rush[ _]?(\d{4}(?:\.\d+)*)') {
                $c2 = Join-Path $d.FullName 'ZBrush.exe'
                if (Test-Path -LiteralPath $c2) { & $reg $c2 $Matches[1] (Get-ZBrushUserDirByVersion $Matches[1]) }
            }
        }
    }
    if ($found.Count -eq 0) { return $null }
    $keys = @(Sort-VersionKeys $found.Keys)
    $targets = @(); $notes = @()
    foreach ($k in $keys) {
        $ud = $found[$k].userDir
        if (-not $ud) { $ud = Get-ZBrushUserDirByVersion $k }
        if ($ud) {
            $targets += (Join-Path $ud 'ZStartup\ZPlugs64\asset_bridge_zbrush')
        } else {
            $notes += "版本 $k 未找到用户资产目录（ZBrush 需至少完整运行过一次）"
        }
    }
    return [PSCustomObject]@{
        key='zbrush'; name='ZBrush'; version=($keys -join ','); path=$found[$keys[-1]].exe;
        min=$MIN_ZBRUSH; src='plugins/zbrush/asset_bridge_zbrush';
        targets=$targets; ok=($targets.Count -gt 0); note=($notes -join '；')
    }
}
function Find-AllApps {
    $det = @()
    foreach ($fn in @('Find-Blender','Find-Maya','Find-SubstancePainter','Find-Unreal',
                     'Find-Toolbag','Find-Photoshop','Find-Sai2','Find-ZBrush')) {
        try {
            $r = & $fn
            if ($r) { $det += $r }
        } catch {
            # 单个检测器出错不该拖垮整个安装器：记日志 + 跳过该软件，其余照常展示
            Log ("安装器: 检测 $fn 失败 -> " + $_.Exception.Message)
        }
    }
    # 与「手动指定的路径」（持久化于 tray_config.json -> manual_paths）合并：
    # 自动检测漏掉的软件，指定过一次就一路可用。
    $out = @()
    foreach ($meta in $AB_APPS_META) {
        $auto = $null
        foreach ($a in $det) { if ($a.key -eq $meta.k) { $auto = $a; break } }
        $man = $null
        if ($script:ManualPaths.ContainsKey($meta.k)) {
            try {
                $man = Resolve-ManualApp $meta.k ([string]$script:ManualPaths[$meta.k])
            } catch {
                Log ("安装器: 解析手动指定路径失败 $($meta.k) -> " + $_.Exception.Message)
                $man = $null
            }
        }
        if ($auto -and $man) { $out += (Merge-AppResults $auto $man) }
        elseif ($auto) { $out += $auto }
        elseif ($man)  { $out += $man }
    }
    return $out
}
function Copy-Plugin([string]$src, [string]$dst, [string]$label, $app) {
    try {
        if (-not (Test-Path $src)) { return "源缺失: $src" }
        if (Test-Path $dst) {
            # 旧版本先移到 backups/ 里（绝不就地改名，避免被 DCC 当成插件扫描到）
            $bk = Get-BackupDir ('install_' + $app.key)
            Move-ToBackup $dst $bk | Out-Null
        }
        New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
        Copy-Item $src $dst -Recurse -Force
        Get-ChildItem $dst -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        if ($app) { Write-Marker $dst $app }
        return "已安装 $label -> $dst"
    } catch { return "安装失败 $label : $_" }
}
function Update-EditorDaemonConfig {
    # 把检测到 / 手动指定的 PS、SAI2 路径同步进 tools\editor_daemon.json
    #（守护进程从这里读 exe；找不到的键保留原值，绝不删）。
    # 手动指定优先于自动检测——用户显式选过的路径就是最终意愿。
    $cfgPath = Join-Path $ROOT 'tools\editor_daemon.json'
    $data = $null
    if (Test-Path $cfgPath) {
        try { $data = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $data = $null }
    }
    $apps = @{}
    if ($data -and $data.apps) {
        foreach ($p in @($data.apps.PSObject.Properties)) { $apps[$p.Name] = [string]$p.Value }
    }
    $changed = $false
    foreach ($k in @('photoshop', 'sai2', 'zbrush')) {
        $exe = ''
        if ($script:ManualPaths.ContainsKey($k)) { $exe = [string]$script:ManualPaths[$k] }
        if (-not $exe) {
            $a = $null
            if ($script:IApps) { $a = $script:IApps | Where-Object { $_.key -eq $k } | Select-Object -First 1 }
            if ($a -and $a.ok -and $a.path) { $exe = [string]$a.path }
        }
        if ($exe -and (Test-Path -LiteralPath $exe) -and $apps[$k] -ne $exe) {
            $apps[$k] = $exe
            $changed = $true
        }
    }
    if (-not $changed) { return }
    $srv = 'http://127.0.0.1:27631'
    if ($data -and $data.server_url) { $srv = [string]$data.server_url }
    $out = [ordered]@{ server_url = $srv; apps = $apps }
    try {
        [System.IO.File]::WriteAllText($cfgPath, ($out | ConvertTo-Json),
            (New-Object System.Text.UTF8Encoding($false)))
        Log ('已同步编辑器守护配置: ' + $cfgPath)
    } catch { Log ('写 editor_daemon.json 失败: {0}' -f $_) }
}
function Get-ZBrushHookBlock([string]$pluginDir) {
    # init.py 注入块（marker 包裹，安装/卸载都靠它定位）
    return @"

# >>> AssetBridge zbrush adapter >>>
import sys as _ab_sys, os as _ab_os
_ab_dir = r'$pluginDir'
if _ab_os.path.isdir(_ab_dir) and _ab_dir not in _ab_sys.path:
    _ab_sys.path.insert(0, _ab_dir)
try:
    import asset_bridge_zbrush as _ab
    _ab.autostart()
except Exception as _ab_e:
    print('AssetBridge 适配器自动加载失败: %r' % _ab_e)
# <<< AssetBridge zbrush adapter <<<
"@
}
function Install-ZBrushExtras($app) {
    # Copy-Plugin 已把插件拷到用户 ZStartup\ZPlugs64\asset_bridge_zbrush。
    # 这里补两件 ZBrush 特有的事：
    #   ① 写 ab_config.json（worker 读它拿 server/payload/inbox/outbox，不硬编码）
    #   ② 往安装目录 ZData\Python\init.py 注入自动加载钩子（幂等；失败不阻塞，
    #      用户可 ZScript>Python 面板手动 Load Script）
    $msgs = @()
    $payload = $script:PayloadDir
    if (-not $payload) { $payload = (Join-Path $ROOT 'runtime\payload') }
    foreach ($dst in @($app.targets)) {
        if (-not (Test-Path $dst)) { continue }
        $cfgObj = [ordered]@{
            server_url        = 'http://127.0.0.1:27631'
            payload_dir       = $payload
            inbox_dir         = (Join-Path $payload 'zbrush_inbox')
            outbox_dir        = (Join-Path $payload 'zbrush_outbox')
            poll_interval     = 2.0
            default_to        = 'blender'
            import_settle_sec = 2.0
            upload_timeout    = 300.0
        }
        try {
            [System.IO.File]::WriteAllText((Join-Path $dst 'ab_config.json'),
                ($cfgObj | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
            $msgs += "已写配置 -> $dst\ab_config.json"
        } catch { $msgs += "写配置失败: $_" }
    }
    $initPy = ''
    if ($app.path) { $initPy = Join-Path (Split-Path $app.path) 'ZData\Python\init.py' }
    $hookDir = ''
    if (@($app.targets).Count -gt 0) { $hookDir = @($app.targets)[0] }
    if (-not $initPy -or -not (Test-Path $initPy)) {
        $msgs += "未找到 $initPy（安装目录结构异常？跳过自动加载注入，可手动 Load Script）"
    } elseif (-not $hookDir) {
        $msgs += "无插件目录，跳过 init.py 注入"
    } else {
        try {
            $src = [System.IO.File]::ReadAllText($initPy, [System.Text.Encoding]::UTF8)
            if ($src -notmatch 'AssetBridge zbrush adapter') {
                $out = $src.TrimEnd() + "`r`n" + (Get-ZBrushHookBlock $hookDir)
                [System.IO.File]::WriteAllText($initPy, $out, (New-Object System.Text.UTF8Encoding($false)))
                $msgs += "已注入自动加载钩子 -> $initPy"
            } else {
                $msgs += "init.py 钩子已存在（跳过）"
            }
        } catch {
            $msgs += "注入 init.py 失败（安装目录只读?）: $_ —— 可在 ZScript>Python 面板手动 Load Script"
        }
    }
    return ($msgs -join '  |  ')
}
function Uninstall-ZBrushExtras($app) {
    # 只摘 init.py 钩子；插件目录本体由 Uninstall-App 通用逻辑移 backups
    $msgs = @()
    $initPy = ''
    if ($app.path) { $initPy = Join-Path (Split-Path $app.path) 'ZData\Python\init.py' }
    if ($initPy -and (Test-Path $initPy)) {
        try {
            $src = [System.IO.File]::ReadAllText($initPy, [System.Text.Encoding]::UTF8)
            if ($src -match 'AssetBridge zbrush adapter') {
                $out = [regex]::Replace($src,
                    '(?s)\r?\n?# >>> AssetBridge zbrush adapter >>>.*?# <<< AssetBridge zbrush adapter <<<\r?\n?', '')
                [System.IO.File]::WriteAllText($initPy, $out.TrimEnd() + "`r`n",
                    (New-Object System.Text.UTF8Encoding($false)))
                $msgs += '已移除 init.py 钩子'
            }
        } catch { $msgs += "移除 init.py 钩子失败: $_" }
    }
    return ($msgs -join '  |  ')
}
function Install-App($app) {
    if (-not $app) { return '未检测到该软件（请用下方「指定」按钮手动选择路径）' }
    if ($app.ok -eq $false) { return "无法安装：$($app.note)" }
    if ($app.noinstall) {
        # PS / SAI2 不装插件：把路径同步进守护配置即完成「安装」
        Update-EditorDaemonConfig
        return ($app.name + ' 无需安装插件：贴图会由「编辑器守护」调用本程序打开，保存后自动回传（路径已同步）')
    }
    $targets = @($app.targets)
    if ($targets.Count -eq 0) { return '没有可用的安装目标，请重新检测' }
    $src = Get-AppSourceDir $app.src
    if (-not (Test-Path $src)) { return "源缺失: $src" }
    $msgs = @()
    foreach ($dst in $targets) { $msgs += (Copy-Plugin $src $dst $app.name $app) }
    $msg = ($msgs -join '  |  ')
    if ($app.key -eq 'zbrush') {
        $extra = Install-ZBrushExtras $app
        if ($extra) { $msg = ($msg + '  |  ' + $extra) }
    }
    Log ('安装器: ' + $msg)
    return $msg
}
function Uninstall-App($app) {
    if (-not $app) { return '未检测到该软件' }
    if ($app.key -eq 'zbrush') {
        $extra = Uninstall-ZBrushExtras $app
        if ($extra) { Log ('安装器: ' + $extra) }
    }
    $exist = @()
    if ($app.targets) { $exist = @($app.targets | Where-Object { Test-Path $_ }) }
    if ($exist.Count -eq 0) { return "未安装 $($app.name) 插件，无需卸载" }
    $bk = Get-BackupDir ('uninstall_' + $app.key)
    $n = 0
    foreach ($t in $exist) {
        try { Move-ToBackup $t $bk | Out-Null; $n++ } catch { }
    }
    $msg = "已卸载 $($app.name) 插件（$n 处），备份于 $bk"
    Log ('安装器: ' + $msg)
    return $msg
}
function Get-PluginSourceFromGitHub {
    $branch = 'main'
    $zip = ($script:GitHubRepo -replace '/$', '') + "/archive/refs/heads/$branch.zip"
    $tmp = Join-Path $env:TEMP ('AssetBridgeDL_' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $zipPath = Join-Path $tmp 'repo.zip'
    try {
        Log ('安装器: 从 GitHub 下载 ' + $zip)
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $zip -OutFile $zipPath -ErrorAction Stop
        Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
        # GitHub 压缩包根目录为 仓库名-分支，例如 AssetBridge-main
        $inner = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like 'AssetBridge-*' } | Select-Object -First 1
        if ($inner -and (Test-Path (Join-Path $inner.FullName 'plugins'))) {
            $script:InstallSource = $inner.FullName
            return "已下载到 $($inner.FullName)"
        }
        return '下载完成但结构不符（未找到 plugins/ 目录）'
    } catch {
        return "下载失败: $_"
    }
}
# 把「手动指定的路径」解析成与 Find-* 同结构的应用对象。
#   - 路径可用且版本达标 -> ok=$true
#   - 路径存在但版本过低/读不到版本 -> ok=$false + note 写明原因
#   - 路径根本不是有效安装 -> $null
# 纯函数、不弹窗：Request-ManualPath（用户刚选完，要弹提示）和 Find-AllApps
# （每次刷新都跑，绝不能弹窗）共用它。
function Resolve-ManualApp([string]$key, [string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $null }
    $path = $path.Trim().Trim('"')
    $src = ''
    switch ($key) {
        'blender' { $src = 'plugins/blender/asset_bridge_blender' }
        'maya'    { $src = 'plugins/maya/asset_bridge_maya' }
        'sp'      { $src = 'plugins/substance_painter/asset-bridge' }
        'ue'      { $src = 'plugins/unreal/AssetBridge' }
        'toolbag' { $src = 'plugins/toolbag/asset_bridge' }
        'photoshop' { $src = '' }
        'sai2'      { $src = '' }
        'zbrush'    { $src = 'plugins/zbrush/asset_bridge_zbrush' }
        default   { return $null }
    }
    # 允许直接给安装目录：替他找目录下的可执行文件
    if (Test-Path -LiteralPath $path -PathType Container) {
        $leaf = switch ($key) {
            'blender'    { 'blender.exe' }
            'maya'       { 'maya.exe' }
            'sp'         { 'Adobe Substance 3D Painter.exe' }
            'toolbag'    { 'toolbag.exe' }
            'photoshop'  { 'Photoshop.exe' }
            'sai2'       { 'sai2.exe' }
            'zbrush'     { 'ZBrush.exe' }
            default      { '' }
        }
        if ($leaf) {
            $hit = $null
            foreach ($t in @((Join-Path $path $leaf), (Join-Path $path "bin\$leaf"))) {
                if (Test-Path -LiteralPath $t -PathType Leaf) { $hit = (Resolve-Path -LiteralPath $t).Path; break }
            }
            if (-not $hit) { return $null }
            $path = $hit
        }
    }
    switch ($key) {
        'blender' {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'blender.exe') { return $null }
            $ver = ''
            $parent = Split-Path $path
            if ($parent -match '(\d+\.\d+(?:\.\d+)?)') { $ver = $Matches[1] }
            if (-not $ver) { $ver = Get-ExeVer $path }
            $ok = ($ver -and (Ver-GE $ver $MIN_BLENDER))
            $note = if ($ok) { '' } else { "版本过低或无法识别（需 >= $MIN_BLENDER）" }
            $mm = ''
            if ($ver) { $mm = ($ver -split '\.')[0..1] -join '.' }
            return [PSCustomObject]@{
                key='blender'; name='Blender'; version=$ver; path=$path; min=$MIN_BLENDER;
                src=$src; ok=$ok; note=$note;
                targets=@("$(Join-Path $env:APPDATA 'Blender Foundation')\Blender\$mm\scripts\addons\asset_bridge_blender")
            }
        }
        'maya' {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'maya.exe') { return $null }
            $ver = ''
            if ($path -match 'Maya[_ ]?(\d{4})') { $ver = $Matches[1] }
            if (-not $ver) { $ver = Convert-MayaYear (Get-ExeVer $path) }
            $ok = ($ver -and (Ver-GE $ver $MIN_MAYA))
            $note = if ($ok) { '' } else { "版本过低或无法识别（需 >= $MIN_MAYA）" }
            return [PSCustomObject]@{
                key='maya'; name='Maya'; version=$ver; path=$path; min=$MIN_MAYA;
                src=$src; ok=$ok; note=$note;
                targets=@("$(Join-Path $env:USERPROFILE 'Documents')\maya\$ver\zh_CN\scripts\asset_bridge_maya")
            }
        }
        'sp' {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
            $vi = Get-ExeVerAll $path
            $pv = $vi.pv; $build = 0
            $parts = @($vi.fv -split '\.')
            if ($parts.Count -ge 4) { [void][int]::TryParse($parts[3], [ref]$build) }
            $ok = $false; $note = ''
            if (-not $pv) { $note = '无法读取版本信息' }
            elseif (-not (Ver-GE $pv $MIN_SP)) { $note = "版本过低（需 >= $MIN_SP）" }
            elseif ($build -gt 0 -and $build -lt $MIN_SP_BUILD) { $note = "构建号过低（需 >= build $MIN_SP_BUILD，检测到 build $build）" }
            else {
                $ok = $true
                if ($build -eq 0) { $note = "该安装未在程序里写构建号，按版本 $pv 判定（要求 >= $MIN_SP build $MIN_SP_BUILD）" }
            }
            $verText = $pv
            if ($build -gt 0) { $verText = "$pv (build $build)" }
            return [PSCustomObject]@{
                key='sp'; name='Substance Painter'; version=$verText; path=$path; min="$MIN_SP (build $MIN_SP_BUILD)";
                src=$src; ok=$ok; note=$note;
                targets=@("$(Join-Path $env:USERPROFILE 'Documents')\Adobe\Adobe Substance 3D Painter\plugins\asset-bridge")
            }
        }
        'ue' {
            # UE 记的是引擎根目录（targets 由根目录推导）
            if (-not (Test-Path -LiteralPath $path -PathType Container)) { return $null }
            $ed = Join-Path $path 'Engine\Binaries\Win64\UnrealEditor.exe'
            if (-not (Test-Path -LiteralPath $ed -PathType Leaf)) { return $null }
            $ver = ''
            $bv = Join-Path $path 'Engine\Build\Build.version'
            if (Test-Path $bv) {
                try {
                    $j = Get-Content $bv -Raw -Encoding UTF8 | ConvertFrom-Json
                    $ver = "$($j.MajorVersion).$($j.MinorVersion)"
                } catch { }
            }
            if (-not $ver -and ($path -match '(\d+\.\d+(?:\.\d+)?)')) { $ver = $Matches[1] }
            $ok = ($ver -and (Ver-GE $ver $MIN_UE))
            $note = if ($ok) { '' } else { "版本过低或无法识别（需 >= $MIN_UE）" }
            return [PSCustomObject]@{
                key='ue'; name='Unreal Engine'; version=$ver; path=$ed; min=$MIN_UE;
                src=$src; ok=$ok; note=$note;
                targets=@(Join-Path $path 'Engine\Plugins\AssetBridge')
            }
        }
        'toolbag' {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'toolbag.exe') { return $null }
            $ver = Get-ExeVer $path
            if (-not $ver -and ($path -match '(\d+(?:\.\d+)+)'))    { $ver = $Matches[1] }
            if (-not $ver -and ($path -match 'Toolbag[ _-]?(\d+)')) { $ver = $Matches[1] }
            if (-not $ver) { $ver = '5.0' }
            $ok = (Ver-GE $ver $MIN_TOOLBAG)
            $note = if ($ok) { '' } else { "版本过低或无法识别（需 >= $MIN_TOOLBAG）" }
            return [PSCustomObject]@{
                key='toolbag'; name='Marmoset Toolbag'; version=$ver; path=$path; min=$MIN_TOOLBAG;
                src=$src; ok=$ok; note=$note;
                targets=@((Join-Path (Split-Path $path) 'data\plugin\asset_bridge'))
            }
        }
        'zbrush' {
            # 版本从路径（Zbrush2026.2.1）或 exe 文件版本读；targets 指向
            # 该版本的用户资产目录 ZStartup\ZPlugs64\asset_bridge_zbrush
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'ZBrush.exe') { return $null }
            $ver = ''
            if ($path -match 'Z[Bb]rush[ _]?(\d{4}(?:\.\d+)*)') { $ver = $Matches[1] }
            if (-not $ver) { $ver = Get-ExeVer $path }
            $ok = ($ver -and (Ver-GE $ver $MIN_ZBRUSH))
            $note = if ($ok) { '' } else { "版本过低或无法识别（需 >= $MIN_ZBRUSH，Python API 2026.0 起才有）" }
            $tg = @()
            if ($ok) {
                $ud = Get-ZBrushUserDirByVersion $ver
                if ($ud) {
                    $tg = @((Join-Path $ud 'ZStartup\ZPlugs64\asset_bridge_zbrush'))
                } else {
                    $ok = $false
                    $note = '未找到该版本的用户资产目录（ZBrush 需至少完整运行过一次）'
                }
            }
            return [PSCustomObject]@{
                key='zbrush'; name='ZBrush'; version=$ver; path=$path; min=$MIN_ZBRUSH;
                src=$src; ok=$ok; note=$note; targets=$tg
            }
        }
        'photoshop' {
            # 不装插件：只校验 exe 存在，路径随后同步进 editor_daemon.json
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'Photoshop.exe') { return $null }
            $ver = Get-ExeVer $path
            $note = ''
            if (-not $ver) { $ver = '未知'; $note = '未能读取版本信息（不影响使用）' }
            return [PSCustomObject]@{
                key='photoshop'; name='Photoshop'; version=$ver; path=$path; min='不限';
                src=''; ok=$true; note=$note; targets=@(); noinstall=$true
            }
        }
        'sai2' {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                [IO.Path]::GetFileName($path) -ine 'sai2.exe') { return $null }
            $ver = Get-ExeVer $path
            $note = ''
            if (-not $ver) { $ver = '2.x'; $note = '未能读取版本信息（SAI2 一般不写版本号），按可用处理' }
            return [PSCustomObject]@{
                key='sai2'; name='SAI2'; version=$ver; path=$path; min='不限';
                src=''; ok=$true; note=$note; targets=@(); noinstall=$true
            }
        }
    }
    return $null
}

# 合并「自动检测结果」与「手动指定结果」：
#  - 只有一边有效 -> 用那一边
#  - 两边都有效 -> targets / 版本 / 路径取并集（检测到 5.0、手动指定 5.2 时两个版本都装）
#  - 两边都无效 -> 以自动检测的说明为主（它有更全的上下文）
function Merge-AppResults($auto, $man) {
    if (-not $auto) { return $man }
    if (-not $man)  { return $auto }
    if ($man.ok -and -not $auto.ok) { return $man }
    if (-not $man.ok) { return $auto }
    $tg = @()
    foreach ($t in (@($auto.targets) + @($man.targets))) { if ($t -and ($tg -notcontains $t)) { $tg += $t } }
    $vers = @()
    foreach ($v in @($auto.version, $man.version)) {
        foreach ($one in @($v -split ',')) { $one = $one.Trim(); if ($one -and ($vers -notcontains $one)) { $vers += $one } }
    }
    $paths = @()
    foreach ($p in @($auto.path, $man.path)) { if ($p -and ($paths -notcontains $p)) { $paths += $p } }
    $notes = @()
    foreach ($n in @($auto.note, $man.note)) { if ($n -and ($notes -notcontains $n)) { $notes += $n } }
    return [PSCustomObject]@{
        key=$auto.key; name=$auto.name; version=($vers -join ','); path=($paths -join ' ; ');
        min=$auto.min; src=$auto.src; targets=$tg; ok=$true; note=($notes -join '；')
    }
}

function Request-ManualPath([string]$key) {
    # 未自动检测到时，让用户手动指定软件路径；返回与 Find-* 同结构的对象，或 $null。
    # 指定成功后会写进 tray_config.json（manual_paths）——以后刷新/重开安装器都认。
    $path = $null
    switch ($key) {
        'blender' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'Blender 可执行文件|blender.exe'
            $ofd.Title  = '选择 blender.exe'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'maya' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'Maya 可执行文件|maya.exe'
            $ofd.Title  = '选择 maya.exe'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'sp' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'Substance 3D Painter|*.exe'
            $ofd.Title  = '选择 Substance 3D Painter.exe'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'ue' {
            $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
            $fbd.Description = '选择 Unreal Engine 安装根目录（需包含 Engine\Binaries\Win64\UnrealEditor.exe）'
            $fbd.ShowNewFolderButton = $false
            if ($fbd.ShowDialog() -eq 'OK') { $path = $fbd.SelectedPath }
        }
        'toolbag' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'Marmoset Toolbag|toolbag.exe'
            $ofd.Title  = '选择 toolbag.exe（Toolbag 5）'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'zbrush' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'ZBrush|ZBrush.exe'
            $ofd.Title  = '选择 ZBrush.exe（2026.1+，需完整运行过一次以生成用户目录）'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'photoshop' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'Photoshop|Photoshop.exe'
            $ofd.Title  = '选择 Photoshop.exe'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
        'sai2' {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Filter = 'SAI2|sai2.exe'
            $ofd.Title  = '选择 sai2.exe'
            if ($ofd.ShowDialog() -eq 'OK') { $path = $ofd.FileName }
        }
    }
    if (-not $path) { return $null }
    $app = Resolve-ManualApp $key $path
    if (-not $app) {
        [System.Windows.Forms.MessageBox]::Show(
                ('路径无效：未能在该路径找到有效的程序。' + "`n" + $path +
                 "`n`nBlender / Maya / Substance Painter / Toolbag / Photoshop / SAI2 请选择对应的 .exe；UE 请选择引擎根目录。"),
                '路径无效', 'OK', 'Warning') | Out-Null
        return $null
    }
    if (-not $app.ok) {
        [System.Windows.Forms.MessageBox]::Show(
            ('该 {0} {1}。' -f $app.name, $app.note) + "`n" + ('路径: {0}' -f $path),
            '版本不符', 'OK', 'Warning') | Out-Null
        return $null
    }
    # 记住这次手动指定（持久化），之后 Find-AllApps 每次都会带上它
    $script:ManualPaths[$key] = $path
    try { Save-Config } catch { Log ('保存手动路径失败: {0}' -f $_) }
    Log ('安装器: 已记住手动指定路径 {0} -> {1}' -f $key, $path)
    return $app
}

# ---- 安装器界面 ----
# 控件引用放脚本作用域：.NET 事件回调是在消息循环里执行的，届时 Show-Installer 的
# 局部变量早已不在作用域内。全部状态刷新交给脚本级函数 Update-InstallerUI。
$script:IReport       = $null
$script:IStatus       = $null
$script:IInstallBtns  = @{}
$script:IUninstallBtns = @{}
$script:IStatusLabel  = @{}
$script:IApps         = @()

function Update-InstallerUI {
    $script:IApps = @(Find-AllApps)
    $byKey = @{}
    foreach ($a in $script:IApps) { $byKey[$a.key] = $a }
    $lines = @()
    foreach ($meta in $AB_APPS_META) {
        $a  = $byKey[$meta.k]
        $ib = $script:IInstallBtns[$meta.k]
        $ub = $script:IUninstallBtns[$meta.k]
        if (-not $a) {
            $lines += ("{0,-18} 未检测到（可点下方『指定』按钮手动选择）" -f $meta.n)
            if ($ib) { $ib.Enabled = $false; $ib.Text = '安装 ' + $meta.s }
            if ($ub) { $ub.Enabled = $false }
            $sl = $script:IStatusLabel[$meta.k]
            if ($sl) { $sl.Text = '未检测到' }
            $lines += ''
            continue
        }
        $st   = Get-InstallState $a
        if ($a.noinstall) { $st.text = '无需安装插件（由编辑器守护调用）' }
        # 行内短标签（完整状态仍在顶部报告里）
        $short = if ($a.noinstall) { '无需装' }
                 elseif ($st.latest) { '已安装' }
                 elseif ($st.installed) { '可重装' }
                 else { '未安装' }
        $sl = $script:IStatusLabel[$meta.k]
        if ($sl) { $sl.Text = $short }
        $flag = if ($a.ok) { '✓ 可安装' } else { '✗ 不可安装' }
        $lines += ("{0,-18} {1,-10} [{2}] {3}" -f $meta.n, ('v' + $a.version), $flag, $st.text)
        if ($a.path) { $lines += ("   程序: " + $a.path) }
        $lines += ("   最低要求 >= " + $a.min)
        if ($a.note) { $lines += ("   注意: " + $a.note) }
        if ($script:ManualPaths.ContainsKey($meta.k)) {
            $lines += ("   手动指定: " + $script:ManualPaths[$meta.k] + "（已记住，刷新后仍生效）")
        }
        foreach ($t in @($a.targets)) { $lines += ("   安装到: " + $t) }
        if ($ib) {
            $ib.Enabled = [bool]$a.ok
            $ib.Text = $(if ($a.noinstall) { '同步 ' + $meta.s }
                        elseif ($st.installed) { '更新 ' + $meta.s }
                        else { '安装 ' + $meta.s })
        }
        if ($ub) { $ub.Enabled = [bool]$st.installed }
        $lines += ''
    }
    if ($script:InstallSource) { $lines += ("安装源: " + $script:InstallSource) }
    else { $lines += ('安装源: ' + $ROOT + '\plugins（本仓库）') }
    if ($script:IReport) { $script:IReport.Text = [string]::Join([Environment]::NewLine, $lines) }
    # 检测/指定完 PS、SAI2 后，把路径同步进编辑器守护配置（有变化才写盘）
    Update-EditorDaemonConfig
}

function Show-Installer {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Asset Bridge 插件安装器'
    $form.ClientSize = New-Object System.Drawing.Size(560, 644)
    $form.StartPosition = 'CenterParent'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false; $form.MinimizeBox = $false

    $report = New-Object System.Windows.Forms.TextBox
    $report.SetBounds(14, 14, 532, 240)
    $report.Multiline = $true; $report.ReadOnly = $true; $report.ScrollBars = 'Vertical'
    $report.Font = New-Object System.Drawing.Font('Consolas', 9)
    $form.Controls.Add($report)

    $status = New-Object System.Windows.Forms.Label
    $status.SetBounds(14, 262, 532, 20)
    $form.Controls.Add($status)

    $bRefresh = New-Object System.Windows.Forms.Button
    $bRefresh.Text = '刷新检测'; $bRefresh.SetBounds(14, 288, 100, 30)
    $form.Controls.Add($bRefresh)

    $bDl = New-Object System.Windows.Forms.Button
    $bDl.Text = '从 GitHub 下载最新版'; $bDl.SetBounds(124, 288, 160, 30)
    $form.Controls.Add($bDl)

    $bInstall = @{}
    $bUninstall = @{}
    $bManual = @{}
    $bStatus = @{}

    # 每软件一行：[软件名 88] [主按钮 190x28] [指定📁 34x28] [卸载🗑 34x28] [状态 120]
    # 21 个按钮从「三组纵排」压成 7 行，消除窗口溢出。主按钮文字与事件回调逻辑不变。
    $rowY0 = 352; $rowH = 34
    $tip = New-Object System.Windows.Forms.ToolTip
    $tip.AutoPopDelay = 8000; $tip.InitialDelay = 400; $tip.ReshowDelay = 100
    $iconSrc20 = Join-Path $ScriptDir 'icons'   # 20px 行内图标（act_folder/act_trash）

    $lbl1 = New-Object System.Windows.Forms.Label
    $lbl1.Text = '按软件操作（每行一个软件）：'; $lbl1.SetBounds(14, 328, 532, 18)
    $form.Controls.Add($lbl1)
    $idx = 0
    foreach ($meta in $AB_APPS_META) {
        $by = $rowY0 + ($rowH * $idx)

        $nl = New-Object System.Windows.Forms.Label
        $nl.Text = $meta.s; $nl.TextAlign = 'MiddleLeft'
        $nl.SetBounds(14, $by + 4, 88, 22)
        $form.Controls.Add($nl)

        $b = New-Object System.Windows.Forms.Button
        $b.Text = '安装 ' + $meta.s; $b.SetBounds(106, $by, 190, 28)
        $b.Enabled = $false
        $b.Tag = $meta.k
        $tip.SetToolTip($b, ('一键安装/更新 {0} 插件（PS/SAI2 为同步守护配置）' -f $meta.n))
        $bInstall[$meta.k] = $b
        $form.Controls.Add($b)

        $bm = New-Object System.Windows.Forms.Button
        $bm.Text = '📁'; $bm.SetBounds(302, $by, 34, 28)
        $bm.Tag = $meta.k
        $imgF = Get-AbImage 'act_folder.png' $iconSrc20
        if ($imgF -ne $null) { $bm.Text = ''; $bm.Image = $imgF; $bm.ImageAlign = 'MiddleCenter' }
        $tip.SetToolTip($bm, ('手动指定 {0} 的安装路径（UE 请选引擎根目录），指定后永久记住' -f $meta.n))
        $bManual[$meta.k] = $bm
        $form.Controls.Add($bm)

        $bu = New-Object System.Windows.Forms.Button
        $bu.Text = '🗑'; $bu.SetBounds(340, $by, 34, 28)
        $bu.Enabled = $false
        $bu.Tag = $meta.k
        $imgT = Get-AbImage 'act_trash.png' $iconSrc20
        if ($imgT -ne $null) { $bu.Text = ''; $bu.Image = $imgT; $bu.ImageAlign = 'MiddleCenter' }
        $tip.SetToolTip($bu, ('卸载 {0} 的插件（先备份到 backups/，可回滚）' -f $meta.n))
        $bUninstall[$meta.k] = $bu
        $form.Controls.Add($bu)

        $sl = New-Object System.Windows.Forms.Label
        $sl.Text = ''; $sl.TextAlign = 'MiddleLeft'
        $sl.ForeColor = [System.Drawing.Color]::Gray
        $sl.SetBounds(382, $by + 4, 120, 22)
        $bStatus[$meta.k] = $sl
        $form.Controls.Add($sl)

        $idx++
    }

    $bClose = New-Object System.Windows.Forms.Button
    $bClose.Text = '关闭'; $bClose.SetBounds(232, 600, 96, 30)
    $bClose.add_Click({ $form.Close() })
    $form.Controls.Add($bClose)

    # 控件交给脚本作用域（事件回调触发时 Show-Installer 的局部变量已不可见）
    $script:IReport        = $report
    $script:IStatus        = $status
    $script:IInstallBtns   = $bInstall
    $script:IUninstallBtns = $bUninstall
    $script:IStatusLabel   = $bStatus

    $bRefresh.add_Click({
        $script:IStatus.Text = '正在检测...'
        $script:IStatus.Update()
        Update-InstallerUI
        $script:IStatus.Text = '已重新检测  ' + (Get-Date -Format 'HH:mm:ss')
    })
    $bDl.add_Click({
        $script:IStatus.Text = '正在从 GitHub 下载...'
        $script:IStatus.Update()
        $script:IStatus.Text = (Get-PluginSourceFromGitHub)
        Update-InstallerUI
    })
    foreach ($meta in $AB_APPS_META) {
        $bInstall[$meta.k].add_Click({
            param($sender, $e)
            try {
                $a = $script:IApps | Where-Object { $_.key -eq $sender.Tag } | Select-Object -First 1
                if (-not $a) { $script:IStatus.Text = '未检测到该软件，请用「指定」按钮手动选择路径'; return }
                $script:IStatus.Text = (Install-App $a)
                Update-InstallerUI
            } catch {
                $script:IStatus.Text = ('操作失败：{0}（详见日志）' -f $_.Exception.Message)
                Log ('安装器: 安装事件出错 -> ' + $_.Exception.Message)
            }
        })
        $bUninstall[$meta.k].add_Click({
            param($sender, $e)
            try {
                $a = $script:IApps | Where-Object { $_.key -eq $sender.Tag } | Select-Object -First 1
                if (-not $a) { return }
                $r = [System.Windows.Forms.MessageBox]::Show(
                    ("确定卸载 " + $a.name + " 的 Asset Bridge 插件吗？`n`n将从以下位置移除（会先备份到 backups/，可回滚）：`n" + (@($a.targets) -join "`n")),
                    '确认卸载', 'YesNo', 'Warning')
                if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
                $script:IStatus.Text = (Uninstall-App $a)
                Update-InstallerUI
            } catch {
                $script:IStatus.Text = ('操作失败：{0}（详见日志）' -f $_.Exception.Message)
                Log ('安装器: 卸载事件出错 -> ' + $_.Exception.Message)
            }
        })
        $bManual[$meta.k].add_Click({
            param($sender, $e)
            try {
                $app = Request-ManualPath $sender.Tag
                if ($app) {
                    $script:IStatus.Text = (Install-App $app)
                    Update-InstallerUI
                }
            } catch {
                $script:IStatus.Text = ('操作失败：{0}（详见日志）' -f $_.Exception.Message)
                Log ('安装器: 指定路径事件出错 -> ' + $_.Exception.Message)
            }
        })
    }

    Set-FormIcon $form
    Update-InstallerUI
    [void]$form.ShowDialog()
}

# ---- 自检定时器 -----------------------------------------------------------
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $script:Interval * 1000
$timer.add_Tick({
    $up = Test-Server
    $script:LastUp = $up
    if (-not $up -and $script:Guard) {
        Log '检测到服务未运行 -> 自动拉起'
        Start-Server
    }
    Refresh-State
    # D3：ZBrush 落盘通知（守护进程写 notify.json -> 气泡展示路径，点击复制，10s 自动消失）
    try {
        $nf = Join-Path $EDITOR_STATE 'notify.json'
        if (Test-Path $nf) {
            $nj = Get-Content $nf -Raw -Encoding UTF8 | ConvertFrom-Json
            $script:AbNotifyPath = [string]$nj.path
            if ($script:AbNotifyPath) {
                $ni.ShowBalloonTip(10000, 'AssetBridge · ZBrush 接收',
                    ("模型已就绪，请在 ZBrush 里 Tool:Load 手动加载：`n" + $script:AbNotifyPath +
                     "`n（点击本气泡复制路径；右键托盘菜单也可复制）"), 'Info')
            }
            Remove-Item $nf -Force -ErrorAction SilentlyContinue
        }
    } catch { }
})

# 双击托盘图标 -> 状态窗
$ni.add_DoubleClick({ Show-StatusWindow })

# ---- 启动 ----------------------------------------------------------------
Load-Config
$timer.Interval = $script:Interval * 1000
Log ('托盘启动 port={0} interval={1}' -f $script:Port, $script:Interval)

Refresh-State
if (-not $script:LastUp -and $script:Guard) {
    Log '启动时服务未运行 -> 拉起'
    Start-Server
}
$ni.Visible = $true
$timer.Start()

# 全局兜底：把 UI 线程上任何"未处理异常"从 JIT 崩溃对话框变成托盘内提示 + 写日志。
# 没有这段，任一事件回调里的 cmdlet 出错（ErrorActionPreference=Stop 下尤其致命）
# 都会弹出 ".NET 实时(JIT)调试" 崩溃框并直接杀死整个托盘进程。
try {
    [System.Windows.Forms.Application]::SetUnhandledExceptionMode('CatchException')
    [System.Windows.Forms.Application]::add_ThreadException({
        param($sender, $e)
        try { Log ('未处理异常: ' + $e.Exception.ToString()) } catch { }
        try {
            [System.Windows.Forms.MessageBox]::Show(
                ('操作时发生错误，托盘已保持运行（未退出）。' + "`n`n" + $e.Exception.Message +
                 "`n`n详细信息已写入日志 tray.log"),
                'AssetBridge', 'OK', 'Error') | Out-Null
        } catch { }
    })
} catch { }

[System.Windows.Forms.Application]::Run()

# 消息循环已结束（正常退出路径）：释放互斥量、删掉锁文件，避免下次起不来
try { $mutex.ReleaseMutex() } catch { }
try { $mutex.Dispose() } catch { }
try { if (Test-Path $LOCKFILE) { Remove-Item $LOCKFILE -Force } } catch { }
try { $ni.Visible = $false; $ni.Dispose() } catch { }
