' ===========================================================================
'  Asset Bridge - launch the tray supervisor with NO window at all.
'
'  Double-click this file. wscript.exe is a GUI-subsystem host, so Windows
'  never creates a console window for it, and window style 0 keeps the child
'  PowerShell window hidden too. The tray icon then appears next to the clock.
'
'  NOTE: keep this file pure ASCII - it is read by the Windows Script Host
'  using the local code page.
'
'  Tray log : E:\CirnoWorkSpace\AssetBridge\logs\tray.log
' ===========================================================================
Option Explicit

Dim sh, fso, here, ps1, cmd

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1  = here & "\assetbridge_tray.ps1"

If Not fso.FileExists(ps1) Then
    MsgBox "Tray script not found:" & vbCrLf & ps1, 16, "Asset Bridge"
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
