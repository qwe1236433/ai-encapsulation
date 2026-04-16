#Requires -Version 5.1
<#
.SYNOPSIS
  一键打开三窗：合并 Feed（B）→ 持续数分（C）→ MediaCrawler 小红书（A）。

.DESCRIPTION
  每个窗独立 powershell.exe -NoExit，标题区分。窗 A 使用 run-mediacrawler-xhs-keywords-watch.ps1：keyword_candidates_for_cli.txt 或 MediaCrawler config/base_config.py、xhs_config.py 变更后自动 taskkill 并重启爬虫。
  停止：各窗 Ctrl+C，或写入 logs\continuous-xhs-ingest.STOP / continuous-xhs-analytics.STOP。

.PARAMETER SkipCrawler
  不启动窗 A（只跑合并 + 数分）。

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
    [switch] $WhatIf
)

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
    Write-Host "ERROR: missing $crawlScript" -ForegroundColor Red
    exit 1
}

$mc = if ($env:MEDIACRAWLER_ROOT) { $env:MEDIACRAWLER_ROOT } else { "D:\MediaCrawler" }
$pyMc = Join-Path $mc "venv\Scripts\python.exe"
if (-not $SkipCrawler -and -not (Test-Path -LiteralPath $pyMc)) {
    Write-Host "WARN: MediaCrawler Python not found: $pyMc (set MEDIACRAWLER_ROOT or install venv)" -ForegroundColor DarkYellow
}

function Invoke-PipelineWindow([string] $Title, [string] $Inner) {
    $cmd = "& { `$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location -LiteralPath '$repo'; $Inner }"
    if ($WhatIf) {
        Write-Host "WhatIf: powershell -NoExit -Command $cmd" -ForegroundColor DarkGray
        return
    }
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $repo -ArgumentList @(
        "-NoExit",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        $cmd
    )
}

Write-Host ""
Write-Host "Starting XHS pipeline (3 windows)..." -ForegroundColor Cyan
Write-Host "  Repo: $repo"
Write-Host ""

Invoke-PipelineWindow "XHS-B Merge jsonl->Feed" "& '$mergeScript' -MergeOnly"

Start-Sleep -Milliseconds 600

Invoke-PipelineWindow "XHS-C Analytics digest" "& '$anaScript'"

if (-not $SkipCrawler) {
    Start-Sleep -Milliseconds 600
    Invoke-PipelineWindow "XHS-A MediaCrawler+watch" "& '$crawlScript'"
}

Write-Host "Done. Close windows or use STOP files under logs\ to stop loops." -ForegroundColor Green
Write-Host ""
