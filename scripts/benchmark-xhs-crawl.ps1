#Requires -Version 5.1
<#
.SYNOPSIS
  统计 MediaCrawler 小红书 jsonl 规模，可选跑一次采集并计时（需本机扫码/已保存登录态）。

.DESCRIPTION
  爬虫目录默认 D:\MediaCrawler。统计目录下所有 *.jsonl 行数、字节、按文件列出。
  不加 -StatsOnly 时会执行 main.py（与 run-mediacrawler-xhs-search.ps1 相同参数），用 Stopwatch 计量 wall time，
  并在结束后汇报新增行数、条/分钟（仅当 delta>0）。

.PARAMETER McRoot
  MediaCrawler 根目录。

.PARAMETER StatsOnly
  只打印当前 jsonl 与 base_config 摘要，不启动浏览器。

.EXAMPLE
  cd D:\ai封装
  .\scripts\benchmark-xhs-crawl.ps1 -StatsOnly

.EXAMPLE
  本机已就绪，测一整轮采集耗时：
  .\scripts\benchmark-xhs-crawl.ps1
#>
[CmdletBinding()]
param(
    [string] $McRoot = "D:\MediaCrawler",
    [switch] $StatsOnly
)

$ErrorActionPreference = "Stop"

function Get-JsonlDirStats([string] $Dir) {
    $result = [ordered]@{
        TotalLines = 0
        TotalBytes = 0
        Files      = @()
    }
    if (-not (Test-Path -LiteralPath $Dir)) {
        return [pscustomobject]$result
    }
    $files = Get-ChildItem -LiteralPath $Dir -Filter "*.jsonl" -File -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $lines = (Get-Content -LiteralPath $f.FullName -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
        $result.TotalLines += $lines
        $result.TotalBytes += $f.Length
        $result.Files += [pscustomobject]@{
            Name        = $f.Name
            Lines       = $lines
            Bytes       = $f.Length
            LastWrite   = $f.LastWriteTime
        }
    }
    return [pscustomobject]$result
}

function Read-BaseConfigHints([string] $McRoot) {
    $path = Join-Path $McRoot "config\base_config.py"
    $hints = [ordered]@{
        Keywords            = ""
        MaxNotes            = ""
        CommentsOn = ""
        Concurrency       = ""
        SaveFormat        = ""
    }
    if (-not (Test-Path -LiteralPath $path)) {
        return [pscustomobject]$hints
    }
    $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($raw -match 'KEYWORDS\s*=\s*"([^"]*)"') { $hints.Keywords = $Matches[1] }
    if ($raw -match 'CRAWLER_MAX_NOTES_COUNT\s*=\s*(\d+)') { $hints.MaxNotes = $Matches[1] }
    if ($raw -match 'ENABLE_GET_COMMENTS\s*=\s*(\w+)') { $hints.CommentsOn = $Matches[1] }
    if ($raw -match 'MAX_CONCURRENCY_NUM\s*=\s*(\d+)') { $hints.Concurrency = $Matches[1] }
    if ($raw -match 'SAVE_DATA_OPTION\s*=\s*"([^"]*)"') { $hints.SaveFormat = $Matches[1] }
    return [pscustomobject]$hints
}

$jsonlDir = Join-Path $McRoot "data\xhs\jsonl"
$cfg = Read-BaseConfigHints $McRoot

Write-Host "=== MediaCrawler hints (base_config.py) ===" -ForegroundColor Cyan
Write-Host ("  KEYWORDS: {0}" -f $cfg.Keywords)
Write-Host ("  CRAWLER_MAX_NOTES_COUNT: {0}  (单关键词搜索一轮约上限)" -f $cfg.MaxNotes)
Write-Host ("  ENABLE_GET_COMMENTS: {0}" -f $cfg.CommentsOn)
Write-Host ("  MAX_CONCURRENCY_NUM: {0}" -f $cfg.Concurrency)
Write-Host ("  SAVE_DATA_OPTION: {0}" -f $cfg.SaveFormat)
Write-Host ""

$before = Get-JsonlDirStats $jsonlDir
Write-Host "=== JSONL under $jsonlDir (before) ===" -ForegroundColor Cyan
Write-Host ("  Total lines: {0}  Total bytes: {1}" -f $before.TotalLines, $before.TotalBytes)
$before.Files | ForEach-Object { Write-Host ("  - {0}  lines={1}  size={2}  mtime={3}" -f $_.Name, $_.Lines, $_.Bytes, $_.LastWrite) }

if ($StatsOnly) {
    Write-Host ""
    Write-Host "StatsOnly: no crawl started." -ForegroundColor DarkGray
    exit 0
}

$py = Join-Path $McRoot "venv\Scripts\python.exe"
$main = Join-Path $McRoot "main.py"
if (-not (Test-Path -LiteralPath $py) -or -not (Test-Path -LiteralPath $main)) {
    Write-Host "ERROR: venv or main.py missing under $McRoot" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Starting crawl (QR login if required)..." -ForegroundColor Yellow
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Push-Location -LiteralPath $McRoot
try {
    $env:PYTHONUTF8 = "1"
    # 须用空格分隔参数；勿写 "--platform", "xhs"（会被 Typer 当成子命令名而报错）
    & $py $main --platform xhs --lt qrcode --type search
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    $sw.Stop()
}

$after = Get-JsonlDirStats $jsonlDir
$deltaLines = $after.TotalLines - $before.TotalLines
$sec = [math]::Max($sw.Elapsed.TotalSeconds, 0.001)

Write-Host ""
Write-Host "=== Crawl finished ===" -ForegroundColor Cyan
Write-Host ("  Wall time: {0:N1} s  (exit code {1})" -f $sw.Elapsed.TotalSeconds, $exitCode)
Write-Host ("  Total jsonl lines after: {0}  delta lines this run: {1}" -f $after.TotalLines, $deltaLines)
if ($deltaLines -gt 0) {
    $perMin = $deltaLines / ($sec / 60.0)
    Write-Host ("  Throughput (this run): {0:N2} notes/min (jsonl lines)" -f $perMin)
} else {
    Write-Host "  No new lines detected (same file overwritten, new file elsewhere, or crawl failed before write)." -ForegroundColor DarkYellow
}

exit $exitCode
