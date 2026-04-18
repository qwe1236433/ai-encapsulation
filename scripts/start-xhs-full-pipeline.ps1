#Requires -Version 5.1
<#
.SYNOPSIS
  一键打开三/四窗闭环：合并 Feed（B）→ 持续数分（C）→ MediaCrawler 小红书（A）→（可选）Hermes 闭环（D）。

.DESCRIPTION
  每个窗独立 powershell.exe -NoExit，标题区分。窗 A 使用 run-mediacrawler-xhs-keywords-watch.ps1：keyword_candidates_for_cli.txt 或 MediaCrawler config/base_config.py、xhs_config.py 变更后自动 taskkill 并重启爬虫。
  加上 -WithHermes 后会额外起窗 D：周期性跑 hermes-closed-loop.ps1，通过 LLM 对数分结果做微调，若通过 keyword_pool 提案会覆盖写 keyword_candidates_for_cli.txt 让窗 A 重启。
  停止：各窗 Ctrl+C，或写入 logs\continuous-xhs-ingest.STOP / continuous-xhs-analytics.STOP / hermes-closed-loop.STOP。

.PARAMETER SkipCrawler
  不启动窗 A（只跑合并 + 数分）。

.PARAMETER WithHermes
  额外启动窗 D：Hermes 闭环调度器，默认 30 分钟一次 tick。

.PARAMETER HermesIntervalMinutes
  窗 D 两次 tick 的间隔；默认 30。

.PARAMETER HermesDryRun
  窗 D 观察模式：LLM 通过 keyword_pool 也不会真改 keyword_candidates_for_cli.txt。

.PARAMETER CrawlerNoAutoRestart
  传给 run-mediacrawler-xhs-keywords-watch.ps1：子进程退出后不自动重启。

.PARAMETER CrawlerGiveUpAfterQuickExits
  传给监视脚本：连续快速退出达此次数后停止自动重启（默认 8）。0=不限。

.PARAMETER CrawlerNoRedirectChildLogs
  传给监视脚本：不重定向 python 输出到 logs。

.PARAMETER WhatIf
  仅打印将执行的启动命令，不新开进程。

.EXAMPLE
  cd D:\ai封装
  .\scripts\start-xhs-full-pipeline.ps1

.EXAMPLE仅合并与数分：
  .\scripts\start-xhs-full-pipeline.ps1 -SkipCrawler
#>
[CmdletBinding()]
param(
    [switch] $SkipCrawler,
    [switch] $CrawlerNoAutoRestart,
    [ValidateRange(0, 100)]
    [int] $CrawlerGiveUpAfterQuickExits = 8,
    [switch] $CrawlerNoRedirectChildLogs,
    [switch] $WithHermes,
    [ValidateRange(1, 1440)]
    [int] $HermesIntervalMinutes = 30,
    [switch] $HermesDryRun,
    [switch] $WhatIf
)

try { chcp 65001 | Out-Null } catch {}

$repo = Split-Path $PSScriptRoot -Parent
$mergeScript = Join-Path $repo "scripts\continuous-xhs-ingest.ps1"
$anaScript = Join-Path $repo "scripts\continuous-xhs-analytics.ps1"
$crawlScript = Join-Path $repo "scripts\run-mediacrawler-xhs-keywords-watch.ps1"
$hermesScript = Join-Path $repo "scripts\hermes-closed-loop.ps1"

foreach ($p in @($mergeScript, $anaScript)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "ERROR: missing $p" -ForegroundColor Red
        exit 1
    }
}
if (-not $SkipCrawler -and -not (Test-Path -LiteralPath $crawlScript)) {
    Write-Host "ERROR: missing $crawlScript" -ForegroundColor Red
    exit 1
}
if ($WithHermes -and -not (Test-Path -LiteralPath $hermesScript)) {
    Write-Host "ERROR: missing $hermesScript (needed for -WithHermes)" -ForegroundColor Red
    exit 1
}

$mc = if ($env:MEDIACRAWLER_ROOT) { $env:MEDIACRAWLER_ROOT } else { "D:\MediaCrawler" }
$pyMc = Join-Path $mc "venv\Scripts\python.exe"
if (-not $SkipCrawler -and -not (Test-Path -LiteralPath $pyMc)) {
    Write-Host "WARN: MediaCrawler Python not found: $pyMc (set MEDIACRAWLER_ROOT or install venv)" -ForegroundColor DarkYellow
}

function Start-PipelineWindow([string] $Label, [string[]] $ArgList) {
    if ($WhatIf) {
        Write-Host "WhatIf [$Label]: powershell.exe $($ArgList -join ' ')" -ForegroundColor DarkGray
        return
    }
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $repo -ArgumentList $ArgList
}

Write-Host ""
Write-Host "Starting XHS pipeline (3 windows)..." -ForegroundColor Cyan
Write-Host "  Repo: $repo"
Write-Host ""

Start-PipelineWindow "merge" @(
    "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", $mergeScript, "-MergeOnly"
)

Start-Sleep -Milliseconds 600

Start-PipelineWindow "analytics" @(
    "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", $anaScript
)

if (-not $SkipCrawler) {
    Start-Sleep -Milliseconds 600
    $cArgs = @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $crawlScript,
        "-GiveUpAfterQuickExits", "$CrawlerGiveUpAfterQuickExits"
    )
    if ($CrawlerNoAutoRestart) { $cArgs += "-NoAutoRestart" }
    if ($CrawlerNoRedirectChildLogs) { $cArgs += "-NoRedirectChildLogs" }
    Start-PipelineWindow "crawler" $cArgs
}

if ($WithHermes) {
    Start-Sleep -Milliseconds 600
    $hArgs = @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $hermesScript,
        "-IntervalMinutes", "$HermesIntervalMinutes"
    )
    if ($HermesDryRun) { $hArgs += "-DryRun" }
    Start-PipelineWindow "hermes" $hArgs
}

Write-Host "Done. Close windows or use STOP files under logs\ to stop loops." -ForegroundColor Green
if ($WithHermes) {
    Write-Host "  Hermes closed-loop STOP file: logs\hermes-closed-loop.STOP" -ForegroundColor DarkGray
    Write-Host "  Hermes tick log:              logs\hermes-closed-loop.log (JSONL)" -ForegroundColor DarkGray
}
Write-Host ""
