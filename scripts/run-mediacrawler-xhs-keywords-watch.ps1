#Requires -Version 5.1
<#
.SYNOPSIS
  监视 keyword_candidates_for_cli.txt 与 MediaCrawler config/base_config.py、config/xhs_config.py；变更后结束爬虫并带新 --keywords 重启。

.DESCRIPTION
  轮询 SHA256 + 防抖。apply_mediacrawler_base_config.py 写入 config 后，本脚本会检测到并 taskkill 重启。
  停止子进程使用 taskkill /T。

.PARAMETER DebounceSeconds
  检测到变化后再等待的秒数，默认 4。

.PARAMETER PollSeconds
  轮询间隔，默认 2。

.EXAMPLE
  .\scripts\run-mediacrawler-xhs-keywords-watch.ps1
#>
param(
    [string] $McRoot = "",
    [string] $KeywordsFile = "",
    [ValidateRange(1, 120)]
    [int] $DebounceSeconds = 4,
    [ValidateRange(1, 60)]
    [int] $PollSeconds = 2
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

function Get-Sha256HexOfFile([string] $LiteralPath) {
    if (-not (Test-Path -LiteralPath $LiteralPath)) { return "" }
    $full = Convert-Path -LiteralPath $LiteralPath
    $fs = [System.IO.File]::OpenRead($full)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha.ComputeHash($fs)
            return [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
        }
        finally { $sha.Dispose() }
    }
    finally { $fs.Dispose() }
}

function Get-KwFingerprint([string] $Path) {
    return (Get-Sha256HexOfFile $Path)
}

function Get-McConfigFingerprint([string] $Root) {
    $parts = @()
    foreach ($rel in @("config\base_config.py", "config\xhs_config.py")) {
        $p = Join-Path $Root $rel
        if (Test-Path -LiteralPath $p) {
            $parts += (Get-Sha256HexOfFile $p)
        }
        else {
            $parts += "-"
        }
    }
    return ($parts -join "|")
}

function Get-WatchSignature([string] $KwPath, [string] $McR) {
    $k = Get-KwFingerprint $KwPath
    $c = Get-McConfigFingerprint $McR
    return "${k}|${c}"
}

function Read-KeywordsLine([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim()
}

function Stop-CrawlerTree([System.Diagnostics.Process] $Proc) {
    if ($null -eq $Proc) { return }
    try {
        if (-not $Proc.HasExited) {
            & taskkill.exe /PID $Proc.Id /T /F 2>$null
        }
    }
    catch { }
    try {
        if (-not $Proc.HasExited) { Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue }
    }
    catch { }
}

function Start-CrawlerProcess([string] $KwLine) {
    $argv = @(
        "main.py",
        "--platform", "xhs",
        "--lt", "qrcode",
        "--type", "search"
    )
    if ($KwLine) {
        $argv += @("--keywords", $KwLine)
        Write-Host "Starting MediaCrawler with --keywords (len=$($KwLine.Length))" -ForegroundColor Cyan
    }
    else {
        Write-Host "Starting MediaCrawler without --keywords (base_config)" -ForegroundColor DarkYellow
    }
    return Start-Process -FilePath $py -ArgumentList $argv -WorkingDirectory $McRoot -PassThru -WindowStyle Normal
}

$env:PYTHONUTF8 = "1"
Write-Host "Watching keywords: $KeywordsFile" -ForegroundColor Green
Write-Host "Watching MC config: $McRoot\config\base_config.py, xhs_config.py (debounce=${DebounceSeconds}s poll=${PollSeconds}s)" -ForegroundColor Green
Write-Host "McRoot: $McRoot"

$stableSig = Get-WatchSignature $KeywordsFile $McRoot
$pendingSince = $null
$pendingSig = $null
$child = $null

try {
    $line0 = Read-KeywordsLine $KeywordsFile
    $child = Start-CrawlerProcess $line0

    while ($true) {
        Start-Sleep -Seconds $PollSeconds

        if ($null -ne $child -and $child.HasExited) {
            Write-Host "MediaCrawler exited (code=$($child.ExitCode)). Restarting with current keywords in 3s..." -ForegroundColor Yellow
            Start-Sleep -Seconds 3
            $stableSig = Get-WatchSignature $KeywordsFile $McRoot
            $pendingSince = $null
            $pendingSig = $null
            $line = Read-KeywordsLine $KeywordsFile
            $child = Start-CrawlerProcess $line
            continue
        }

        $sig = Get-WatchSignature $KeywordsFile $McRoot
        if ($sig -eq $stableSig) {
            $pendingSince = $null
            $pendingSig = $null
            continue
        }

        if ($null -eq $pendingSince -or $sig -ne $pendingSig) {
            $pendingSig = $sig
            $pendingSince = [DateTime]::UtcNow
            continue
        }

        $elapsed = ([DateTime]::UtcNow - $pendingSince).TotalSeconds
        if ($elapsed -lt $DebounceSeconds) { continue }

        $sig2 = Get-WatchSignature $KeywordsFile $McRoot
        if ($sig2 -ne $pendingSig) {
            $pendingSig = $sig2
            $pendingSince = [DateTime]::UtcNow
            continue
        }

        $pfx = if ($sig2.Length -ge 16) { $sig2.Substring(0, 16) } else { $sig2 }
        Write-Host "Keywords or MediaCrawler config changed (sig prefix $pfx...). Restarting crawler..." -ForegroundColor Cyan
        Stop-CrawlerTree $child
        $child = $null
        Start-Sleep -Seconds 2

        $stableSig = $sig2
        $pendingSince = $null
        $pendingSig = $null
        $line = Read-KeywordsLine $KeywordsFile
        $child = Start-CrawlerProcess $line
    }
}
finally {
    Stop-CrawlerTree $child
}
