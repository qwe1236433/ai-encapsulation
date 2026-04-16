#Requires -Version 5.1
<#
.SYNOPSIS
  小红书研究一键：merge → 关键词(可选轮换) → 导出特征 → 校验 → 指标 → 训练 → 评估 → 归档快照 → digest 对比报告。

.DESCRIPTION
  将当前数分能力串成单次可执行流水线（不等同于 continuous-xhs-analytics 的长驻循环）。
  - 关键词：默认使用 research/keyword_rotation_pool.txt（若存在），否则 keyword_rotation_pool.example.txt；可用 -NoKeywordRotation 关闭。
  - 归档：成功后调用 archive_analytics_snapshot.py，写入 research/analytics_history/run_*，供 compare_digest_analytics_runs对比。
  - 报告：生成 research/runtime/digest_comparison_report.md（含 --include-current 与近 N 次快照）。

.PARAMETER McJsonl
  MediaCrawler jsonl 目录，默认 D:\MediaCrawler\data\xhs\jsonl 或环境变量 FLOW_API_MEDIACRAWLER_JSONL。

.PARAMETER SkipMerge
  跳过 merge（沿用现有 samples.json / digest）。

.PARAMETER SkipTrain
  跳过训练与评估（仍可做导出/校验/指标/对比）。

.EXAMPLE
  cd D:\ai封装
  .\scripts\xhs-research-oneclick.ps1
  不合并、只重导+训练：
  .\scripts\xhs-research-oneclick.ps1 -SkipMerge
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = "",
    [string] $McJsonl = "",
    [switch] $SkipMerge,
    [string] $BatchId = "",
    [string] $LabelsSpec = "",
    [string] $FeaturesOut = "",
    [string] $DigestOut = "",
    [string] $SamplesPath = "",
    [int] $TopKeywords = 18,
    [string] $RotationPool = "",
    [switch] $NoKeywordRotation,
    [int] $CvFolds = 5,
    [int] $EvalBootstrap = 150,
    [double] $EvalTimeHoldoutFraction = 0.0,
    [int] $CompareLast = 3,
    [switch] $SkipTrain,
    [switch] $SkipSuggestKeywords,
    [switch] $AllowMixedBatch,
    [switch] $NoArchive,
    [switch] $SkipCompareReport
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path $PSScriptRoot -Parent
}
Set-Location -LiteralPath $RepoRoot

if (-not $SamplesPath) {
    $SamplesPath = Join-Path $RepoRoot "openclaw\data\xhs-feed\samples.json"
}
if (-not $FeaturesOut) {
    $FeaturesOut = Join-Path $RepoRoot "research\features_v0.csv"
}
if (-not $DigestOut) {
    $DigestOut = Join-Path $RepoRoot "openclaw\data\xhs-feed\samples.digest.json"
}

if (-not $LabelsSpec) {
    $ls = Join-Path $RepoRoot "research\labels_spec.json"
    $ex = Join-Path $RepoRoot "research\labels_spec.example.json"
    if (Test-Path -LiteralPath $ls) { $LabelsSpec = $ls }
    elseif (Test-Path -LiteralPath $ex) { $LabelsSpec = $ex }
    else {
        Write-Host "ERROR: need research\labels_spec.json or labels_spec.example.json" -ForegroundColor Red
        exit 2
    }
}

$mcIn = $McJsonl.Trim()
if (-not $mcIn) {
    $mcIn = [Environment]::GetEnvironmentVariable("FLOW_API_MEDIACRAWLER_JSONL", "Process")
}
if (-not $mcIn) { $mcIn = "D:\MediaCrawler\data\xhs\jsonl" }

$bid = $BatchId.Trim()
if (-not $bid -and -not $SkipMerge) {
    $bid = "EXP-1CLICK-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss")
}

$mergeScript = Join-Path $RepoRoot "scripts\merge-xhs-feed.ps1"
$exportPy = Join-Path $RepoRoot "scripts\export_features_v0.py"
$verifyPy = Join-Path $RepoRoot "scripts\verify_features_labels_spec.py"
$metricsPy = Join-Path $RepoRoot "scripts\compute_feed_metrics_v0.py"
$suggestPy = Join-Path $RepoRoot "scripts\suggest_keywords_from_feed.py"
$trainPy = Join-Path $RepoRoot "research\train_baseline_v0.py"
$evalPy = Join-Path $RepoRoot "research\evaluate_baseline_weights.py"
$archivePy = Join-Path $RepoRoot "scripts\archive_analytics_snapshot.py"
$comparePy = Join-Path $RepoRoot "scripts\compare_digest_analytics_runs.py"

foreach ($p in @($mergeScript, $exportPy, $verifyPy, $metricsPy, $suggestPy, $trainPy, $evalPy, $archivePy, $comparePy)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "ERROR: missing $p" -ForegroundColor Red
        exit 1
    }
}

function Step([string] $Name) {
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
}

if (-not $SkipMerge) {
    Step "merge-xhs-feed (jsonl -> samples + digest)"
    if (-not (Test-Path -LiteralPath $mcIn)) {
        Write-Host "ERROR: McJsonl not found: $mcIn" -ForegroundColor Red
        exit 2
    }
    & $mergeScript -In $mcIn -Out $SamplesPath -Dedupe key -DigestOut $DigestOut -BatchId $bid
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not (Test-Path -LiteralPath $SamplesPath)) {
    Write-Host "ERROR: samples missing: $SamplesPath" -ForegroundColor Red
    exit 2
}

if (-not $SkipSuggestKeywords) {
    Step "suggest_keywords_from_feed (+ optional rotation)"
    $poolArg = @()
    if (-not $NoKeywordRotation) {
        $poolFile = $RotationPool.Trim()
        if (-not $poolFile) {
            $pt = Join-Path $RepoRoot "research\keyword_rotation_pool.txt"
            $pex = Join-Path $RepoRoot "research\keyword_rotation_pool.example.txt"
            if (Test-Path -LiteralPath $pt) { $poolFile = $pt }
            elseif (Test-Path -LiteralPath $pex) { $poolFile = $pex }
        }
        if ($poolFile -and (Test-Path -LiteralPath $poolFile)) {
            $poolArg = @("--rotation-pool", $poolFile)
            Write-Host "Using rotation pool: $poolFile"
        }
    }
    $seedPath = Join-Path $RepoRoot "research\keyword_seed.txt"
    $seedKw = ""
    if (Test-Path -LiteralPath $seedPath) {
        $seedKw = (
            (Get-Content -LiteralPath $seedPath) |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith("#") }
        ) -join ","
    }
    $skArgs = @($suggestPy, "--samples", $SamplesPath, "--top-keywords", "$TopKeywords") + $poolArg
    if ($seedKw) { $skArgs += @("--seed-keywords", $seedKw) }
    & python @skArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: suggest_keywords exit=$LASTEXITCODE" -ForegroundColor DarkYellow
    }
}

Step "export_features_v0"
& python $exportPy --samples $SamplesPath --out $FeaturesOut --labels-spec $LabelsSpec --feed-digest $DigestOut --verify-samples-digest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "verify_features_labels_spec"
& python $verifyPy --features $FeaturesOut --labels-spec $LabelsSpec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "compute_feed_metrics_v0"
& python $metricsPy --features $FeaturesOut
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: feed_metrics exit=$LASTEXITCODE" -ForegroundColor DarkYellow
}

$artDir = Join-Path $RepoRoot "research\artifacts"
New-Item -ItemType Directory -Force -Path $artDir | Out-Null
$outV0 = Join-Path $artDir "auto_baseline_v0.json"
$outV1 = Join-Path $artDir "auto_baseline_v1.json"

if (-not $SkipTrain) {
    $common = @($trainPy, "--features", $FeaturesOut, "--labels-spec", $LabelsSpec, "--cv-folds", "$CvFolds")
    if ($AllowMixedBatch) { $common += "--allow-mixed-batch" }

    Step "train_baseline_v0 (v0)"
    & python @($common + @("--out", $outV0, "--feature-schema", "v0"))
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Step "train_baseline_v0 (v1)"
    & python @($common + @("--out", $outV1, "--feature-schema", "v1"))
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $eb = [Math]::Max(0, $EvalBootstrap)
    $th = [double]$EvalTimeHoldoutFraction
    Step "evaluate_baseline_weights"
    $ev0 = @($evalPy, "--artifact", $outV0, "--bootstrap", "$eb")
    $ev1 = @($evalPy, "--artifact", $outV1, "--bootstrap", "$eb")
    if ($AllowMixedBatch) {
        $ev0 += "--allow-mixed-batch"
        $ev1 += "--allow-mixed-batch"
    }
    if ($th -gt 0.0 -and $th -lt 1.0) {
        $ev0 += @("--time-holdout-fraction", "$th")
        $ev1 += @("--time-holdout-fraction", "$th")
    }
    & python @ev0
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: eval v0 exit=$LASTEXITCODE" -ForegroundColor DarkYellow
    }
    & python @ev1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: eval v1 exit=$LASTEXITCODE" -ForegroundColor DarkYellow
    }
}

if (-not $NoArchive) {
    if (Test-Path -LiteralPath $DigestOut) {
        Step "archive_analytics_snapshot"
        & python $archivePy --digest $DigestOut
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARN: archive exit=$LASTEXITCODE" -ForegroundColor DarkYellow
        }
    }
    else {
        Write-Host "SKIP archive: no digest at $DigestOut" -ForegroundColor DarkYellow
    }
}

if (-not $SkipCompareReport) {
    Step "compare_digest_analytics_runs"
    & python $comparePy --last $CompareLast --include-current
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: compare exit=$LASTEXITCODE" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "OK: one-click done. See research/runtime/digest_comparison_report.md" -ForegroundColor Green
exit 0
