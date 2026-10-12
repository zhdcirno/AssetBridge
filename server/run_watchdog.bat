@echo off
REM ===========================================================================
REM  Asset Bridge - one-click watchdog launcher
REM  Double-click this file to start the bridge server in watchdog mode
REM  (it stays alive and is restarted automatically if it exits).
REM  Keep the window open while you work in Blender / Unreal / Maya / SP.
REM  Keep the file pure ASCII: cmd.exe decodes .bat with the local code page.
REM ===========================================================================
call "%~dp0start_bridge_server.bat" --watch
