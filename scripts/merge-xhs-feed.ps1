# 合并爬虫 raw → openclaw 工厂用 samples.json（调用 export_to_xhs_feed.py）
# 路径与去重策略优先读环境变量，其次读仓库根 .env（与 api/main 习惯一致）。
#
# 用法（在仓库根）:
#   .\scripts\merge-xhs-feed.ps1
#   .\scripts\merge-xhs-feed.ps1 -Dedupe key
#   .\scripts\merge-xhs-feed.ps1 -In "D:\path\to\jsonl" -Out "openclaw\data\xhs-feed\samples.json"

param(
    [string] $InPath = "",
    [string] $OutPath = "",
    [string] $Dedupe = "",
    [string] $DigestOut = "",
    [string] $BatchId = ""
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

Write-Host "Merge: $InPath -> $OutPath (dedupe=$dedupeVal)" -ForegroundColor Cyan
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
& python @pyArgs
exit $LASTEXITCODE
