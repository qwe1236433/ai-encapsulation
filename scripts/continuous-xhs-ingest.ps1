#Requires -Version 5.1
<#
.SYNOPSIS
  长期增量采集编排：周期性跑 MediaCrawler → 把 jsonl 目录全量合并进工厂 Feed（去重 + digest）。

.DESCRIPTION
  这不是「单标签页挂到永远」的插件，而是工业上更稳的做法：
  - 每轮启动一次 Playwright 任务（SAVE_LOGIN_STATE=true 时通常无需重复扫码），写 jsonl；
  - 每轮结束后立刻调用 merge-xhs-feed.ps1，从**整个** jsonl 目录重建 samples.json（key 去重，旧数据保留）；
  - 轮与轮之间休眠 IntervalMinutes，降低对平台的请求压力。

  「源源不断」推荐两窗方案（同一浏览器不关）：
  - 窗 A：D:\\MediaCrawler 内 `python main.py ...`，且 base_config 中 `XHS_ENABLE_SESSION_LOOP=True`（单进程内多轮 search，不反复开小红书）。
  - 窗 B：本脚本加 **-MergeOnly**，只定时 merge jsonl → samples.json，不要每轮再启动爬虫。

  若仍用「每轮重启 main.py」旧模式，可不加 MergeOnly（会反复占 profile / 重复登录风险）。

  合规：须遵守小红书用户协议与适用法律；请自行控制频率与总量，勿用于干扰平台运营。

.PARAMETER IntervalMinutes
  每轮结束后的休眠分钟数（稳健默认 15；MergeOnly 时应略大于 MediaCrawler 的 XHS_SESSION_LOOP_INTERVAL_SEC）。

.PARAMETER McRoot
  MediaCrawler 根目录，默认 D:\MediaCrawler。

.PARAMETER MaxRounds
  0 = 无限循环；N>0 只跑 N 轮后退出（试跑用）。

.PARAMETER MergeOnCrawlError
  爬虫非零退出时仍尝试合并（可拾取已写入的 jsonl）。默认不合并。

.PARAMETER StopFilePath
  若该文件存在则在本轮结束后优雅退出（可手动 New-Item创建）。

.PARAMETER MergeOnly
  仅循环合并 Feed，不调用 MediaCrawler。与「窗 A：main.py 会话循环」配合使用。

.EXAMPLE
  cd D:\ai封装
  .\scripts\continuous-xhs-ingest.ps1

.EXAMPLE
  MediaCrawler 已在另一窗口常驻循环，只合并：
  .\scripts\continuous-xhs-ingest.ps1 -MergeOnly

.EXAMPLE试跑 3 轮，每轮间隔 5 分钟：
  .\scripts\continuous-xhs-ingest.ps1 -MaxRounds 3 -IntervalMinutes 5
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 1440)]
    [int] $IntervalMinutes = 15,
    [string] $McRoot = "",
    [int] $MaxRounds = 0,
    [switch] $MergeOnCrawlError,
    [switch] $MergeOnly,
    [string] $StopFilePath = ""
)

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$repoRoot = Split-Path $here -Parent
Set-Location -LiteralPath $repoRoot

if (-not $McRoot) {
    $McRoot = [Environment]::GetEnvironmentVariable("MEDIACRAWLER_ROOT", "Process")
}
if (-not $McRoot) { $McRoot = "D:\MediaCrawler" }

if (-not $StopFilePath) {
    $StopFilePath = Join-Path $repoRoot "logs\continuous-xhs-ingest.STOP"
}

$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "continuous-xhs-ingest.log"

function Write-IngestLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

$py = Join-Path $McRoot "venv\Scripts\python.exe"
$mainPy = Join-Path $McRoot "main.py"
if (-not $MergeOnly) {
    if (-not (Test-Path -LiteralPath $py) -or -not (Test-Path -LiteralPath $mainPy)) {
        Write-IngestLog "ERROR: MediaCrawler not found: $py or $mainPy"
        exit 1
    }
}

$mergeScript = Join-Path $here "merge-xhs-feed.ps1"
if (-not (Test-Path -LiteralPath $mergeScript)) {
    Write-IngestLog "ERROR: missing $mergeScript"
    exit 1
}

$round = 0
while ($true) {
    if ($MaxRounds -gt 0 -and $round -ge $MaxRounds) {
        Write-IngestLog "MaxRounds=$MaxRounds reached; exit."
        exit 0
    }
    if (Test-Path -LiteralPath $StopFilePath) {
        Write-IngestLog "Stop file present: $StopFilePath ; exit."
        exit 0
    }

    $round++
    $bid = "EXP-CONT-{0}-R{1:D4}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $round
    Write-IngestLog "=== Round $round batch_id=$bid ==="

    $crawlExit = 0
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    if ($MergeOnly) {
        Write-IngestLog "MergeOnly: skip crawl (MediaCrawler应在另一终端会话循环)"
        $sw.Stop()
    }
    else {
        Push-Location -LiteralPath $McRoot
        try {
            $env:PYTHONUTF8 = "1"
            & $py $mainPy --platform xhs --lt qrcode --type search
            $crawlExit = $LASTEXITCODE
        }
        finally {
            Pop-Location
            Set-Location -LiteralPath $repoRoot
            $sw.Stop()
        }
        Write-IngestLog ("Crawl finished in {0:N1}s exit={1}" -f $sw.Elapsed.TotalSeconds, $crawlExit)
    }

    $doMerge = ($crawlExit -eq 0) -or $MergeOnCrawlError
    if (-not $doMerge) {
        Write-IngestLog "Skip merge (crawl error). Sleep ${IntervalMinutes}m ..."
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    Write-IngestLog "Merge -> samples.json (dedupe=key) digest batch_id=$bid"
    & $mergeScript -Dedupe key -BatchId $bid -DigestOut (Join-Path $repoRoot "openclaw\data\xhs-feed\samples.digest.json")
    $mergeExit = $LASTEXITCODE
    Write-IngestLog ("Merge exit={0}" -f $mergeExit)

    Write-IngestLog ("Sleep {0} minutes until next round..." -f $IntervalMinutes)
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
