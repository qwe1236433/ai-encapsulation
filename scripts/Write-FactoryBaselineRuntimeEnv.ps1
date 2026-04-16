#Requires -Version 5.1
# 将 auto_baseline_v1.json（或指定 artifact）路径写入 research/runtime/factory_baseline.env
# 供 api/main.py 第二次 load_dotenv 覆盖 XHS_FACTORY_BASELINE_JSON（仅覆盖该文件内声明的键）。
param(
    [Parameter(Mandatory = $true)]
    [string] $BaselineJsonPath,
    [string] $RepoRoot = ""
)
$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }
$item = Get-Item -LiteralPath $BaselineJsonPath
$abs = $item.FullName -replace "\\", "/"
$rt = Join-Path $RepoRoot "research\runtime"
New-Item -ItemType Directory -Force -Path $rt | Out-Null
$out = Join-Path $rt "factory_baseline.env"
$utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$lines = @(
    "# AUTO-GENERATED — do not hand-edit if you rely on continuous-xhs-analytics sync",
    "# updated_utc=$utc",
    "XHS_FACTORY_BASELINE_JSON=$abs"
)
($lines -join "`n") + "`n" | Set-Content -LiteralPath $out -Encoding UTF8
Write-Host "Wrote $out" -ForegroundColor Green
