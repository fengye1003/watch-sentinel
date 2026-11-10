$ErrorActionPreference = "SilentlyContinue"
if ($PSScriptRoot) { $Root = $PSScriptRoot } else { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Root*app.py*" }
if (-not $procs) {
    Write-Host "[sentinel] not running"
    exit 0
}
$procs | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Host "[sentinel] stopped pid $($_.ProcessId)"
}

