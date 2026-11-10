$ErrorActionPreference = "Stop"
if ($PSScriptRoot) { $Root = $PSScriptRoot } else { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$TaskName = "SentinelWatch"
$DogName = "SentinelWatchdog"

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

# 2026-11-10: use wscript + hidden VBS host. Task Scheduler + powershell.exe
# -WindowStyle Hidden still flashes a console window (the window is created
# before the flag applies). wscript.exe is a GUI-subsystem host, so
# WScript.Shell.Run(cmd, 0, False) gives a truly invisible process.
$vbs = Join-Path $Root "watchdog-hidden.vbs"
if (-not (Test-Path -LiteralPath $vbs)) { throw "missing $vbs" }
$ws = Join-Path $env:SystemRoot "System32\wscript.exe"
$action = New-ScheduledTaskAction -Execute $ws -Argument "`"$vbs`""
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

# 1) start at logon
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logonTrigger `
    -Principal $principal -Settings $settings -Description "Watch Sentinel - start at logon" -Force | Out-Null

# 2) keepalive: run start.ps1 every 5 minutes (it exits immediately if running)
$dogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $DogName -Action $action -Trigger $dogTrigger `
    -Principal $principal -Settings $settings -Description "Watch Sentinel - 5 min keepalive" -Force | Out-Null

Write-Host "[sentinel] autostart installed: '$TaskName' (logon) + '$DogName' (every 5 min)"
