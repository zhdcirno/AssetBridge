' ===========================================================================
'  Asset Bridge - start the tray supervisor (NO window at all).
'
'  Just double-click this file. The tray icon appears next to the clock:
'    * double-click the icon -> status window (close = back to tray)
'    * right-click the icon  -> start / stop / restart / pause watchdog /
'                               logs / settings / exit
'
'  The tray keeps the relay server (127.0.0.1:27631) alive by itself, so no
'  cmd window has to stay open any more.
'
'  Keep this file pure ASCII (Windows Script Host reads it with the local
'  code page).
' ===========================================================================
Option Explicit

Dim sh, fso, ps1, cmd

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

ps1 = "E:\CirnoWorkSpace\AssetBridge\tray\assetbridge_tray.ps1"

If Not fso.FileExists(ps1) Then
    MsgBox "Tray script not found:" & vbCrLf & ps1, 16, "Asset Bridge"
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
