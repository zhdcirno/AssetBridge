@echo off
REM ===========================================================================
REM  Asset Bridge - launch the tray supervisor with NO window.
REM
REM  This .bat is only a thin wrapper. It hands over to the VBS launcher which
REM  starts PowerShell with window style 0, so no console window is left behind
REM  (a .bat itself always flashes a cmd window for a moment - if you want zero
REM  flash at all, double-click tray\assetbridge_tray_launch.vbs instead).
REM
REM  The tray keeps the relay server alive by itself, so no cmd window needs to
REM  stay open any more.
REM
REM  KEEP THIS FILE PURE ASCII - cmd.exe reads .bat with the local code page and
REM  non-ASCII text can eat line breaks.
REM ===========================================================================
wscript.exe "%~dp0tray\assetbridge_tray_launch.vbs"
exit
