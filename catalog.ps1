[CmdletBinding(DefaultParameterSetName = "Start")]
param(
    [Parameter(Mandatory, ParameterSetName = "Start")]
    [switch]$Start,

    [Parameter(Mandatory, ParameterSetName = "Build")]
    [switch]$Build,

    [Parameter(Mandatory, ParameterSetName = "Verify")]
    [switch]$Verify,

    [Parameter(Mandatory, ParameterSetName = "Tree", Position = 0)]
    [string]$Tree,

    [Parameter(ParameterSetName = "Tree")]
    [ValidateRange(0, 5)]
    [int]$Depth = 2,

    [Parameter(Mandatory, ParameterSetName = "Stats")]
    [switch]$Stats,

    [Parameter(Mandatory, ParameterSetName = "Runtime")]
    [switch]$Runtime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$catalogRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$catalogBackend = Join-Path $catalogRoot "backend"
$catalogCompose = Join-Path $catalogRoot "compose.catalog.yml"
$catalogEnvFile = Join-Path $catalogBackend ".env.catalog.local"
$runtimeEnvFile = Join-Path $catalogBackend ".env.runtime.local"
$catalogPython = Join-Path $catalogRoot "..\..\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $catalogPython)) {
    $catalogPython = "python"
}

function New-CatalogEnvironment {
    if (Test-Path -LiteralPath $catalogEnvFile) {
        return
    }

    $catalogBytes = New-Object byte[] 32
    $catalogRandom = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $catalogRandom.GetBytes($catalogBytes)
    }
    finally {
        $catalogRandom.Dispose()
    }
    $catalogPassword = ($catalogBytes | ForEach-Object { $_.ToString("x2") }) -join ""
    $catalogLines = @(
        "# 此文件由 catalog.ps1 生成，只用于本机地点库，不要提交。",
        "CATALOG_POSTGRES_PASSWORD=$catalogPassword",
        "CATALOG_DATABASE_URL=postgresql://catalogowner:${catalogPassword}@127.0.0.1:55432/openzltravelcatalog"
    )
    $catalogUtf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($catalogEnvFile, $catalogLines, $catalogUtf8)
    Write-Host "已创建独立地点库配置：backend/.env.catalog.local"
}

function Initialize-RuntimeCredential {
    $catalogExisting = Get-Content -LiteralPath $catalogEnvFile -Encoding UTF8
    if ($catalogExisting | Where-Object { $_ -match '^TRAVELAPP_POSTGRES_PASSWORD=' }) {
        return
    }

    $catalogBytes = New-Object byte[] 32
    $catalogRandom = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $catalogRandom.GetBytes($catalogBytes)
    }
    finally {
        $catalogRandom.Dispose()
    }
    $catalogPassword = ($catalogBytes | ForEach-Object { $_.ToString("x2") }) -join ""
    $catalogUpdated = @($catalogExisting) + "TRAVELAPP_POSTGRES_PASSWORD=$catalogPassword"
    [System.IO.File]::WriteAllLines(
        $catalogEnvFile,
        $catalogUpdated,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Import-CatalogEnvironment {
    New-CatalogEnvironment
    Initialize-RuntimeCredential
    foreach ($catalogLine in Get-Content -LiteralPath $catalogEnvFile -Encoding UTF8) {
        $catalogValue = $catalogLine.Trim()
        if (-not $catalogValue -or $catalogValue.StartsWith("#")) {
            continue
        }
        $catalogParts = $catalogValue.Split("=", 2)
        if ($catalogParts.Count -ne 2) {
            throw "地点库配置行格式错误：$catalogValue"
        }
        Set-Item -Path "Env:$($catalogParts[0])" -Value $catalogParts[1]
    }
}

function Write-RuntimeEnvironment {
    $catalogUrl = "postgresql://travelapp:$($env:TRAVELAPP_POSTGRES_PASSWORD)" +
        "@127.0.0.1:55432/openzltravelcatalog"
    $catalogLines = @(
        "# 此文件由 catalog.ps1 生成，只包含本机应用运行配置，不要提交。",
        "DATABASE_URL=$catalogUrl",
        "CATALOG_DATABASE_URL=$catalogUrl"
    )
    [System.IO.File]::WriteAllLines(
        $runtimeEnvFile,
        $catalogLines,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Test-DockerReady {
    try {
        & docker info *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Start-DockerEngine {
    if (Test-DockerReady) {
        return
    }

    $catalogDockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $catalogDockerDesktop)) {
        throw "Docker 服务未运行，且没有找到 Docker Desktop。"
    }
    Write-Host "正在启动 Docker Desktop..."
    Start-Process -WindowStyle Hidden -FilePath $catalogDockerDesktop
    for ($catalogAttempt = 1; $catalogAttempt -le 60; $catalogAttempt++) {
        Start-Sleep -Seconds 2
        if (Test-DockerReady) {
            return
        }
    }
    throw "Docker Desktop 在 120 秒内没有就绪。"
}

function Invoke-CatalogCompose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker compose --env-file $catalogEnvFile -f $catalogCompose @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose 命令执行失败。"
    }
}

function Start-CatalogDatabase {
    Import-CatalogEnvironment
    Start-DockerEngine
    Invoke-CatalogCompose -Arguments @("up", "-d")

    for ($catalogAttempt = 1; $catalogAttempt -le 60; $catalogAttempt++) {
        $catalogHealth = & docker inspect `
            --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" `
            openzltravelcatalog 2>$null
        if ($LASTEXITCODE -eq 0 -and $catalogHealth -eq "healthy") {
            Write-Host "地点库已就绪：127.0.0.1:55432/openzltravelcatalog"
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "PostgreSQL 容器在 120 秒内没有通过健康检查。"
}

function Install-CatalogDependencies {
    param([switch]$IncludeOsmium)

    $catalogImports = if ($IncludeOsmium) { "import psycopg, osmium" } else { "import psycopg" }
    & $catalogPython -c $catalogImports 2>$null
    if ($LASTEXITCODE -eq 0) {
        return
    }
    Write-Host "正在安装地点库离线构建依赖..."
    & $catalogPython -m pip install -r (Join-Path $catalogBackend "requirements-data.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "地点库依赖安装失败。"
    }
}

function Initialize-AppSchema {
    $catalogSchema = Join-Path $catalogBackend "database\app.sql"
    Get-Content -LiteralPath $catalogSchema -Raw -Encoding UTF8 |
        & docker exec -i openzltravelcatalog `
        psql -v ON_ERROR_STOP=1 -U catalogowner -d openzltravelcatalog
    if ($LASTEXITCODE -ne 0) {
        throw "业务 app Schema 初始化失败。"
    }
}

function Invoke-CatalogModule {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Push-Location $catalogBackend
    try {
        & $catalogPython @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "地点库命令执行失败。"
        }
    }
    finally {
        Pop-Location
    }
}

if ($Start) {
    Start-CatalogDatabase
    return
}

Start-CatalogDatabase

if ($Build) {
    Install-CatalogDependencies -IncludeOsmium
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.build")
    return
}

if ($Runtime) {
    Install-CatalogDependencies
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.runtime_access")
    Initialize-AppSchema
    Write-RuntimeEnvironment
    Write-Host "已写入本地运行配置：backend/.env.runtime.local"
    return
}

Install-CatalogDependencies
if ($Verify) {
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.validate", "verify")
}
elseif ($Stats) {
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.validate", "stats")
}
else {
    Invoke-CatalogModule -Arguments @(
        "-m", "catalog_builder.validate", "tree", $Tree, "--depth", $Depth
    )
}
