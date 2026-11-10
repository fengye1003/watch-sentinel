$ErrorActionPreference = "SilentlyContinue"
foreach ($TaskName in @("SentinelWatch", "SentinelWatchdog")) {
    $task = Get-ScheduledTask -TaskName $TaskName
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[sentinel] removed: $TaskName"
    }
}
Write-Host "[sentinel] autostart removed"
