@echo off
REM ===========================================================================
REM  Asset Bridge - start the tray supervisor.
REM
REM  Recommended: double-click "AssetBridge-Tray.vbs" instead - a .vbs never
REM  creates a console window, while a .bat always flashes one briefly.
REM  This .bat is kept as a fallback in case script-host .vbs files are
REM  blocked on this machine.
REM  KEEP THIS FILE PURE ASCII.
REM ===========================================================================
wscript.exe "E:\CirnoWorkSpace\AssetBridge\tray\assetbridge_tray_launch.vbs"
exit
