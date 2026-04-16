#Requires -Version 5.1
<#
.SYNOPSIS
  稳定小红书数据链路：MediaCrawler 关键词搜索（二维码登录）→ jsonl 归并为工厂 Feed + digest。

.DESCRIPTION
  - 爬虫程序不在本仓库：默认使用本机 D:\MediaCrawler（NanmiCoder/MediaCrawler），入口为 venv 内 python main.py。
  - 归并层在本仓库：调用 scripts/merge-xhs-feed.ps1（export_to_xhs_feed），与 Flow API / .env 中 FLOW_API_* 习惯一致。
  - 首次或 Cookie 失效时需人工在120 秒内用小红书 App 扫码；无法完全无人化；爬虫成功结束后会自动合并并写入与 digest 对齐的 batch_id。

.PARAMETER SkipCrawl
  不启动爬虫，只执行合并（爬虫已在其他终端跑完，或配合计划任务仅增量归并 jsonl）。

.PARAMETER SkipMerge
  只启动爬虫，不合并。

.PARAMETER McRoot
  MediaCrawler 根目录。默认 D:\MediaCrawler；也可在调用前设环境变量 MEDIACRAWLER_ROOT。

.PARAMETER McArgs
  传给 main.py 的额外参数（字符串数组）。默认等价于：--platform xhs --lt qrcode --type search

.PARAMETER InPath
  合并输入（jsonl 目录或文件）。默认与 merge-xhs-feed.ps1 相同：FLOW_API_MEDIACRAWLER_JSONL 或 D:\MediaCrawler\data\xhs\jsonl。

.PARAMETER OutPath
  合并输出 samples.json。默认 FLOW_API_FEED_OUT 或 openclaw\data\xhs-feed\samples.json。

.PARAMETER Dedupe
  none | key | content。稳定批次建议 key（与实验报告一致）。默认 key。

.PARAMETER BatchId
  写入 digest 的批次号。默认自动生成 EXP-yyyy-MM-dd-HHmmss。

.PARAMETER DigestOut
  digest 侧车 JSON 路径。默认 openclaw\data\xhs-feed\samples.digest.json（与 .env.example 注释一致）。

.PARAMETER ValidateMode
  传给 export_to_xhs_feed：none | report | warn | fail。默认读 .env，否则 report。

.PARAMETER LogPath
  追加写日志；默认仓库根 logs\stable-xhs-pipeline.log

.EXAMPLE
  cd D:\ai封装
  .\scripts\stable-xhs-pipeline.ps1

.EXAMPLE
  爬虫已跑完，只归并并打批次：
  .\scripts\stable-xhs-pipeline.ps1 -SkipCrawl -BatchId "EXP-2026-04-13-BATCH02"

.EXAMPLE
  MediaCrawler 装在其他盘：
  .\scripts\stable-xhs-pipeline.ps1 -McRoot "E:\tools\MediaCrawler"
#>
[CmdletBinding()]
param(
    [switch] $SkipCrawl,
    [switch] $SkipMerge,
    [string] $McRoot = "",
    [string[]] $McArgs = @("--platform", "xhs", "--lt", "qrcode", "--type", "search"),
    [string] $InPath = "",
    [string] $OutPath = "",
    [string] $Dedupe = "key",
    [string] $BatchId = "",
    [string] $DigestOut = "",
    [string] $ValidateMode = "",
    [string] $LogPath = ""
)

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$repoRoot = Split-Path $here -Parent
Set-Location -LiteralPath $repoRoot

if (-not $McRoot) {
    $McRoot = [Environment]::GetEnvironmentVariable("MEDIACRAWLER_ROOT", "Process")
}
if (-not $McRoot) { $McRoot = "D:\MediaCrawler" }

if (-not $LogPath) {
    $logDir = Join-Path $repoRoot "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $LogPath = Join-Path $logDir "stable-xhs-pipeline.log"
}

function Write-PipelineLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Host $Message
}

$bid = $BatchId.Trim()
if (-not $bid) {
    $bid = "EXP-{0}-{1}" -f (Get-Date -Format "yyyy-MM-dd"), (Get-Date -Format "HHmmss")
}

$digestVal = $DigestOut.Trim()
if (-not $digestVal) {
    $digestVal = Join-Path $repoRoot "openclaw\data\xhs-feed\samples.digest.json"
}

Write-PipelineLog ("=== stable-xhs-pipeline start batch_id={0} repo={1} ===" -f $bid, $repoRoot)

$crawlExit = 0
if (-not $SkipCrawl) {
    $py = Join-Path $McRoot "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) {
        Write-PipelineLog ("ERROR: MediaCrawler python not found: {0} (see MediaCrawler联调步骤.md)" -f $py)
        exit 1
    }
    $mainPy = Join-Path $McRoot "main.py"
    if (-not (Test-Path -LiteralPath $mainPy)) {
        Write-PipelineLog ("ERROR: main.py not found: {0}" -f $mainPy)
        exit 1
    }
    $mcCmdLine = $McArgs -join " "
    Write-PipelineLog ("Crawl: {0} main.py {1} (QR login in browser)" -f $py, $mcCmdLine)
    Push-Location -LiteralPath $McRoot
    try {
        $env:PYTHONUTF8 = "1"
        & $py $mainPy @McArgs
        $crawlExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
        Set-Location -LiteralPath $repoRoot
    }
    Write-PipelineLog "Crawl exit code: $crawlExit"
    if ($crawlExit -ne 0) {
        Write-PipelineLog "ERROR: crawl exited non-zero; merge skipped. Use -SkipCrawl to merge only if jsonl is still valid."
        exit $crawlExit
    }
}

if ($SkipMerge) {
    Write-PipelineLog "SkipMerge: done."
    exit 0
}

$mergeScript = Join-Path $here "merge-xhs-feed.ps1"
if (-not (Test-Path -LiteralPath $mergeScript)) {
    Write-PipelineLog ("ERROR: missing merge script: {0}" -f $mergeScript)
    exit 1
}

$mergeParams = @{
    Dedupe      = $Dedupe
    BatchId     = $bid
    DigestOut   = $digestVal
}
if ($InPath) { $mergeParams.InPath = $InPath }
if ($OutPath) { $mergeParams.OutPath = $OutPath }
if ($ValidateMode) { $mergeParams.ValidateMode = $ValidateMode }

Write-PipelineLog ("Merge: merge-xhs-feed.ps1 batch_id={0} dedupe={1}" -f $bid, $Dedupe)
& $mergeScript @mergeParams
$mergeExit = $LASTEXITCODE
Write-PipelineLog ("Merge exit code: {0}" -f $mergeExit)
Write-PipelineLog "=== stable-xhs-pipeline end ==="
exit $mergeExit
