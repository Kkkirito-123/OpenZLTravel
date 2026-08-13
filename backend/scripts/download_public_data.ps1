<#!
.SYNOPSIS
    下载 OpenZLTravel 离线目录所需的公开原始数据。

.DESCRIPTION
    下载中国全量 OpenStreetMap PBF 和 GeoNames 中国城市文件。
    使用 curl 的断点续传，网络中断后可以重复执行本脚本继续下载。
    原始文件只用于离线构建，不会被后端运行时直接扫描。
#>

$ErrorActionPreference = "Stop"

$dataRoot = Join-Path $PSScriptRoot "..\data\raw"
$osmDirectory = Join-Path $dataRoot "osm"
$geonamesDirectory = Join-Path $dataRoot "geonames"
$osmFile = Join-Path $osmDirectory "china-latest.osm.pbf"
$geonamesFile = Join-Path $geonamesDirectory "CN.zip"

$sources = @(
    @{
        Url = "https://download.geofabrik.de/asia/china-latest.osm.pbf"
        Target = $osmFile
    },
    @{
        Url = "https://download.geonames.org/export/dump/CN.zip"
        Target = $geonamesFile
    }
)

New-Item -ItemType Directory -Force -Path $osmDirectory, $geonamesDirectory | Out-Null

foreach ($source in $sources) {
    Write-Host "下载 $($source.Url)"
    & curl.exe -L --fail --retry 5 --retry-delay 5 -C - $source.Url -o $source.Target
    if ($LASTEXITCODE -ne 0) {
        throw "下载失败：$($source.Url)"
    }
}

Write-Host "公开原始数据下载完成。接下来执行：python scripts/build_catalog.py"
