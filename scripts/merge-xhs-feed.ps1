# 合并爬虫 raw → openclaw 工厂用 samples.json（调用 export_to_xhs_feed.py）
# 路径与去重策略优先读环境变量，其次读仓库根 .env（与 api/main 习惯一致）。
#
# 用法（在仓库根）:
#   .\scripts\merge-xhs-feed.ps1
#   .\scripts\merge-xhs-feed.ps1 -Dedupe key
#   .\scripts\merge-xhs-feed.ps1 -In "D:\path\to\jsonl" -Out "openclaw\data\xhs-feed\samples.json"
# 数据质量：未设置 FLOW_API_EXPORT_VALIDATE_MODE 时，本脚本默认 --validate-mode report（stderr 报告，不挡写出）。
#   关闭：-ValidateMode none 或 .env 中 FLOW_API_EXPORT_VALIDATE_MODE=none
# 入库前健康度：未设置 FLOW_API_FEED_HEALTH_GATE_MODE 时默认 report（写 research/runtime/feed_ingest_health.json，不挡写出）。
#   硬拦截：.env 设 FLOW_API_FEED_HEALTH_GATE_MODE=fail 并提供 FLOW_API_FEED_HEALTH_SPEC（或 -HealthSpec）。

param(
    [string] $InPath = "",
    [string] $OutPath = "",
    [string] $Dedupe = "",
    [string] $DigestOut = "",
    [string] $BatchId = "",
    [string] $ValidateMode = "",
    [string] $ValidateSchema = "",
    [string] $HealthGateMode = "",
    [string] $HealthSpec = "",
    [string] $HealthReportOut = "",
    [string] $HealthLabelsSpec = ""
)

$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root

function Get-DotEnvValue([string] $Path, [string] $Key) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $t = $line.Trim()
        if ($t.StartsWith("#") -or -not $t) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        if ($k -ne $Key) { continue }
        $v = $t.Substring($i + 1).Trim()
        if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        return $v
    }
    return $null
}

$envFile = Join-Path $root ".env"

if (-not $InPath) {
    $InPath = [Environment]::GetEnvironmentVariable("FLOW_API_MEDIACRAWLER_JSONL", "Process")
    if (-not $InPath) { $InPath = Get-DotEnvValue $envFile "FLOW_API_MEDIACRAWLER_JSONL" }
    if (-not $InPath) { $InPath = "D:\MediaCrawler\data\xhs\jsonl" }
}

if (-not $OutPath) {
    $OutPath = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_OUT", "Process")
    if (-not $OutPath) { $OutPath = Get-DotEnvValue $envFile "FLOW_API_FEED_OUT" }
    if (-not $OutPath) { $OutPath = Join-Path $root "openclaw\data\xhs-feed\samples.json" }
}

$dedupeVal = $Dedupe.Trim().ToLowerInvariant()
if ($dedupeVal -and $dedupeVal -notin @("none", "key", "content")) {
    Write-Host "Dedupe must be none, key, or content. Got: $Dedupe" -ForegroundColor Red
    exit 2
}
if (-not $dedupeVal) {
    $dedupeVal = [Environment]::GetEnvironmentVariable("FLOW_API_EXPORT_DEDUPE", "Process")
    if (-not $dedupeVal) { $dedupeVal = Get-DotEnvValue $envFile "FLOW_API_EXPORT_DEDUPE" }
    if (-not $dedupeVal) { $dedupeVal = "none" }
    $dedupeVal = $dedupeVal.Trim().ToLowerInvariant()
    if ($dedupeVal -notin @("none", "key", "content")) { $dedupeVal = "none" }
}

if (-not (Test-Path -LiteralPath $InPath)) {
    Write-Host "Input path not found: $InPath (set FLOW_API_MEDIACRAWLER_JSONL or use -In)" -ForegroundColor Red
    exit 2
}

$outDir = Split-Path -Parent $OutPath
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$digestVal = $DigestOut.Trim()
if (-not $digestVal) {
    $digestVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_DIGEST_OUT", "Process")
    if (-not $digestVal) { $digestVal = Get-DotEnvValue $envFile "FLOW_API_FEED_DIGEST_OUT" }
    if (-not $digestVal) { $digestVal = "" }
}

$batchVal = $BatchId.Trim()
if (-not $batchVal) {
    $batchVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_BATCH_ID", "Process")
    if (-not $batchVal) { $batchVal = Get-DotEnvValue $envFile "FLOW_API_FEED_BATCH_ID" }
    if (-not $batchVal) { $batchVal = "" }
}

$validateVal = $ValidateMode.Trim().ToLowerInvariant()
if ($validateVal -and $validateVal -notin @("none", "report", "warn", "fail")) {
    Write-Host "ValidateMode must be none, report, warn, or fail. Got: $ValidateMode" -ForegroundColor Red
    exit 2
}
if (-not $validateVal) {
    $validateVal = [Environment]::GetEnvironmentVariable("FLOW_API_EXPORT_VALIDATE_MODE", "Process")
    if (-not $validateVal) { $validateVal = Get-DotEnvValue $envFile "FLOW_API_EXPORT_VALIDATE_MODE" }
    if (-not $validateVal) { $validateVal = "report" }
    $validateVal = $validateVal.Trim().ToLowerInvariant()
    if ($validateVal -notin @("none", "report", "warn", "fail")) { $validateVal = "report" }
}

$schemaVal = $ValidateSchema.Trim()
if (-not $schemaVal) {
    $schemaVal = [Environment]::GetEnvironmentVariable("FLOW_API_EXPORT_VALIDATE_SCHEMA", "Process")
    if (-not $schemaVal) { $schemaVal = Get-DotEnvValue $envFile "FLOW_API_EXPORT_VALIDATE_SCHEMA" }
    if (-not $schemaVal) { $schemaVal = "" }
}

Write-Host "Merge: $InPath -> $OutPath (dedupe=$dedupeVal validate=$validateVal)" -ForegroundColor Cyan
$pyArgs = @(
    "scripts\export_to_xhs_feed.py",
    "--in", $InPath,
    "--out", $OutPath,
    "--dedupe", $dedupeVal
)
if ($digestVal) {
    $pyArgs += @("--digest-out", $digestVal)
}
if ($batchVal) {
    $pyArgs += @("--batch-id", $batchVal)
}
if ($validateVal -ne "none") {
    $pyArgs += @("--validate-mode", $validateVal)
}
if ($schemaVal) {
    $pyArgs += @("--validate-schema", $schemaVal)
}

$healthGateVal = $HealthGateMode.Trim().ToLowerInvariant()
if ($healthGateVal -and $healthGateVal -notin @("none", "report", "fail")) {
    Write-Host "HealthGateMode must be none, report, or fail. Got: $HealthGateMode" -ForegroundColor Red
    exit 2
}
if (-not $healthGateVal) {
    $healthGateVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_HEALTH_GATE_MODE", "Process")
    if (-not $healthGateVal) { $healthGateVal = Get-DotEnvValue $envFile "FLOW_API_FEED_HEALTH_GATE_MODE" }
    if (-not $healthGateVal) { $healthGateVal = "report" }
    $healthGateVal = $healthGateVal.Trim().ToLowerInvariant()
    if ($healthGateVal -notin @("none", "report", "fail")) { $healthGateVal = "report" }
}

$healthSpecVal = $HealthSpec.Trim()
if (-not $healthSpecVal) {
    $healthSpecVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_HEALTH_SPEC", "Process")
    if (-not $healthSpecVal) { $healthSpecVal = Get-DotEnvValue $envFile "FLOW_API_FEED_HEALTH_SPEC" }
    if (-not $healthSpecVal) { $healthSpecVal = "" }
}

$healthReportVal = $HealthReportOut.Trim()
if (-not $healthReportVal) {
    $healthReportVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_HEALTH_REPORT_OUT", "Process")
    if (-not $healthReportVal) { $healthReportVal = Get-DotEnvValue $envFile "FLOW_API_FEED_HEALTH_REPORT_OUT" }
    if (-not $healthReportVal) {
        $healthReportVal = Join-Path $root "research\runtime\feed_ingest_health.json"
    }
}

$healthLabelsVal = $HealthLabelsSpec.Trim()
if (-not $healthLabelsVal) {
    $healthLabelsVal = [Environment]::GetEnvironmentVariable("FLOW_API_FEED_HEALTH_LABELS_SPEC", "Process")
    if (-not $healthLabelsVal) { $healthLabelsVal = Get-DotEnvValue $envFile "FLOW_API_FEED_HEALTH_LABELS_SPEC" }
    if (-not $healthLabelsVal) {
        $lsH = Join-Path $root "research\labels_spec.json"
        $lsEx = Join-Path $root "research\labels_spec.example.json"
        if (Test-Path -LiteralPath $lsH) { $healthLabelsVal = $lsH }
        elseif (Test-Path -LiteralPath $lsEx) { $healthLabelsVal = $lsEx }
        else { $healthLabelsVal = "" }
    }
}

if ($healthGateVal -ne "none") {
    $pyArgs += @("--health-gate-mode", $healthGateVal)
}
if ($healthSpecVal) {
    $pyArgs += @("--health-spec", $healthSpecVal)
}
if ($healthGateVal -ne "none" -and $healthReportVal) {
    $pyArgs += @("--health-report-out", $healthReportVal)
}
if ($healthLabelsVal) {
    $pyArgs += @("--health-labels-spec", $healthLabelsVal)
}

Write-Host "Health: gate=$healthGateVal spec=$(if ($healthSpecVal) { $healthSpecVal } else { '(none)' })" -ForegroundColor Cyan
& python @pyArgs
exit $LASTEXITCODE
