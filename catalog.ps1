[CmdletBinding(DefaultParameterSetName = "Start")]
param(
    [Parameter(Mandatory, ParameterSetName = "Start")]
    [switch]$Start,

    [Parameter(Mandatory, ParameterSetName = "Build")]
    [switch]$Build,

    [Parameter(Mandatory, ParameterSetName = "Verify")]
    [switch]$Verify,

    [Parameter(Mandatory, ParameterSetName = "Stats")]
    [switch]$Stats,

    [Parameter(Mandatory, ParameterSetName = "Tree", Position = 0)]
    [string]$Tree,

    [Parameter(ParameterSetName = "Tree")]
    [ValidateRange(0, 5)]
    [int]$Depth = 2,

    [Parameter(Mandatory, ParameterSetName = "Runtime")]
    [switch]$ProvisionRuntime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $projectRoot "backend"
$composeFile = Join-Path $projectRoot "compose.yml"
$catalogEnv = Join-Path $backendRoot ".env.catalog.local"
$pythonCommand = Get-Command python -ErrorAction Stop

function Import-CatalogEnvironment {
    <# 只读取本机地点库配置，不把密码或连接串写到终端。 #>
    if (-not (Test-Path -LiteralPath $catalogEnv)) {
        throw "缺少 backend/.env.catalog.local，请从 .env.catalog.example 复制并填写。"
    }
    foreach ($line in Get-Content -LiteralPath $catalogEnv -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
            throw "地点库配置行格式错误，请使用 KEY=VALUE。"
        }
        Set-Item -Path "Env:$($parts[0].Trim())" -Value $parts[1]
    }
}

function Invoke-CatalogModule {
    <# 在 backend 包根目录运行独立 catalog_builder，保证相对数据路径稳定。 #>
    param([Parameter(Mandatory)][string[]]$Arguments)

    Push-Location $backendRoot
    try {
        & $pythonCommand.Source @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "地点目录命令执行失败。"
        }
    }
    finally {
        Pop-Location
    }
}

function Start-CatalogDatabase {
    <# 只启动 PostGIS；Provider 缓存由新运行时的进程内 TTL 缓存负责。 #>
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 服务未运行。"
    }
    & docker compose --env-file $catalogEnv -f $composeFile up -d catalogdb
    if ($LASTEXITCODE -ne 0) {
        throw "PostGIS 容器启动失败。"
    }
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        $health = & docker inspect --format "{{.State.Health.Status}}" openzltravelcatalog 2>$null
        if ($LASTEXITCODE -eq 0 -and $health -eq "healthy") {
            Write-Host "PostGIS 地点目录已就绪：http://127.0.0.1:55432"
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "PostGIS 在 60 秒内没有通过健康检查。"
}

Import-CatalogEnvironment
Start-CatalogDatabase

if ($Start) {
    return
}

if ($Build) {
    Invoke-CatalogModule -Arguments @("-m", "pip", "install", "-e", ".[catalog]")
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.build")
    return
}

if ($ProvisionRuntime) {
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.runtime_access")
    Write-Host "只读账号已创建，请把 travelapp 连接串写入 backend/.env 的 CATALOG_DATABASE_URL。"
    return
}

if ($Verify) {
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.validate", "verify")
}
elseif ($Stats) {
    Invoke-CatalogModule -Arguments @("-m", "catalog_builder.validate", "stats")
}
else {
    Invoke-CatalogModule -Arguments @(
        "-m", "catalog_builder.validate", "tree", $Tree, "--depth", [string]$Depth
    )
}
