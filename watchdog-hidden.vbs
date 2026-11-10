' Sentinel watchdog hidden runner (invoked by Task Scheduler via wscript.exe)
' Why: Task Scheduler + powershell.exe -WindowStyle Hidden still flashes a console
' window, because the console is created before the flag takes effect. wscript.exe
' is a GUI-subsystem host, so windowStyle=0 from here means no window at all.
' Path is derived from ScriptFullName -> no non-ASCII literal inside this file.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & dir & "\start.ps1" & Chr(34)
sh.Run cmd, 0, False
