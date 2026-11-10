$ErrorActionPreference = "Stop"
if ($PSScriptRoot) { $Root = $PSScriptRoot } else { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $Root

$port = 8099
$cfgPath = Join-Path $Root "data\config.json"
if (Test-Path $cfgPath) {
    try { $port = [int](Get-Content $cfgPath -Raw | ConvertFrom-Json).api.port } catch { $port = 8099 }
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[sentinel] python.exe not found in PATH"
    exit 1
}

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Root*app.py*" }
if ($running) {
    Write-Host "[sentinel] already running: pid $($running.ProcessId -join ', ')"
    exit 0
}

$logDir = Join-Path $Root "data"
New-Item -ItemType Directory -Force $logDir | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$appPath = Join-Path $Root "app.py"
Start-Process -FilePath $python.Source -ArgumentList "`"$appPath`"" -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "server.log") `
    -RedirectStandardError (Join-Path $logDir "server.err.log")

Start-Sleep -Seconds 3
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 5
    Write-Host "[sentinel] started, health=$($health.ok), port=$port"
} catch {
    Write-Host "[sentinel] failed to start; see data\server.err.log"
    exit 1
}

