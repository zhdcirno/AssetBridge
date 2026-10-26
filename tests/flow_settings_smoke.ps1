# Flow-settings panel data-model smoke test (no WinForms; validates the data the UI binds to)
$ErrorActionPreference = 'Stop'
$ROOT     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$PROFILES = Join-Path $ROOT 'shared\profiles.json'
if (-not (Test-Path $PROFILES)) { Write-Error ("missing " + $PROFILES); exit 1 }

$prof = Get-Content $PROFILES -Raw -Encoding UTF8 | ConvertFrom-Json
$routes = @($prof.routes.PSObject.Properties)
Write-Host ("route count: " + $routes.Count)

$fail = 0
foreach ($p in $routes) {
    $k   = $p.Name
    $def = $p.Value
    $src = ($k -split '>', 2)[0]
    $fmt = if ($def.format) { [string]$def.format } else { 'obj' }
    $fmts = @()
    try { $fmts = @($prof.apps.$src.formats) } catch { }
    if ([string]$def.kind -eq 'textures') { $fmts = @('textures') + $fmts }
    if ($fmts.Count -eq 0) { $fmts = @($fmt) }
    if (-not ($fmts -contains $fmt)) {
        Write-Host ("  [FAIL] " + $k + " default format " + $fmt + " not in options " + ($fmts -join '/'))
        $fail++
    }
}
foreach ($k in @('blender>unreal','zbrush>unreal','blender>photoshop','unreal>zbrush')) {
    $def = $prof.routes.$k
    $src = ($k -split '>', 2)[0]
    $fmts = @($prof.apps.$src.formats)
    if ([string]$def.kind -eq 'textures') { $fmts = @('textures') + $fmts }
    Write-Host ("  " + $k + " -> default=" + $def.format + " options=" + ($fmts -join '/'))
}

# Noise-filter logic (identical to Format-LogForUI in assetbridge_tray.ps1)
$noise = @('refresh-state called', 'callback ok=', 'task dispatched timeout')
function Strip-Noise($lines) {
    $out = @()
    foreach ($l in $lines) {
        $skip = $false
        foreach ($n in $noise) { if ($l -match [regex]::Escape($n)) { $skip = $true; break } }
        if (-not $skip) { $out += $l }
    }
    return $out
}
$sample = @(
    '2026-10-18 12:00:01 [tray] refresh-state called up=True pid=1234',
    '2026-10-18 12:00:02 [tray] btn=window-server-open click',
    '2026-10-18 12:00:03 [tray] btn=ed task dispatched timeout=30s: ed',
    '2026-10-18 12:00:04 [tray] editor daemon started pid=5678'
)
$filtered = Strip-Noise $sample
if (($filtered -contains $sample[0]) -or ($filtered -contains $sample[2])) {
    Write-Host '  [FAIL] noise not filtered'
    $fail++
} else {
    Write-Host ("  noise filter OK: kept " + $filtered.Count + "/4 lines (dropped refresh-state and timeout noise, kept click/start events)")
}

if ($fail -eq 0) { Write-Host 'FLOW_SETTINGS_SMOKE_OK' } else { Write-Host ("FLOW_SETTINGS_SMOKE_FAIL (" + $fail + ")"); exit 1 }
