#Requires -Version 5.1
<#
.SYNOPSIS
  批量跑 Hermes TAVC（POST /task/sync），统计 final_pass 占比与 case_library 归档数量。

.DESCRIPTION
  需先启动 Hermes（及 OpenClaw 等依赖）。默认请求 http://127.0.0.1:8080。
  案例库计数：统计 HERMES_SESSIONS_DIR/case_library 下 *.json（宿主机路径，与 docker volume 一致）。

.PARAMETER HermesBase
  Hermes 根 URL，无尾部斜杠。

.PARAMETER Iterations
  运行次数（默认 20）。

.PARAMETER Goal
  单次 goal 字符串；与 GoalFile 二选一。

.PARAMETER GoalFile
  多行文本，每行一个 goal；按轮次循环使用。

.PARAMETER MaxAttempts
  每次任务的 TAVC max_attempts（1–8）。

.PARAMETER CaseLibraryDir
  case_library 目录；默认 <仓库根>/hermes/sessions/case_library

.PARAMETER SyncTimeoutSec
 单次 /task/sync 超时（秒）。TAVC+LLM 可能较慢，建议 >= 300。

.EXAMPLE
  .\bench-tavc-batch.ps1 -Iterations 20

.EXAMPLE
  .\bench-tavc-batch.ps1 -HermesBase "http://127.0.0.1:8080" -GoalFile ".\phase2-goal.default.txt" -Iterations 20

.NOTES
  与 env-presets 配合：先在 docker compose / 本机环境中加载预设 .env，再运行本脚本对比归档量与通过率。
#>
[CmdletBinding()]
param(
    [string] $HermesBase = "http://127.0.0.1:8080",
    [ValidateRange(1, 500)]
    [int] $Iterations = 20,
    [string] $Goal = "",
    [string] $GoalFile = "",
    [ValidateRange(1, 8)]
    [int] $MaxAttempts = 3,
    [string] $CaseLibraryDir = "",
    [int] $SyncTimeoutSec = 600,
    [string] $OutJson = ""
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
. (Join-Path $root "utf8-http.ps1")

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        chcp 65001 | Out-Null
    }
}
catch { }

if (-not $CaseLibraryDir) {
    $CaseLibraryDir = Join-Path $root "hermes\sessions\case_library"
}

$goals = @()
if ($GoalFile) {
    $gf = $GoalFile
    if (-not [System.IO.Path]::IsPathRooted($gf)) {
        $gf = Join-Path $root $gf
    }
    if (-not (Test-Path -LiteralPath $gf)) {
        throw "GoalFile not found: $gf"
    }
    $goals = @(Get-Content -LiteralPath $gf -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($goals.Count -eq 0) {
        throw "GoalFile has no non-empty lines: $gf"
    }
}
elseif ($Goal) {
    $goals = @($Goal)
}
else {
    $defaultGoalPath = Join-Path $root "phase2-goal.default.txt"
    if (Test-Path -LiteralPath $defaultGoalPath) {
        $goals = @(Get-Content -LiteralPath $defaultGoalPath -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    if ($goals.Count -eq 0) {
        $goals = @("预测流量救命")
    }
}

function Get-CaseLibraryCount {
    param([string] $Dir)
    if (-not (Test-Path -LiteralPath $Dir)) {
        return 0
    }
    return [int](@(Get-ChildItem -LiteralPath $Dir -Filter "*.json" -File -ErrorAction SilentlyContinue)).Count
}

$base = $HermesBase.TrimEnd("/")
$uri = "$base/task/sync"
$before = Get-CaseLibraryCount -Dir $CaseLibraryDir

Write-Host "Hermes: $uri" -ForegroundColor Cyan
Write-Host "Iterations: $Iterations | max_attempts: $MaxAttempts | case_library: $CaseLibraryDir (before: $before)" -ForegroundColor Cyan
Write-Host ""

$rows = New-Object System.Collections.ArrayList
$pass = 0
$fail = 0

for ($i = 1; $i -le $Iterations; $i++) {
    $g = $goals[($i - 1) % $goals.Count]
    $short = if ($g.Length -gt 72) { $g.Substring(0, 72) + "…" } else { $g }
    Write-Host "[$i/$Iterations] goal: $short" -ForegroundColor Gray

    $payload = @{ goal = $g; max_attempts = $MaxAttempts } | ConvertTo-Json -Compress
    try {
        $txt = Invoke-HttpUtf8 -Method Post -Uri $uri -JsonBody $payload -TimeoutSec $SyncTimeoutSec
        $obj = $txt | ConvertFrom-Json
        $fp = $false
        if ($null -ne $obj.final_pass) {
            $fp = [bool]$obj.final_pass
        }
        if ($fp) { $pass++ } else { $fail++ }
        [void]$rows.Add([ordered]@{
                run            = $i
                final_pass     = $fp
                final_status   = $obj.final_status
                task_id        = $obj.task_id
                lifecycle_phase = $obj.lifecycle_phase
                error          = $null
            })
        $st = if ($fp) { "PASS" } else { "FAIL" }
        $color = if ($fp) { "Green" } else { "Yellow" }
        Write-Host "  -> $st  task_id=$($obj.task_id)" -ForegroundColor $color
    }
    catch {
        $fail++
        $err = $_.Exception.Message
        Write-Host "  -> ERROR  $err" -ForegroundColor Red
        [void]$rows.Add([ordered]@{
                run            = $i
                final_pass     = $false
                final_status   = "error"
                task_id        = $null
                lifecycle_phase = $null
                error          = $err
            })
    }
}

$after = Get-CaseLibraryCount -Dir $CaseLibraryDir
$delta = $after - $before
$pct = if ($Iterations -gt 0) { [math]::Round(100.0 * $pass / $Iterations, 2) } else { 0 }

Write-Host ""
Write-Host "========== 汇总 ==========" -ForegroundColor Cyan
Write-Host "final_pass: $pass / $Iterations  ($pct%)" -ForegroundColor $(if ($pct -ge 50) { "Green" } else { "Yellow" })
Write-Host "final_fail (含 HTTP/异常): $fail"
Write-Host "case_library json: before=$before  after=$after  delta=+$delta"
Write-Host "==========================" -ForegroundColor Cyan

if ($OutJson) {
    $outPath = $OutJson
    if (-not [System.IO.Path]::IsPathRooted($outPath)) {
        $outPath = Join-Path $root $outPath
    }
    $report = [ordered]@{
        hermes_base = $base
        iterations       = $Iterations
        max_attempts     = $MaxAttempts
        final_pass       = $pass
        final_pass_pct   = $pct
        case_library_dir = $CaseLibraryDir
        case_library_before = $before
        case_library_after  = $after
        case_library_delta  = $delta
        runs             = @($rows)
    }
    $jsonText = ($report | ConvertTo-Json -Depth 8 -Compress)
    Write-JsonUtf8File -Path $outPath -JsonText $jsonText
    Write-Host "Wrote: $outPath" -ForegroundColor Green
}
