#Requires -Version 5.1
<#
.SYNOPSIS
  后台启动：合并 jsonl（MergeOnly）+ 持续数分 +（可选）MediaCrawler 监视爬虫。

.DESCRIPTION
  使用 Start-Process 各起一个独立 PowerShell 进程，默认窗口最小化到任务栏（非无窗口，便于出错时点开看）。
  日志：合并 logs\continuous-xhs-ingest.log；数分 logs\continuous-xhs-analytics.log。
  停止：任务栏里对应窗口 Ctrl+C，或写入 logs\continuous-xhs-ingest.STOP / continuous-xhs-analytics.STOP。

  数分出现「SKIP: digest unchanged」属正常：digest 未变时每轮只休眠，等合并写出新 digest 后才会继续判断是否要跑完整数分。

.PARAMETER WindowStyle
  Minimized（默认）| Hidden（完全隐藏，仅看日志）| Normal。

.PARAMETER SkipCrawler
  不启动 MediaCrawler+watch 进程。

.PARAMETER AnalyticsEveryNDigests
  传给 continuous-xhs-analytics.ps1（默认 3）。

.PARAMETER WhatIf
  只打印将启动的命令，不实际 Start-Process。

.EXAMPLE
  cd D:\ai封装
  .\scripts\start-xhs-pipeline-background.ps1

.EXAMPLE
  不启爬虫、隐藏窗口：
  .\scripts\start-xhs-pipeline-background.ps1 -SkipCrawler -WindowStyle Hidden
#>
[CmdletBinding()]
param(
    [ValidateSet("Minimized", "Hidden", "Normal")]
    [string] $WindowStyle = "Minimized",
    [switch] $SkipCrawler,
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
if (-not $SkipCrawler -and -not (Test-Path -LiteralPath $crawlScript)) {
    Write-Host "ERROR: missing $crawlScript (use -SkipCrawler)" -ForegroundColor Red
    exit 1
}

$ws = switch ($WindowStyle) {
    "Minimized" { [System.Diagnostics.ProcessWindowStyle]::Minimized }
    "Hidden" { [System.Diagnostics.ProcessWindowStyle]::Hidden }
    default { [System.Diagnostics.ProcessWindowStyle]::Normal }
}

function Start-BgPowershell([string] $Title, [string[]] $ArgList) {
    if ($WhatIf) {
        Write-Host "WhatIf: Start-Process powershell -WindowStyle $WindowStyle -Args $($ArgList -join ' ')" -ForegroundColor DarkGray
        return
    }
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $repo -WindowStyle $ws -ArgumentList $ArgList | Out-Null
}

Write-Host "Starting background pipeline (repo=$repo, WindowStyle=$WindowStyle)..." -ForegroundColor Cyan

# 窗 B：仅合并
$mergeArgs = @(
    "-NoExit",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "& { `$Host.UI.RawUI.WindowTitle = 'XHS-BG MergeOnly'; Set-Location -LiteralPath '$repo'; & '$mergeScript' -MergeOnly }"
)
Start-BgPowershell "merge" $mergeArgs
Start-Sleep -Milliseconds 400

# 窗 C：数分
$anaArgs = @(
    "-NoExit",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "& { `$Host.UI.RawUI.WindowTitle = 'XHS-BG Analytics'; Set-Location -LiteralPath '$repo'; & '$anaScript' -AnalyticsEveryNDigests $AnalyticsEveryNDigests }"
)
Start-BgPowershell "analytics" $anaArgs

if (-not $SkipCrawler) {
    Start-Sleep -Milliseconds 400
    $crawlArgs = @(
        "-NoExit",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        "& { `$Host.UI.RawUI.WindowTitle = 'XHS-BG CrawlerWatch'; Set-Location -LiteralPath '$repo'; & '$crawlScript' }"
    )
    Start-BgPowershell "crawler" $crawlArgs
}

Write-Host "Done. Logs: logs\continuous-xhs-ingest.log , logs\continuous-xhs-analytics.log" -ForegroundColor Green
Write-Host "STOP files: logs\continuous-xhs-ingest.STOP , logs\continuous-xhs-analytics.STOP" -ForegroundColor DarkGray
