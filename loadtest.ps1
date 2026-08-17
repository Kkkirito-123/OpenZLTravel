param(
    [ValidateSet("Start", "Smoke", "Run", "Results", "Stop", "Probe")]
    [string]$Action = "Smoke",
    [ValidateSet("normal", "slowllm", "raillimit", "amaptimeout", "mixedfailure")]
    [string]$Scenario = "normal",
    [string]$Stages = "10:30,50:60,200:90,500:120",
    [ValidateSet("rail", "amap", "openmeteo", "hotel", "llm")]
    [string]$Provider = "openmeteo",
    [switch]$ConfirmLive
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $projectRoot "compose.loadtest.yml"
$resultsRoot = Join-Path $projectRoot "loadtests\results"
$python = Join-Path $projectRoot "..\..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose -f $composeFile @ComposeArgs
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose 执行失败，退出码：$LASTEXITCODE" }
}

function Wait-LoadtestHealth {
    param([string]$Service, [string]$Name)
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        $containerId = & docker compose -f $composeFile ps -q $Service
        if ($containerId) {
            $health = & docker inspect --format "{{.State.Health.Status}}" $containerId
            if ($health -eq "healthy") { return }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name 未在预期时间内就绪。"
}

function New-RunDirectory {
    param([string]$ScenarioName)
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $directory = Join-Path $resultsRoot "$timestamp-$ScenarioName"
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    return (Resolve-Path -LiteralPath $directory).Path
}

function Start-Environment {
    param([string]$RunDirectory, [string]$ScenarioName)
    $env:LOADTEST_RESULT_DIR = $RunDirectory.Replace("\", "/")
    $env:LOADTEST_SCENARIO = $ScenarioName
    Invoke-Compose -ComposeArgs @("down", "-v")
    Invoke-Compose -ComposeArgs @("up", "-d", "--build", "--force-recreate", "fake-upstream", "app")
    Wait-LoadtestHealth "fake-upstream" "Fake Upstream"
    Wait-LoadtestHealth "app" "OpenZLTravel"
}

function Invoke-Experiment {
    param([string]$ScenarioName, [string]$StageDefinition)
    $runDirectory = New-RunDirectory $ScenarioName
    $env:LOADTEST_STAGES = $StageDefinition
    $processor = Get-CimInstance Win32_Processor | Select-Object -First 1
    $computer = Get-CimInstance Win32_ComputerSystem
    $machine = [ordered]@{
        cpu = $processor.Name
        cores = $processor.NumberOfCores
        logical_processors = $processor.NumberOfLogicalProcessors
        memory_gb = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
        docker_version = (& docker version --format "{{.Server.Version}}")
        scenario = $ScenarioName
        stages = $StageDefinition
        recorded_at = (Get-Date).ToString("o")
    }
    $machine | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $runDirectory "machine.json")
    $locustExitCode = 0
    try {
        Start-Environment $runDirectory $ScenarioName
        & docker compose -f $composeFile run --rm locust
        $locustExitCode = $LASTEXITCODE
        # Locust 停止时可能仍有已返回 202 的后台发现任务，短暂排空后再复制恢复快照。
        Start-Sleep -Seconds 5
    } finally {
        Invoke-Compose -ComposeArgs @("stop", "app", "fake-upstream")
    }
    & docker compose -f $composeFile logs --no-color app |
        Set-Content -Encoding utf8 (Join-Path $runDirectory "app.log")
    & docker compose -f $composeFile logs --no-color fake-upstream |
        Set-Content -Encoding utf8 (Join-Path $runDirectory "fake-upstream.log")
    $runtimeDirectory = Join-Path $runDirectory "runtime"
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $containerId = & docker compose -f $composeFile ps -aq app
    if (-not $containerId) { throw "无法找到已停止的应用容器，不能复制 SQLite 快照。" }
    & docker cp "${containerId}:/data/openzltravel.sqlite3" $runtimeDirectory
    if ($LASTEXITCODE -ne 0) { throw "复制 SQLite 快照失败。" }
    $database = Join-Path $runtimeDirectory "openzltravel.sqlite3"
    Push-Location (Join-Path $projectRoot "backend")
    try {
        & $python -m loadtests.report --results $runDirectory --database $database
    } finally {
        Pop-Location
    }
    $report = Get-Content -Raw -Encoding utf8 (Join-Path $runDirectory "summary.json") |
        ConvertFrom-Json
    Invoke-Compose -ComposeArgs @("down", "-v")
    Write-Host "结果目录：$runDirectory"
    if ($report.http.unhandled_exceptions -gt 0) {
        throw "冒烟中出现未处理异常，请查看 app.log。"
    }
    if ($locustExitCode -ne 0) {
        Write-Warning "Locust 发现失败请求，已保留完整报告；退出码：$locustExitCode"
    }
}

switch ($Action) {
    "Start" {
        $runDirectory = New-RunDirectory $Scenario
        Start-Environment $runDirectory $Scenario
        Write-Host "压测网络已启动；为防止误连真实供应商，容器网络不暴露到宿主机。"
        Write-Host "查看日志：docker compose -f compose.loadtest.yml logs -f app fake-upstream"
        Write-Host "结果目录：$runDirectory"
    }
    "Smoke" { Invoke-Experiment $Scenario "10:30" }
    "Run" { Invoke-Experiment $Scenario $Stages }
    "Results" {
        $latest = Get-ChildItem -LiteralPath $resultsRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { throw "还没有并发实验结果。" }
        Get-Content -Encoding utf8 (Join-Path $latest.FullName "summary.md")
        Write-Host "结果目录：$($latest.FullName)"
    }
    "Stop" { Invoke-Compose -ComposeArgs @("down") }
    "Probe" {
        $arguments = @("-m", "loadtests.provider_probe", $Provider, "--calls", "2")
        if ($ConfirmLive) { $arguments += "--confirm-live" }
        Push-Location (Join-Path $projectRoot "backend")
        try { & $python @arguments } finally { Pop-Location }
    }
}
