@echo off
REM ===========================================================================
REM  Asset Bridge - bridge server launcher
REM  IMPORTANT: keep this file pure ASCII. cmd.exe reads .bat with the local
REM  code page (GBK on Chinese Windows); non-ASCII text gets mis-decoded and
REM  can eat line breaks, which corrupts the IF block below.
REM ===========================================================================
cd /d "%~dp0"

REM --watch : run in watchdog mode (keeps the bridge server alive, restarts it
REM           automatically if it dies). Keep the window open.
if "%1"=="--watch" (
    echo [bridge] Starting watchdog mode - the bridge server will be kept alive...
    powershell -ExecutionPolicy Bypass -NoLogo -File "%~dp0bridge_watchdog.ps1"
    goto :eof
)

REM Prefer python from PATH; fall back to the bundled interpreter.
set "PY=python"
where python >nul 2>nul || set "PY=C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe"

"%PY%" "%~dp0server\server.py" --root "%~dp0runtime"
pause
