$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "SentinelWatch"
$DogName = "SentinelWatchdog"

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
# 2026-11-10: 走 wscript + VBS 壳，避免任务计划程序每 5 分钟闪一个控制台窗口。
# powershell.exe -WindowStyle Hidden 由任务计划程序拉起时，窗口先建后隐藏 => 会闪。
$vbs = Join-Path $Root "watchdog-hidden.vbs"
if (-not (Test-Path $vbs)) { throw "missing $vbs" }
$ws = Join-Path $env:SystemRoot "System32\wscript.exe"
$action = New-ScheduledTaskAction -Execute $ws -Argument "`"$vbs`""
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

# 1) 登录自启
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logonTrigger `
    -Principal $principal -Settings $settings -Description "Watch Sentinel - start at logon" -Force | Out-Null

# 2) 看门狗：每 5 分钟跑一次 start.ps1（已在运行会立刻退出），
#    覆盖睡眠/崩溃/注销后没起来的情况
$dogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $DogName -Action $action -Trigger $dogTrigger `
    -Principal $principal -Settings $settings -Description "Watch Sentinel - 5 min keepalive" -Force | Out-Null

Write-Host "[sentinel] autostart installed: '$TaskName' (logon) + '$DogName' (every 5 min)"
