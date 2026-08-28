# OpenZLTravel 地点目录恢复说明

从 [Google Drive](https://drive.google.com/file/d/1bfyU_XFjQcnFIaAehMtYXhZZdvu1RdXQ/view?usp=drive_link) 下载并解压 `openzltravel-backups.zip`，取得 `openzltravel-catalog-20260828.dump`。本备份只包含 PostgreSQL 的 `catalog` Schema，不包含账号密码、Assistant 会话、LangGraph Checkpoint 或历史行程。

## 1. 校验文件

将备份文件放到项目根目录，然后运行：

```powershell
$expected = "3935F6AD76B2210DB91D33CCF643983426814C3EE3A0323C55FC00185A6332A0"
$actual = (Get-FileHash -Algorithm SHA256 .\openzltravel-catalog-20260828.dump).Hash
$actual -eq $expected
```

结果必须为 `True`。

## 2. 启动 PostGIS

准备好 `backend/.env` 和 `backend/.env.catalog.local`，然后在项目根目录运行：

```powershell
docker compose --env-file backend/.env --env-file backend/.env.catalog.local up -d catalogdb
```

## 3. 恢复地点目录

下面的操作会替换目标数据库中已有的 `catalog` Schema：

```powershell
docker cp .\openzltravel-catalog-20260828.dump openzltravelcatalog:/tmp/openzltravel-catalog.dump
docker exec openzltravelcatalog psql `
  -U catalogowner `
  -d openzltravelcatalog `
  -v ON_ERROR_STOP=1 `
  -c "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS ltree; CREATE EXTENSION IF NOT EXISTS pg_trgm;"

docker exec openzltravelcatalog pg_restore `
  -U catalogowner `
  -d openzltravelcatalog `
  --clean `
  --if-exists `
  --no-owner `
  --no-privileges `
  --exit-on-error `
  /tmp/openzltravel-catalog.dump
```

## 4. 恢复只读权限

使用仓库中的 `docs/data/restore-access.sql` 恢复运行时所需的只读权限：

```powershell
Get-Content -Raw .\docs\data\restore-access.sql |
  docker exec -i openzltravelcatalog psql -U catalogowner -d openzltravelcatalog

.\catalog.ps1 -ProvisionRuntime
.\catalog.ps1 -Verify
```

公开使用或再分发时，需保留 [数据清单](catalog-20260828.json) 中的数据来源与许可证信息。
