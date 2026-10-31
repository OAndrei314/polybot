' Silent .cmd/.bat launcher.
'
' Companion to hidden_launcher.vbs (which wraps .ps1 scripts). Pointing a
' scheduled task straight at a .cmd file makes Task Scheduler spawn a visible
' C:\WINDOWS\SYSTEM32\cmd.exe console window on every run. wscript.exe has no
' UI at all, and the WshShell.Run call with 3rd arg = 0 makes the launched
' process truly windowless.
'
' Usage from Task Scheduler:
'   Program:    wscript.exe
'   Arguments:  "C:\Users\ojoca\Documents\polybot\tools\hidden_cmd_launcher.vbs" "C:\path\to\your.cmd"

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

Set objShell = CreateObject("WScript.Shell")
sCmd = "cmd.exe /c """ & WScript.Arguments(0) & """"
' Run with windowStyle=0 (hidden), bWaitOnReturn=False
objShell.Run sCmd, 0, False
