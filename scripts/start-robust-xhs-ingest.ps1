#Requires -Version 5.1
<#
.SYNOPSIS
  打印「稳健三窗」采集 + 数分说明；可选只启动合并或数分循环。

.DESCRIPTION
  MediaCrawler 侧已预设：10 分钟/轮、页间休眠 4s、单并发、可选 XHS_SESSION_MAX_ROUNDS。
  见 D:\MediaCrawler\config\base_config.py。
  窗 C：digest 每「代数」+1；默认每 3 次新 digest 跑完整数分并写入 research/analytics_history/快照（continuous-xhs-analytics.ps1；-AnalyticsEveryNDigests 1 可改为每次 digest 都跑）。

.PARAMETER StartMerge
  在本机启动 continuous-xhs-ingest.ps1 -MergeOnly（阻塞运行）。

.PARAMETER StartAnalytics
  在本机启动 continuous-xhs-analytics.ps1（阻塞运行）。

.EXAMPLE
  .\scripts\start-robust-xhs-ingest.ps1

.EXAMPLE
  .\scripts\start-robust-xhs-ingest.ps1 -StartMerge

.EXAMPLE
  .\scripts\start-robust-xhs-ingest.ps1 -StartAnalytics
#>
[CmdletBinding()]
param(
    [switch] $StartMerge,
    [switch] $StartAnalytics
)

try { chcp 65001 | Out-Null } catch {}

$repo = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $repo "openclaw"))) {
    Write-Host "WARN: openclaw not under $repo ; check repo path." -ForegroundColor DarkYellow
}
$mc = if ($env:MEDIACRAWLER_ROOT) { $env:MEDIACRAWLER_ROOT } else { "D:\MediaCrawler" }

Write-Host ""
Write-Host "======== 稳健流水线（三窗）========" -ForegroundColor Cyan
Write-Host "窗 A — MediaCrawler 单进程多轮（同一浏览器，约 10 分钟一轮）："
Write-Host "  Set-Location -LiteralPath '$repo'"
Write-Host "  .\scripts\run-mediacrawler-xhs-keywords-watch.ps1（词表或 MC config 变自动重启；无监视则用 run-mediacrawler-xhs-with-suggested-keywords.ps1）"
Write-Host "  （或手动 cd '$mc' 后 python main.py ...；有 keyword_candidates_for_cli 时会带 --keywords）"
Write-Host ""
Write-Host "窗 B — 只合并 jsonl → samples.json（默认约每 15 分钟）："
Write-Host "  Set-Location -LiteralPath '$repo'"
Write-Host "  .\scripts\continuous-xhs-ingest.ps1 -MergeOnly"
Write-Host ""
Write-Host "窗 C — 轮询 digest；默认每 3 次新 digest 才跑完整数分（约每 18 分钟检查）："
Write-Host "  Set-Location -LiteralPath '$repo'"
Write-Host "  .\scripts\continuous-xhs-analytics.ps1"
Write-Host ""
Write-Host "候选搜索词（窗 C 跑完一轮后）：research\keyword_candidates_for_cli.txt（默认 top-keywords=0 全量；历史见 research\analytics_history\）"
Write-Host " 在 MediaCrawler 目录：读取该文件整行作为 main.py 的 --keywords 参数；词表更新后重启窗 A。"
Write-Host "  种子词：复制 research\keyword_seed.example.txt 为 keyword_seed.txt"
Write-Host ""
Write-Host "停止合并：logs\continuous-xhs-ingest.STOP；停止数分：logs\continuous-xhs-analytics.STOP"
Write-Host "调参：$mc\config\base_config.py （XHS_SESSION_LOOP_INTERVAL_SEC / XHS_SESSION_MAX_ROUNDS / CRAWLER_MAX_SLEEP_SEC）"
Write-Host "一键后台（默认仅合并+数分，稳定）：Set-Location '$repo'; .\scripts\start-xhs-pipeline-background.ps1"
Write-Host "一键三窗（含爬虫）：.\scripts\start-xhs-full-pipeline.ps1 或 .\scripts\start-xhs-pipeline-background.ps1 -WithCrawler"
Write-Host "=================================" -ForegroundColor Cyan
Write-Host ""

if ($StartMerge -and $StartAnalytics) {
    Write-Host "ERROR: use -StartMerge or -StartAnalytics, not both in one process." -ForegroundColor Red
    exit 1
}

if ($StartMerge) {
    $ingest = Join-Path $repo "scripts\continuous-xhs-ingest.ps1"
    if (-not (Test-Path -LiteralPath $ingest)) {
        Write-Host "ERROR: $ingest not found" -ForegroundColor Red
        exit 1
    }
    Set-Location -LiteralPath $repo
    & $ingest -MergeOnly
}

if ($StartAnalytics) {
    $ana = Join-Path $repo "scripts\continuous-xhs-analytics.ps1"
    if (-not (Test-Path -LiteralPath $ana)) {
        Write-Host "ERROR: $ana not found" -ForegroundColor Red
        exit 1
    }
    Set-Location -LiteralPath $repo
    & $ana
}
