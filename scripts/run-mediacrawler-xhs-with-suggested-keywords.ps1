#Requires -Version 5.1
# 使用 research/keyword_candidates_for_cli.txt 一行词启动 MediaCrawler（无文件则回退 base_config）
# 用法（在 D:\ai封装）: .\scripts\run-mediacrawler-xhs-with-suggested-keywords.ps1
# 另可：-KeywordsFile "D:\path\to\line.txt"

param(
    [string] $McRoot = "",
    [string] $KeywordsFile = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
if (-not $McRoot) {
    $McRoot = [Environment]::GetEnvironmentVariable("MEDIACRAWLER_ROOT", "Process")
}
if (-not $McRoot) { $McRoot = "D:\MediaCrawler" }
if (-not $KeywordsFile) {
    $KeywordsFile = Join-Path $repo "research\keyword_candidates_for_cli.txt"
}

$py = Join-Path $McRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "Not found: $py" -ForegroundColor Red
    exit 1
}

$kwArg = @()
if (Test-Path -LiteralPath $KeywordsFile) {
    $line = (Get-Content -LiteralPath $KeywordsFile -Raw -Encoding UTF8).Trim()
    if ($line) {
        $kwArg = @("--keywords", $line)
        Write-Host "Using --keywords from $KeywordsFile" -ForegroundColor Cyan
    }
}
else {
    Write-Host "No $KeywordsFile ; MediaCrawler uses base_config KEYWORDS" -ForegroundColor DarkYellow
}

Set-Location -LiteralPath $McRoot
$env:PYTHONUTF8 = "1"
$argv = @("main.py", "--platform", "xhs", "--lt", "qrcode", "--type", "search")
if ($kwArg.Count -gt 0) { $argv += $kwArg }
& $py @argv
exit $LASTEXITCODE
