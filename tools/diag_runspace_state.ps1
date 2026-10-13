# AssetBridge tray state-task runspace diagnostic (ASCII only, safe for PS 5.1 GBK)
# Usage (real terminal, not sandbox):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\diag_runspace_state.ps1
# Bounded: self-exits after 90s no matter what.
$ErrorActionPreference = 'Continue'
$T0 = Get-Date
function W([string]$m) {
    $dt = [int]((Get-Date) - $T0).TotalMilliseconds
    Write-Host ("[{0,6}ms] {1}" -f $dt, $m)
}

$watchdog = New-Object System.Threading.ThreadStart({
    Start-Sleep -Seconds 90
    Write-Host "[DIAG] HARD TIMEOUT after 90s - something hung; exiting"
    [Environment]::Exit(2)
})
$wt = New-Object System.Threading.Thread($watchdog)
$wt.IsBackground = $true
$wt.Start()

W "step0: read tray script + build worker slice (same logic as Get-WorkerSliceText)"
$src = [System.IO.File]::ReadAllText('E:\CirnoWorkSpace\AssetBridge\tray\assetbridge_tray.ps1', [System.Text.Encoding]::UTF8)
$cut = $src.IndexOf('$script:ReallyExit = $false')
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errs)
$sb = New-Object System.Text.StringBuilder
if ($cut -gt 0) { [void]$sb.Append($src.Substring(0, $cut)) }
foreach ($n in $ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    [void]$sb.AppendLine(''); [void]$sb.AppendLine($n.Extent.Text)
}
foreach ($st in $ast.EndBlock.Statements) {
    if ($st -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { continue }
    $t = $st.Extent.Text
    if ($t -match 'New-Object|\.Add\(|\.Visible|ShowDialog|\.Text\s*=') { continue }
    [void]$sb.AppendLine(''); [void]$sb.AppendLine($t)
}
$slice = $sb.ToString()
W ("step1: slice built, length=" + $slice.Length)

W "step2: Invoke-Expression slice IN MAIN THREAD (STA) - tests slice content alone"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Invoke-Expression $slice
W ("step3: main-thread slice load OK in " + $sw.ElapsedMilliseconds + "ms")

W "step4: run state collection in MAIN THREAD (business logic only)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$d = Get-StateData
W ("step5: state collect OK in " + $sw.ElapsedMilliseconds + "ms -> up=" + $d.up + " pid=" + $d.pid + " edUp=" + $d.edUp)

W "step6: create runspace pool and run SAME slice load inside pool thread (MTA) - 30s cap"
$iss = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
$pool = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(1, 2, $iss, $Host)
$pool.Open()
W "step7: pool opened"
$ps = [PowerShell]::Create()
$null = $ps.RunspacePool = $pool
$null = $ps.AddScript({
    param($sliceText)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Invoke-Expression $sliceText
    return @{ ok = $true; ms = $sw.ElapsedMilliseconds }
})
$null = $ps.AddArgument($slice)
$handle = $ps.BeginInvoke()
$deadline = (Get-Date).AddSeconds(30)
$done = $false
while ((Get-Date) -lt $deadline) {
    if ($handle.IsCompleted) { $done = $true; break }
    Start-Sleep -Milliseconds 200
}
if ($done) {
    $r = $ps.EndInvoke($handle)
    W ("step8: POOL slice load completed ok=" + $r.ok + " in " + $r.ms + "ms -> runspace is FINE, look elsewhere")
} else {
    W "step8: POOL slice load HUNG >30s -> ROOT CAUSE = runspace/MTA + Add-Type in slice head (matches tray.log 30s timeout)"
}
try { $ps.Dispose() } catch { }
try { $pool.Close(); $pool.Dispose() } catch { }
W "step9: done"
