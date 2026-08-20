[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "restart", "logs", "ps", "build")]
    [string]$Action = "up"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $projectRoot "compose.yml"
$backendEnv = Join-Path $projectRoot "backend/.env"
$catalogEnv = Join-Path $projectRoot "backend/.env.catalog.local"

function Assert-LocalFile {
    <# 启动前只验证文件存在，不读取或打印其中的密钥。 #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Example
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "缺少 $Path，请先复制 $Example 并填写本机配置。"
    }
}

Assert-LocalFile -Path $backendEnv -Example "backend/.env.example"
Assert-LocalFile -Path $catalogEnv -Example "backend/.env.catalog.example"

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop 未运行。"
}

# 两个 env-file 只交给 Docker Compose：backend/.env 保存应用密钥，
# .env.catalog.local 保存现有 PostGIS volume 对应的本机账号密码。
$composeArgs = @(
    "compose",
    "--env-file", $backendEnv,
    "--env-file", $catalogEnv,
    "-f", $composeFile
)

Push-Location $projectRoot
try {
    switch ($Action) {
        "up" {
            # --remove-orphans 只清理已从统一 Compose 删除的旧容器（如 Redis），
            # 不会删除任何命名 volume。
            & docker @composeArgs up --build --detach --remove-orphans --wait --wait-timeout 180
            if ($LASTEXITCODE -ne 0) { throw "Docker 服务启动失败。" }
            Write-Host "旅行工作台：http://127.0.0.1:5173"
            Write-Host "LangGraph Agent Server：http://127.0.0.1:2024"
        }
        "down" {
            # 故意不使用 --volumes，保留 openzltravelcatalogdata 中的地点目录。
            & docker @composeArgs down
            if ($LASTEXITCODE -ne 0) { throw "Docker 服务停止失败。" }
        }
        "restart" {
            & docker @composeArgs up --build --detach --force-recreate --remove-orphans --wait --wait-timeout 180
            if ($LASTEXITCODE -ne 0) { throw "Docker 服务重启失败。" }
        }
        "logs" { & docker @composeArgs logs --follow --tail 200 }
        "ps" { & docker @composeArgs ps }
        "build" {
            & docker @composeArgs build
            if ($LASTEXITCODE -ne 0) { throw "Docker 镜像构建失败。" }
        }
    }
}
finally {
    Pop-Location
}
