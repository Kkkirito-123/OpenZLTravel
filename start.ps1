param(
    [switch]$Install,
    [switch]$Production
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$agentRoot = (Resolve-Path (Join-Path $projectRoot "..\..")).Path
$python = Join-Path $projectRoot "..\..\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

if ($Install) {
    & $python -m pip install -e $agentRoot
    Push-Location $backendRoot
    try { & $python -m pip install -r requirements.txt } finally { Pop-Location }
    Push-Location $frontendRoot
    try { & npm.cmd install } finally { Pop-Location }
}

Write-Host "启动后端: http://127.0.0.1:8000"
$backendArguments = @(
    "-m", "uvicorn", "app.main:app", "--app-dir", $backendRoot,
    "--host", "127.0.0.1", "--port", "8000"
)
if ($Production) {
    $workers = if ($env:WEB_WORKERS) { [int]$env:WEB_WORKERS } else { 4 }
    $backendArguments += @("--workers", [string]$workers)
}
else {
    $backendArguments += "--reload"
}
Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList $backendArguments

Write-Host "启动前端: http://127.0.0.1:5173"
Push-Location $frontendRoot
try { & npm.cmd run dev } finally { Pop-Location }
