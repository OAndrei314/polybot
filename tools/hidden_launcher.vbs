' Silent PowerShell launcher.
'
' Windows Task Scheduler's "Hidden" flag still flashes a window briefly
' when launching powershell.exe directly. wscript.exe has no UI at all,
' and the WshShell.Run call with 3rd arg = 0 makes the launched process
' truly windowless.
'
' Usage from Task Scheduler:
'   Program:    wscript.exe
'   Arguments:  "C:\Users\ojoca\Documents\polybot\tools\hidden_launcher.vbs" "C:\path\to\your.ps1"

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

Set objShell = CreateObject("WScript.Shell")
sPS = "powershell.exe -NoProfile -ExecutionPolicy Bypass -NonInteractive -File """ & WScript.Arguments(0) & """"
' Run with windowStyle=0 (hidden), bWaitOnReturn=False
objShell.Run sPS, 0, False
