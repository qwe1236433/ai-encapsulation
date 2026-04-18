#Requires -Version 5.1
<#
.SYNOPSIS
  窗 D：Hermes 闭环调度器。周期性调用 hermes_closed_loop_tick.py，完成"数分 → LLM 提案 → 审核 → 写回关键词"闭环。

.DESCRIPTION
  设计原则（与 continuous-xhs-analytics.ps1 对齐）：
    - 每 IntervalMinutes 分钟跑一次 tick，不重入（上一次没回就等）
    - 前置条件没就绪时退避，不当错误处理
    - 写一个 STOP 文件（logs\hermes-closed-loop.STOP）就优雅退出
    - 只把 python 的 stdout 记到 logs\hermes-closed-loop-runner.log，tick 自己还会写
      logs\hermes-closed-loop.log（结构化 JSONL），两份互不干扰

  建议起前：
    - 窗 A run-mediacrawler-xhs-keywords-watch.ps1 已经在跑（有 base profile & 扫码态）
    - 窗 B、窗 C 已经在跑（在产 samples.json → features → baseline_v2.json）
    - .env 里 MINIMAX_API_KEY / MINIMAX_GROUP_ID 已配，Hermes 能成功调 LLM

.PARAMETER IntervalMinutes
  两次 tick 之间的间隔；默认 30（与用户决策一致）。

.PARAMETER MaxTicks
  0 = 无限；N>0 跑 N 次后退出（调试/演示用）。

.PARAMETER Reason
  透传给 tick 的 --reason，审计日志留痕。

.PARAMETER ForceKind
  threshold|keyword_pool|prompt；透传给 tick，强制 tuner 只提某类提案。

.PARAMETER MaxRounds
  透传给 tick；tuner→auditor 每 tick 的最大重试轮数。

.PARAMETER DryRun
  即使 LLM 通过 keyword_pool 也不覆盖 CLI 文件（观察模式）。透传 tick --skip-bridge。

.PARAMETER StopFilePath
  STOP 文件路径；默认 logs\hermes-closed-loop.STOP。

.EXAMPLE
  cd D:\ai封装
  .\scripts\hermes-closed-loop.ps1

.EXAMPLE
  调试：只跑 3 轮，只提 keyword_pool，且不真写 CLI 文件：
  .\scripts\hermes-closed-loop.ps1 -MaxTicks 3 -ForceKind keyword_pool -DryRun
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 1440)]
    [int] $IntervalMinutes = 30,
    [ValidateRange(0, 10000)]
    [int] $MaxTicks = 0,
    [string] $Reason = "closed_loop_tick",
    [ValidateSet("threshold", "keyword_pool", "prompt")]
    [string] $ForceKind = "",
    [ValidateRange(1, 5)]
    [int] $MaxRounds = 3,
    [switch] $DryRun,
    [string] $StopFilePath = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

try { chcp 65001 | Out-Null } catch {}

$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo

$tickPy = Join-Path $repo "scripts\hermes_closed_loop_tick.py"
if (-not (Test-Path -LiteralPath $tickPy)) {
    Write-Host "ERROR: missing $tickPy" -ForegroundColor Red
    exit 1
}

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$runnerLog = Join-Path $logDir "hermes-closed-loop-runner.log"

if (-not $StopFilePath) {
    $StopFilePath = Join-Path $logDir "hermes-closed-loop.STOP"
}

function Write-RunnerLog([string] $msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $runnerLog -Value $line -Encoding UTF8
    Write-Host $line
}

Write-RunnerLog "Hermes closed-loop runner started"
Write-RunnerLog "  repo       = $repo"
Write-RunnerLog "  interval   = ${IntervalMinutes}m"
Write-RunnerLog "  max_ticks  = $MaxTicks (0 = infinite)"
Write-RunnerLog "  reason     = $Reason"
Write-RunnerLog "  force_kind = $(if ($ForceKind) { $ForceKind } else { '(free)' })"
Write-RunnerLog "  dry_run    = $($DryRun.IsPresent)"
Write-RunnerLog "  stop_file  = $StopFilePath"
Write-RunnerLog "  tick_log   = logs\hermes-closed-loop.log (JSONL, by tick script)"

$tickCount = 0

while ($true) {
    if (Test-Path -LiteralPath $StopFilePath) {
        Write-RunnerLog "STOP file present; exiting."
        break
    }

    $tickCount++
    Write-RunnerLog "tick #$tickCount start"

    $argv = @($tickPy, "--reason", $Reason, "--max-rounds", "$MaxRounds", "--quiet")
    if ($ForceKind) { $argv += @("--force-kind", $ForceKind) }
    if ($DryRun) { $argv += "--skip-bridge" }

    $stdoutFile = Join-Path $logDir "hermes-closed-loop-runner.last.stdout"
    $stderrFile = Join-Path $logDir "hermes-closed-loop-runner.last.stderr"

    try {
        $proc = Start-Process -FilePath "python" -ArgumentList $argv -WorkingDirectory $repo -PassThru -NoNewWindow -Wait `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        # 防御：PS 5.1 在 -NoNewWindow -Wait -PassThru -Redirect* 组合下偶尔 $proc 为 $null
        if ($null -ne $proc) { $code = $proc.ExitCode } else { $code = 0 }
    }
    catch {
        Write-RunnerLog "ERROR: launching python failed: $_"
        $code = 255
    }

    $summary = ""
    if (Test-Path -LiteralPath $stdoutFile) {
        # 防御：空 stdout 文件时 Get-Content -Raw 返回 $null，不能直接 .Trim()
        $raw = Get-Content -LiteralPath $stdoutFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($raw) { $summary = $raw.Trim() }
    }
    if ($summary) {
        # 摘要通常是 tick 脚本 --quiet 下的一行 JSON
        Write-RunnerLog "tick #$tickCount exit=$code summary=$summary"
    }
    else {
        Write-RunnerLog "tick #$tickCount exit=$code (no stdout summary)"
    }

    if ($code -ne 0 -and (Test-Path -LiteralPath $stderrFile)) {
        $errTail = (Get-Content -LiteralPath $stderrFile -Tail 20 -Encoding UTF8 -ErrorAction SilentlyContinue) -join "`n"
        if ($errTail) {
            Write-RunnerLog "tick #$tickCount stderr tail:`n$errTail"
        }
    }

    if ($MaxTicks -gt 0 -and $tickCount -ge $MaxTicks) {
        Write-RunnerLog "reached MaxTicks=$MaxTicks; exiting."
        break
    }

    Write-RunnerLog "sleep ${IntervalMinutes}m (next tick #$($tickCount + 1))"
    # 把长休眠切成小片，便于 STOP 文件快速生效
    $remain = $IntervalMinutes * 60
    while ($remain -gt 0) {
        if (Test-Path -LiteralPath $StopFilePath) {
            Write-RunnerLog "STOP file present during sleep; exiting."
            exit 0
        }
        $slice = [Math]::Min(15, $remain)
        Start-Sleep -Seconds $slice
        $remain -= $slice
    }
}

Write-RunnerLog "Hermes closed-loop runner exited normally"
