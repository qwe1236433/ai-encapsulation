#Requires -Version 5.1
<#
.SYNOPSIS
  持续数分：监测 Feed digest 代数 → 每 N 次新 digest 跑一轮导出/校验/候选词/训练/评估，并落盘快照。

.DESCRIPTION
  与 continuous-xhs-ingest.ps1 -MergeOnly 配合：每次合并可能刷新 digest.sha256。
  - digest「代数」在每次 **digest 与上次不同** 时 +1；同一 digest 多次轮询不重复计数。
  - 仅当 digest_generation % AnalyticsEveryNDigests == 0 时跑完整数分（默认 N=3）。
  - 候选词默认 --top-keywords 0（不截断，全部得分词写入 CLI 行）；可用 -TopKeywords 限制。
  - 每轮成功尝试后复制 keyword_candidates*、features、artifact、eval、digest 等到 research/analytics_history/run_*。
  - v1 训练成功时写入 research/runtime/factory_baseline.env，api/main.py 启动时会加载以更新 XHS_FACTORY_BASELINE_JSON（需重启已运行的 API 进程）。
  - 若存在 research/runtime/mediacrawler_base_config.json，则调用 apply_mediacrawler_base_config.py 白名单修改 MediaCrawler config（写前备份）；窗 A 监视脚本会检测 base_config/xhs_config 变化并重启爬虫。
  - 可选 research/runtime/mediacrawler_eval_patch_rules.json：按 eval_auto_baseline_v1.json 的 warnings/n_samples/AUC 等合并补丁进 mediacrawler_base_config.json（再 apply）；规则表见 research/mediacrawler_eval_patch_rules.example.json。

.PARAMETER SkipSyncFactoryBaseline
  不写 factory_baseline.env（不自动指向最新 auto_baseline_v1.json）。

.PARAMETER SkipApplyMediaCrawlerConfig
  不运行 apply_mediacrawler_base_config.py（即使存在 research/runtime/mediacrawler_base_config.json）。

.PARAMETER SkipEvalPatchRules
  不运行 merge_mediacrawler_patch_from_eval.py（不按 eval+规则表合并 MC patch）。

.PARAMETER MediaCrawlerRoot
  MediaCrawler 根目录；默认环境变量 MEDIACRAWLER_ROOT 或 D:\MediaCrawler。

.PARAMETER AnalyticsEveryNDigests
  每多少次「新 digest」触发一轮完整数分；默认 3；1 表示每次 digest 变化都跑。

.PARAMETER TopKeywords
  传给 suggest_keywords_from_feed.py --top-keywords；0 表示不截断（默认0）。

.PARAMETER EvalTimeHoldoutFraction
  传给 evaluate_baseline_weights.py --time-holdout-fraction；0 表示不做时间序 hold-out（默认 0）；
  例如 0.2 表示在可解析 published_at 的行上按时间切分，最后 20% 为测试集。

.PARAMETER SkipFeedMetrics
  不运行 compute_feed_metrics_v0.py（默认在 verify 后写入 research/runtime/feed_quality_metrics.json）。

.PARAMETER AnalyticsHistoryDir
  快照根目录，默认 research/analytics_history。

.EXAMPLE
  cd D:\ai封装
  .\scripts\continuous-xhs-analytics.ps1

.EXAMPLE
  每次 digest 变化都数分（旧行为），且最多 30 个词：
  .\scripts\continuous-xhs-analytics.ps1 -AnalyticsEveryNDigests 1 -TopKeywords 30
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 1440)]
    [int] $IntervalMinutes = 18,
    [ValidateRange(1, 10000)]
    [int] $AnalyticsEveryNDigests = 3,
    [ValidateRange(0, 50000)]
    [int] $TopKeywords = 0,
    [string] $RepoRoot = "",
    [string] $SamplesPath = "",
    [string] $DigestPath = "",
    [string] $FeaturesOut = "",
    [string] $LabelsSpec = "",
    [string] $AnalyticsHistoryDir = "",
    [int] $CvFolds = 5,
    [switch] $SkipTrain,
    [switch] $Force,
    [switch] $AllowMixedBatch,
    [switch] $SkipSuggestKeywords,
    [switch] $SkipWeightEvaluation,
    [switch] $SkipAppendExperimentReport,
    [switch] $SkipSyncFactoryBaseline,
    [switch] $SkipApplyMediaCrawlerConfig,
    [switch] $SkipEvalPatchRules,
    [string] $MediaCrawlerRoot = "",
    [int] $EvalBootstrap = 150,
    [ValidateRange(0.0, 1.0)]
    [double] $EvalTimeHoldoutFraction = 0.0,
    [switch] $SkipFeedMetrics,
    [string] $StopFilePath = ""
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path $PSScriptRoot -Parent
}
Set-Location -LiteralPath $RepoRoot

if (-not $SamplesPath) {
    $SamplesPath = Join-Path $RepoRoot "openclaw\data\xhs-feed\samples.json"
}
if (-not $DigestPath) {
    $DigestPath = Join-Path $RepoRoot "openclaw\data\xhs-feed\samples.digest.json"
}
if (-not $FeaturesOut) {
    $FeaturesOut = Join-Path $RepoRoot "research\features_v0.csv"
}
if (-not $AnalyticsHistoryDir) {
    $AnalyticsHistoryDir = Join-Path $RepoRoot "research\analytics_history"
}
if (-not $LabelsSpec) {
    $ls = Join-Path $RepoRoot "research\labels_spec.json"
    $ex = Join-Path $RepoRoot "research\labels_spec.example.json"
    if (Test-Path -LiteralPath $ls) { $LabelsSpec = $ls }
    elseif (Test-Path -LiteralPath $ex) { $LabelsSpec = $ex }
    else { $LabelsSpec = "" }
}

if (-not $StopFilePath) {
    $StopFilePath = Join-Path $RepoRoot "logs\continuous-xhs-analytics.STOP"
}

$logDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $AnalyticsHistoryDir | Out-Null
$logFile = Join-Path $logDir "continuous-xhs-analytics.log"
$stateFile = Join-Path $logDir "last-analytics-digest-sha.txt"
$genStatePath = Join-Path $logDir "analytics-digest-generation.json"

function Write-AnalyticLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Read-DigestSha256([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        $j = $raw | ConvertFrom-Json
        if ($j.sha256) { return [string]$j.sha256 }
    }
    catch { }
    return $null
}

function Read-GenState([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{
            digest_generation      = 0
            last_counted_sha       = ""
            last_run_generation    = -1
        }
    }
    try {
        $j = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        return @{
            digest_generation      = [int]$j.digest_generation
            last_counted_sha       = [string]$j.last_counted_sha
            last_run_generation    = [int]$j.last_run_generation
        }
    }
    catch {
        return @{
            digest_generation      = 0
            last_counted_sha       = ""
            last_run_generation    = -1
        }
    }
}

function Write-GenState([hashtable] $State, [string] $Path) {
    $obj = [ordered]@{
        digest_generation   = $State.digest_generation
        last_counted_sha    = $State.last_counted_sha
        last_run_generation = $State.last_run_generation
    }
    ($obj | ConvertTo-Json -Compress) | Set-Content -LiteralPath $Path -Encoding UTF8 -NoNewline
}

function Save-AnalyticsSnapshot {
    param(
        [string] $SnapRoot,
        [string] $DigestSha,
        [string] $DigestFile,
        [int] $DigestGen,
        [int] $EveryN,
        [int] $TopKw,
        [int] $FeatureRows,
        [bool] $ExportOk,
        [bool] $VerifyOk,
        [bool] $SuggestOk,
        [bool] $TrainV0Ok,
        [bool] $TrainV1Ok,
        [bool] $EvalV0Ok,
        [bool] $EvalV1Ok
    )
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $short = if ($DigestSha.Length -ge 12) { $DigestSha.Substring(0, 12) } else { $DigestSha }
    $dirName = "run_{0}_g{1}_{2}" -f $stamp, $DigestGen, $short
    $dir = Join-Path $SnapRoot $dirName
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $copyPairs = @(
        @{ Src = (Join-Path $RepoRoot "research\keyword_candidates.json"); Dst = "keyword_candidates.json" }
        @{ Src = (Join-Path $RepoRoot "research\keyword_candidates.txt"); Dst = "keyword_candidates.txt" }
        @{ Src = (Join-Path $RepoRoot "research\keyword_candidates_for_cli.txt"); Dst = "keyword_candidates_for_cli.txt" }
        @{ Src = (Join-Path $RepoRoot "research\features_v0.csv"); Dst = "features_v0.csv" }
        @{ Src = (Join-Path $RepoRoot "research\artifacts\auto_baseline_v0.json"); Dst = "auto_baseline_v0.json" }
        @{ Src = (Join-Path $RepoRoot "research\artifacts\auto_baseline_v1.json"); Dst = "auto_baseline_v1.json" }
        @{ Src = (Join-Path $RepoRoot "research\artifacts\eval_auto_baseline_v0.json"); Dst = "eval_auto_baseline_v0.json" }
        @{ Src = (Join-Path $RepoRoot "research\artifacts\eval_auto_baseline_v1.json"); Dst = "eval_auto_baseline_v1.json" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\factory_baseline.env"); Dst = "factory_baseline.env" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\mediacrawler_base_config.json"); Dst = "mediacrawler_base_config.json" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\mediacrawler_eval_patch_rules.json"); Dst = "mediacrawler_eval_patch_rules.json" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\feed_quality_metrics.json"); Dst = "feed_quality_metrics.json" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\features_export_provenance.json"); Dst = "features_export_provenance.json" }
        @{ Src = (Join-Path $RepoRoot "research\runtime\feed_ingest_health.json"); Dst = "feed_ingest_health.json" }
    )
    foreach ($p in $copyPairs) {
        if (Test-Path -LiteralPath $p.Src) {
            Copy-Item -LiteralPath $p.Src -Destination (Join-Path $dir $p.Dst) -Force
        }
    }
    if (Test-Path -LiteralPath $DigestFile) {
        Copy-Item -LiteralPath $DigestFile -Destination (Join-Path $dir "samples.digest.json") -Force
    }

    $manifest = [ordered]@{
        schema                   = "analytics_snapshot_v1"
        saved_at_local           = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
        digest_sha256            = $DigestSha
        digest_generation        = $DigestGen
        analytics_every_n_digests = $EveryN
        top_keywords_param       = $TopKw
        feature_rows_excl_header = $FeatureRows
        steps = [ordered]@{
            export_features = $ExportOk
            verify_spec     = $VerifyOk
            suggest_keywords = $SuggestOk
            train_v0        = $TrainV0Ok
            train_v1        = $TrainV1Ok
            eval_v0         = $EvalV0Ok
            eval_v1         = $EvalV1Ok
        }
        note                     = "Per-term scores in keyword_candidates.json (scores_top). Model/eval in eval_*.json and auto_baseline_*.json."
    }
    ($manifest | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath (Join-Path $dir "manifest.json") -Encoding UTF8
    Write-AnalyticLog "  snapshot -> $dir"
}

$exportPy = Join-Path $RepoRoot "scripts\export_features_v0.py"
$verifyPy = Join-Path $RepoRoot "scripts\verify_features_labels_spec.py"
$trainPy = Join-Path $RepoRoot "research\train_baseline_v0.py"
$suggestPy = Join-Path $RepoRoot "scripts\suggest_keywords_from_feed.py"
$evalPy = Join-Path $RepoRoot "research\evaluate_baseline_weights.py"
$feedMetricsPy = Join-Path $RepoRoot "scripts\compute_feed_metrics_v0.py"
$appendReportPy = Join-Path $RepoRoot "research\append_eval_to_experiment_report.py"
$writeFactoryEnvPy = Join-Path $RepoRoot "scripts\Write-FactoryBaselineRuntimeEnv.ps1"
$applyMcPy = Join-Path $RepoRoot "scripts\apply_mediacrawler_base_config.py"
$mergeMcEvalPy = Join-Path $RepoRoot "scripts\merge_mediacrawler_patch_from_eval.py"
foreach ($p in @($exportPy, $verifyPy, $trainPy, $suggestPy, $evalPy, $feedMetricsPy, $appendReportPy, $writeFactoryEnvPy, $applyMcPy, $mergeMcEvalPy)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-AnalyticLog "ERROR: missing $p"
        exit 1
    }
}

$artDir = Join-Path $RepoRoot "research\artifacts"
New-Item -ItemType Directory -Force -Path $artDir | Out-Null
$outV0 = Join-Path $artDir "auto_baseline_v0.json"
$outV1 = Join-Path $artDir "auto_baseline_v1.json"

$forceNext = $Force
$N = [int]$AnalyticsEveryNDigests
$mcRootResolved = $MediaCrawlerRoot.Trim()
if (-not $mcRootResolved) {
    $mcRootResolved = [Environment]::GetEnvironmentVariable("MEDIACRAWLER_ROOT", "Process")
}
if (-not $mcRootResolved) { $mcRootResolved = "D:\MediaCrawler" }

while ($true) {
    if (Test-Path -LiteralPath $StopFilePath) {
        Write-AnalyticLog "Stop file present; exit."
        exit 0
    }

    if (-not (Test-Path -LiteralPath $DigestPath)) {
        Write-AnalyticLog "WAIT: no digest yet ($DigestPath). Sleep ${IntervalMinutes}m ..."
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    $sha = Read-DigestSha256 $DigestPath
    if (-not $sha) {
        Write-AnalyticLog "WAIT: digest has no sha256. Sleep ${IntervalMinutes}m ..."
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    $g = Read-GenState $genStatePath

    $needRetrySameDigest = (
        ($g.digest_generation -gt 0) -and
        (($g.digest_generation % $N) -eq 0) -and
        ($g.last_run_generation -lt $g.digest_generation)
    )

    if ($sha -eq $g.last_counted_sha -and -not $forceNext -and -not $needRetrySameDigest) {
        Write-AnalyticLog "SKIP: digest unchanged ($($sha.Substring(0,12))...). Sleep ${IntervalMinutes}m"
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    if ($sha -ne $g.last_counted_sha) {
        $g.digest_generation = [int]$g.digest_generation + 1
        $g.last_counted_sha = $sha
        Write-GenState $g $genStatePath
        Write-AnalyticLog "DIGEST_EVENT: generation=$($g.digest_generation) sha=$($sha.Substring(0,12))..."
    }
    elseif ($needRetrySameDigest -or $forceNext) {
        Write-AnalyticLog "RETRY: same digest, re-run pipeline (gen=$($g.digest_generation))"
    }

    $shouldRun = $false
    if ($forceNext) {
        $shouldRun = $true
    }
    elseif (($g.digest_generation -gt 0) -and (($g.digest_generation % $N) -eq 0) -and ($g.last_run_generation -lt $g.digest_generation)) {
        $shouldRun = $true
    }

    if (-not $shouldRun) {
        Write-AnalyticLog "SKIP: analytics every $N digest events (now gen=$($g.digest_generation), last_run_gen=$($g.last_run_generation)). Sleep ${IntervalMinutes}m"
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    if (-not (Test-Path -LiteralPath $SamplesPath)) {
        Write-AnalyticLog "ERROR: samples missing $SamplesPath"
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }

    if (-not $LabelsSpec -or -not (Test-Path -LiteralPath $LabelsSpec)) {
        Write-AnalyticLog "ERROR: need research\labels_spec.json or labels_spec.example.json for export+verify"
        exit 2
    }

    $exportOk = $false
    $verifyOk = $false
    $suggestOk = $false
    $trainV0Ok = $false
    $trainV1Ok = $false
    $evalV0Ok = $false
    $evalV1Ok = $false
    $dataRows = 0

    Write-AnalyticLog "RUN: digest_gen=$($g.digest_generation) every_n=$N sha256=$sha"
    Write-AnalyticLog " export_features -> $FeaturesOut"

    $exportArgs = @(
        $exportPy,
        "--samples", $SamplesPath,
        "--out", $FeaturesOut,
        "--feed-digest", $DigestPath,
        "--verify-samples-digest",
        "--labels-spec", $LabelsSpec
    )
    & python @exportArgs
    if ($LASTEXITCODE -ne 0) {
        Write-AnalyticLog "ERROR: export_features exit=$LASTEXITCODE"
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }
    $exportOk = $true

    Write-AnalyticLog "  verify_features_labels_spec"
    & python $verifyPy --features $FeaturesOut --labels-spec $LabelsSpec
    if ($LASTEXITCODE -ne 0) {
        Write-AnalyticLog "ERROR: verify exit=$LASTEXITCODE"
        Start-Sleep -Seconds ($IntervalMinutes * 60)
        continue
    }
    $verifyOk = $true

    if (-not $SkipFeedMetrics) {
        Write-AnalyticLog "  compute_feed_metrics_v0 -> research/runtime/feed_quality_metrics.json"
        $fqOut = Join-Path $RepoRoot "research\runtime\feed_quality_metrics.json"
        & python $feedMetricsPy --features $FeaturesOut --out $fqOut
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "WARN: compute_feed_metrics_v0 exit=$LASTEXITCODE"
        }
    }

    $lines = (Get-Content -LiteralPath $FeaturesOut | Measure-Object -Line).Lines
    $dataRows = [Math]::Max(0, $lines - 1)
    Write-AnalyticLog "  features rows (excl header): $dataRows"

    if (-not $SkipSuggestKeywords) {
        $seedPath = Join-Path $RepoRoot "research\keyword_seed.txt"
        $seedKw = ""
        if (Test-Path -LiteralPath $seedPath) {
            $seedKw = (
                (Get-Content -LiteralPath $seedPath) |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -and -not $_.StartsWith("#") }
            ) -join ","
        }
        Write-AnalyticLog "  suggest_keywords_from_feed (top-keywords=$TopKeywords seed len=$($seedKw.Length))"
        $skArgs = @($suggestPy, "--samples", $SamplesPath, "--top-keywords", "$TopKeywords")
        if ($seedKw) { $skArgs += @("--seed-keywords", $seedKw) }
        & python @skArgs
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "WARN: suggest_keywords exit=$LASTEXITCODE"
        }
        else {
            $suggestOk = $true
        }
    }

    if (-not $SkipTrain) {
        $common = @(
            $trainPy,
            "--features", $FeaturesOut,
            "--labels-spec", $LabelsSpec,
            "--cv-folds", "$CvFolds"
        )
        if ($AllowMixedBatch) { $common += "--allow-mixed-batch" }

        Write-AnalyticLog "  train v0 -> $outV0"
        & python @($common + @("--out", $outV0, "--feature-schema", "v0"))
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "ERROR: train v0 exit=$LASTEXITCODE"
        }
        else { $trainV0Ok = $true }

        Write-AnalyticLog "  train v1 -> $outV1"
        & python @($common + @("--out", $outV1, "--feature-schema", "v1"))
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "ERROR: train v1 exit=$LASTEXITCODE"
        }
        else { $trainV1Ok = $true }
    }

    if (-not $SkipWeightEvaluation) {
        $eb = [Math]::Max(0, $EvalBootstrap)
        $th = [double]$EvalTimeHoldoutFraction
        Write-AnalyticLog "  evaluate_baseline_weights v0 (bootstrap=$eb time_holdout=$th)"
        $evArgs = @($evalPy, "--artifact", $outV0, "--bootstrap", "$eb")
        if ($AllowMixedBatch) { $evArgs += "--allow-mixed-batch" }
        if ($th -gt 0.0 -and $th -lt 1.0) { $evArgs += @("--time-holdout-fraction", "$th") }
        & python @evArgs
        $evalV0Ok = ($LASTEXITCODE -eq 0)
        if (-not $evalV0Ok) {
            Write-AnalyticLog "WARN: evaluate v0 exit=$LASTEXITCODE"
        }
        Write-AnalyticLog "  evaluate_baseline_weights v1 (bootstrap=$eb time_holdout=$th)"
        $evArgs1 = @($evalPy, "--artifact", $outV1, "--bootstrap", "$eb")
        if ($AllowMixedBatch) { $evArgs1 += "--allow-mixed-batch" }
        if ($th -gt 0.0 -and $th -lt 1.0) { $evArgs1 += @("--time-holdout-fraction", "$th") }
        & python @evArgs1
        $evalV1Ok = ($LASTEXITCODE -eq 0)
        if (-not $evalV1Ok) {
            Write-AnalyticLog "WARN: evaluate v1 exit=$LASTEXITCODE"
        }
        if ($evalV0Ok -and $evalV1Ok -and -not $SkipAppendExperimentReport) {
            $evalOut0 = Join-Path $artDir "eval_auto_baseline_v0.json"
            $evalOut1 = Join-Path $artDir "eval_auto_baseline_v1.json"
            if ((Test-Path -LiteralPath $evalOut0) -and (Test-Path -LiteralPath $evalOut1)) {
                Write-AnalyticLog "  append_eval_to_experiment_report (digest prefix)"
                $apArgs = @(
                    $appendReportPy,
                    "--digest-sha", $sha,
                    "--eval", $evalOut0,
                    "--eval", $evalOut1
                )
                & python @apArgs
                if ($LASTEXITCODE -ne 0) {
                    Write-AnalyticLog "WARN: append_eval_to_experiment_report exit=$LASTEXITCODE"
                }
            }
            else {
                Write-AnalyticLog "WARN: eval json missing; skip report append"
            }
        }
    }

    if (-not $SkipEvalPatchRules) {
        $evalV1Json = Join-Path $artDir "eval_auto_baseline_v1.json"
        if (Test-Path -LiteralPath $evalV1Json) {
            Write-AnalyticLog "  merge_mediacrawler_patch_from_eval (if rules file present)"
            & python $mergeMcEvalPy --repo-root $RepoRoot --eval $evalV1Json
            if ($LASTEXITCODE -ne 0) {
                Write-AnalyticLog "WARN: merge_mediacrawler_patch_from_eval exit=$LASTEXITCODE"
            }
        }
    }

    if (-not $SkipSyncFactoryBaseline -and -not $SkipTrain -and $trainV1Ok -and (Test-Path -LiteralPath $outV1)) {
        Write-AnalyticLog "  Write-FactoryBaselineRuntimeEnv (v1 artifact)"
        & $writeFactoryEnvPy -BaselineJsonPath $outV1 -RepoRoot $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "WARN: Write-FactoryBaselineRuntimeEnv exit=$LASTEXITCODE"
        }
    }

    if (-not $SkipApplyMediaCrawlerConfig) {
        Write-AnalyticLog "  apply_mediacrawler_base_config (if patch present)"
        & python $applyMcPy --mc-root $mcRootResolved
        if ($LASTEXITCODE -ne 0) {
            Write-AnalyticLog "WARN: apply_mediacrawler_base_config exit=$LASTEXITCODE"
        }
    }

    Save-AnalyticsSnapshot -SnapRoot $AnalyticsHistoryDir -DigestSha $sha -DigestFile $DigestPath -DigestGen $g.digest_generation `
        -EveryN $N -TopKw $TopKeywords -FeatureRows $dataRows `
        -ExportOk $exportOk -VerifyOk $verifyOk -SuggestOk $suggestOk `
        -TrainV0Ok $trainV0Ok -TrainV1Ok $trainV1Ok -EvalV0Ok $evalV0Ok -EvalV1Ok $evalV1Ok

    $fullSuccess = $exportOk -and $verifyOk -and (
        $SkipSuggestKeywords -or $suggestOk
    ) -and (
        $SkipTrain -or ($trainV0Ok -and $trainV1Ok)
    ) -and (
        $SkipWeightEvaluation -or ($evalV0Ok -and $evalV1Ok)
    )

    if ($fullSuccess) {
        $g.last_run_generation = $g.digest_generation
        Write-GenState $g $genStatePath
        Set-Content -LiteralPath $stateFile -Value $sha -Encoding UTF8 -NoNewline
    }
    else {
        Write-AnalyticLog "WARN: pipeline partial failure; last_run_generation unchanged (will retry on same digest_gen when eligible)."
    }

    $forceNext = $false
    Write-AnalyticLog "OK: cycle done. Sleep ${IntervalMinutes}m"
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
