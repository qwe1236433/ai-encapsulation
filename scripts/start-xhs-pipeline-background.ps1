#Requires -Version 5.1
<#
.SYNOPSIS
  后台启动：默认只跑「合并 jsonl + 持续数分」（稳定）；爬虫需显式 -WithCrawler。

.DESCRIPTION
  默认不启 MediaCrawler：爬虫进程一旦 exit=1，监视脚本若自动重启，浏览器会表现为「开了关、关了开」。
  合并与数分不依赖浏览器，适合作为一键默认。

  需要爬虫时：加 -WithCrawler。默认传 -NoAutoRestart（子进程挂了就停）；要自动重启请加 -CrawlerAllowAutoRestart。

  日志：logs\continuous-xhs-ingest.log；logs\continuous-xhs-analytics.log；爬虫见 logs\mediacrawler-watch.log。
  停止：各窗口 Ctrl+C，或 logs\continuous-xhs-ingest.STOP / continuous-xhs-analytics.STOP。

.PARAMETER WindowStyle
  Minimized（默认）| Hidden | Normal。

.PARAMETER WithCrawler
  额外启动 run-mediacrawler-xhs-keywords-watch.ps1。

.PARAMETER CrawlerAllowAutoRestart
  与 -WithCrawler 合用：允许子进程退出后自动重启（默认不允许）。

.PARAMETER AnalyticsEveryNDigests
  传给 continuous-xhs-analytics.ps1（默认 3）。

.PARAMETER CrawlerGiveUpAfterQuickExits
  仅当允许自动重启时有效：连续短退出达此次数后停止（默认 8）。0=不限制。

.PARAMETER CrawlerNoRedirectChildLogs
  爬虫子进程输出不重定向到 logs。

.PARAMETER WhatIf
  只打印将启动的命令，不实际 Start-Process。

.EXAMPLE
  cd D:\ai封装
  .\scripts\start-xhs-pipeline-background.ps1

.EXAMPLE
  三窗且允许崩溃后自动重启：
  .\scripts\start-xhs-pipeline-background.ps1 -WithCrawler -CrawlerAllowAutoRestart
#>
[CmdletBinding()]
param(
    [ValidateSet("Minimized", "Hidden", "Normal")]
    [string] $WindowStyle = "Minimized",
    [switch] $WithCrawler,
    [switch] $CrawlerAllowAutoRestart,
    [ValidateRange(0, 100)]
    [int] $CrawlerGiveUpAfterQuickExits = 8,
    [switch] $CrawlerNoRedirectChildLogs,
    [int] $AnalyticsEveryNDigests = 3,
    [switch] $WhatIf
)

$ErrorActionPreference = "Stop"
try { chcp 65001 | Out-Null } catch {}

$repo = Split-Path $PSScriptRoot -Parent
$mergeScript = Join-Path $repo "scripts\continuous-xhs-ingest.ps1"
$anaScript = Join-Path $repo "scripts\continuous-xhs-analytics.ps1"
$crawlScript = Join-Path $repo "scripts\run-mediacrawler-xhs-keywords-watch.ps1"

foreach ($p in @($mergeScript, $anaScript)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "ERROR: missing $p" -ForegroundColor Red
        exit 1
    }
}
if ($WithCrawler -and -not (Test-Path -LiteralPath $crawlScript)) {
    Write-Host "ERROR: missing $crawlScript" -ForegroundColor Red
    exit 1
}

$ws = switch ($WindowStyle) {
    "Minimized" { [System.Diagnostics.ProcessWindowStyle]::Minimized }
    "Hidden" { [System.Diagnostics.ProcessWindowStyle]::Hidden }
    default { [System.Diagnostics.ProcessWindowStyle]::Normal }
}

function Start-BgPowershell([string] $Label, [string[]] $ArgList) {
    if ($WhatIf) {
        Write-Host "WhatIf [$Label]: powershell.exe $($ArgList -join ' ')" -ForegroundColor DarkGray
        return
    }
    try {
        Start-Process -FilePath "powershell.exe" -WorkingDirectory $repo -WindowStyle $ws -ArgumentList $ArgList | Out-Null
    }
    catch {
        Write-Host "ERROR: Start-Process failed ($Label): $_" -ForegroundColor Red
        throw
    }
}

Write-Host "Starting background pipeline (repo=$repo, WindowStyle=$WindowStyle, WithCrawler=$WithCrawler)..." -ForegroundColor Cyan
if (-not $WithCrawler) {
    Write-Host "提示: 默认不启 MediaCrawler —不会出现小红书扫码/浏览器窗口，只跑合并+数分。" -ForegroundColor DarkYellow
    Write-Host "      需要登录/爬取请加: -WithCrawler；或另开终端: cd D:\MediaCrawler ; .\venv\Scripts\python.exe main.py --platform xhs --lt qrcode --type search" -ForegroundColor DarkYellow
}

# 窗 B / C：用 -File 启动（避免 -Command 嵌套中文路径 + 无 BOM 脚本时被 PS5.1 误解析，导致某个窗口秒退）
$mergeArgs = @(
    "-NoExit",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $mergeScript,
    "-MergeOnly"
)
Start-BgPowershell "merge" $mergeArgs
Start-Sleep -Milliseconds 500

$anaArgs = @(
    "-NoExit",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $anaScript,
    "-AnalyticsEveryNDigests", "$AnalyticsEveryNDigests"
)
Start-BgPowershell "analytics" $anaArgs

if ($WithCrawler) {
    Start-Sleep -Milliseconds 500
    $crawlArgs = @(
        "-NoExit",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $crawlScript,
        "-GiveUpAfterQuickExits", "$CrawlerGiveUpAfterQuickExits"
    )
    if (-not $CrawlerAllowAutoRestart) { $crawlArgs += "-NoAutoRestart" }
    if ($CrawlerNoRedirectChildLogs) { $crawlArgs += "-NoRedirectChildLogs" }
    Start-BgPowershell "crawler" $crawlArgs
}

Write-Host "Done. Logs: logs\continuous-xhs-ingest.log , logs\continuous-xhs-analytics.log" -ForegroundColor Green
if ($WithCrawler) {
    Write-Host " Crawler: logs\mediacrawler-watch.log , logs\mediacrawler-child.stderr.log" -ForegroundColor Green
}
Write-Host "STOP files: logs\continuous-xhs-ingest.STOP , logs\continuous-xhs-analytics.STOP" -ForegroundColor DarkGray
