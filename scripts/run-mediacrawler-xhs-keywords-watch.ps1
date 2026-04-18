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

.PARAMETER NoAutoRestart
  子进程退出后不自动拉起（便于看清报错，避免死循环重启）。

.PARAMETER MinRestartDelaySeconds
  子进程退出后首次等待秒数，默认 5。

.PARAMETER MaxRestartDelaySeconds
  连续快速崩溃时退避上限（秒），默认 180。

.PARAMETER GiveUpAfterQuickExits
  连续「短于阈值即退出」的次数达到此值后，停止自动重启并退出本监视脚本（0=不限制）。便于避免浏览器反复关开。

.PARAMETER NoRedirectChildLogs
  不把 python 的 stdout/stderr 重定向到 logs（默认会重定向到 mediacrawler-child.*.log，便于查 exit=1 原因）。若扫码/浏览器异常可尝试加此开关。

.EXAMPLE
  .\scripts\run-mediacrawler-xhs-keywords-watch.ps1

.EXAMPLE
  爬虫一退就停，不重试：
  .\scripts\run-mediacrawler-xhs-keywords-watch.ps1 -NoAutoRestart

.EXAMPLE
  连续闪退 6 次后停，避免网页死循环重启：
  .\scripts\run-mediacrawler-xhs-keywords-watch.ps1 -GiveUpAfterQuickExits 6
#>
param(
    [string] $McRoot = "",
    [string] $KeywordsFile = "",
    [ValidateRange(1, 120)]
    [int] $DebounceSeconds = 4,
    [ValidateRange(1, 60)]
    [int] $PollSeconds = 2,
    [switch] $NoAutoRestart,
    [ValidateRange(1, 600)]
    [int] $MinRestartDelaySeconds = 5,
    [ValidateRange(5, 3600)]
    [int] $MaxRestartDelaySeconds = 180,
    [ValidateRange(0, 100)]
    [int] $GiveUpAfterQuickExits = 0,
    [switch] $NoRedirectChildLogs,
    [ValidateRange(0, 1440)]
    [int] $KeywordChangeMinGapMinutes = 60,
    [ValidateRange(0, 60)]
    [int] $GracefulStopSeconds = 5
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

function Stop-StaleMcChrome {
    <#
      只杀 MediaCrawler CDP 启动的 chrome.exe（命令行里含 browser_data\cdp_*_user_data_dir），
      不会误伤你日常 Chrome。用于启动/重启前清理残留窗口，避免出现"两个 Chrome 窗口、
      老窗口还亮着旧横幅"的情况。
    #>
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue
    } catch { return }
    if (-not $procs) { return }
    $killed = 0
    foreach ($p in $procs) {
        $cmd = $p.CommandLine
        if (-not $cmd) { continue }
        if ($cmd -match 'browser_data\\cdp_[^\\]*_user_data_dir') {
            try {
                & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null
                $killed++
            } catch { }
        }
    }
    if ($killed -gt 0) {
        Write-WatchLog ("Cleaned {0} stale MediaCrawler-CDP chrome.exe process(es) before launch." -f $killed)
        Start-Sleep -Milliseconds 800
    }
}

function Stop-CrawlerTree([System.Diagnostics.Process] $Proc, [int] $GracefulSec = 5) {
    if ($null -eq $Proc) { return }
    try {
        if ($Proc.HasExited) { return }
        & taskkill.exe /PID $Proc.Id /T 2>$null | Out-Null
        $deadline = (Get-Date).AddSeconds([Math]::Max(1, $GracefulSec))
        while ((Get-Date) -lt $deadline) {
            if ($Proc.HasExited) { return }
            Start-Sleep -Milliseconds 250
        }
    }
    catch { }
    try {
        if (-not $Proc.HasExited) {
            & taskkill.exe /PID $Proc.Id /T /F 2>$null | Out-Null
        }
    }
    catch { }
    try {
        if (-not $Proc.HasExited) { Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue }
    }
    catch { }
}

function Start-CrawlerProcess([string] $KwLine) {
    # 启动前先清理上一次遗留的 MC-CDP Chrome 进程，避免出现双窗口 / 老横幅仍在屏上的情况。
    # 识别依据：chrome.exe 命令行包含 browser_data\cdp_*_user_data_dir（MC 特有），不会影响日常 Chrome。
    Stop-StaleMcChrome
    $argv = @(
        "main.py",
        "--platform", "xhs",
        "--lt", "qrcode",
        "--type", "search"
    )
    if ($KwLine) {
        $argv += @("--keywords", $KwLine)
        Write-Host "Starting MediaCrawler with --keywords (len=$($KwLine.Length))" -ForegroundColor Cyan
        Write-WatchLog ("Starting MediaCrawler with --keywords (len={0})" -f $KwLine.Length)
    }
    else {
        Write-Host "Starting MediaCrawler without --keywords (base_config)" -ForegroundColor DarkYellow
        Write-WatchLog "Starting MediaCrawler without --keywords (base_config)"
    }
    if (-not $NoRedirectChildLogs) {
        $ld = Join-Path $repo "logs"
        New-Item -ItemType Directory -Force -Path $ld | Out-Null
        $script:__mcChildOut = Join-Path $ld "mediacrawler-child.stdout.log"
        $script:__mcChildErr = Join-Path $ld "mediacrawler-child.stderr.log"
        Write-Host "Child logs: $($script:__mcChildOut) , $($script:__mcChildErr)" -ForegroundColor DarkGray
        return Start-Process -FilePath $py -ArgumentList $argv -WorkingDirectory $McRoot -PassThru -WindowStyle Normal `
            -RedirectStandardOutput $script:__mcChildOut -RedirectStandardError $script:__mcChildErr
    }
    return Start-Process -FilePath $py -ArgumentList $argv -WorkingDirectory $McRoot -PassThru -WindowStyle Normal
}

$env:PYTHONUTF8 = "1"
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$watchLog = Join-Path $logDir "mediacrawler-watch.log"

function Write-WatchLog([string] $msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $watchLog -Value $line -Encoding UTF8
    Write-Host $line
}

Write-Host "Watching keywords: $KeywordsFile" -ForegroundColor Green
Write-Host "Watching MC config: $McRoot\config\base_config.py, xhs_config.py (debounce=${DebounceSeconds}s poll=${PollSeconds}s)" -ForegroundColor Green
Write-Host "McRoot: $McRoot"
Write-Host "Log: $watchLog"
Write-Host "提示: 浏览器若反复关开，多为 MediaCrawler 进程异常退出后本脚本在重启；查 exit=1 请看 logs\mediacrawler-child.stderr.log 与本日志尾部。" -ForegroundColor DarkYellow
if ($NoAutoRestart) { Write-Host "NoAutoRestart: child exit will NOT respawn." -ForegroundColor Yellow }
if ($GiveUpAfterQuickExits -gt 0) { Write-Host "GiveUpAfterQuickExits=$GiveUpAfterQuickExits : 连续短进程退出达此值后将停止自动重启。" -ForegroundColor DarkYellow }

$stableSig = Get-WatchSignature $KeywordsFile $McRoot
$pendingSince = $null
$pendingSig = $null
$child = $null
$consecutiveQuickExits = 0
$quickExitThresholdSec = 45
$lastKeywordSwitchUtc = $null
$cooldownNotifiedForSig = $null

function Get-RestartDelaySeconds {
    if ($consecutiveQuickExits -le 0) { return $MinRestartDelaySeconds }
    $extra = ($consecutiveQuickExits - 1) * 20
    $d = $MinRestartDelaySeconds + $extra
    if ($d -gt $MaxRestartDelaySeconds) { return $MaxRestartDelaySeconds }
    return $d
}

try {
    $line0 = Read-KeywordsLine $KeywordsFile
    $child = Start-CrawlerProcess $line0
    $childStartUtc = [DateTime]::UtcNow

    while ($true) {
        Start-Sleep -Seconds $PollSeconds

        if ($null -ne $child -and $child.HasExited) {
            $code = $child.ExitCode
            $runSec = ([DateTime]::UtcNow - $childStartUtc).TotalSeconds
            if ($runSec -lt $quickExitThresholdSec) {
                $consecutiveQuickExits++
            }
            else {
                $consecutiveQuickExits = 0
            }
            $codeHint = ""
            if ($code -eq -1073741510 -or $code -eq 3221225786) {
                $codeHint = " (0xC000013A: 多为 Ctrl+C / 关窗 / taskkill 中断)"
            }
            elseif ($code -eq 1) {
                $codeHint = " (常见: Python 异常退出；见 logs\mediacrawler-child.stderr.log 或加 -NoRedirectChildLogs 用交互控制台)"
            }
            Write-WatchLog ("MediaCrawler exited (code={0}{3}, ran {1:N1}s, quickExitStreak={2})." -f $code, $runSec, $consecutiveQuickExits, $codeHint)
            if (-not $NoRedirectChildLogs -and $script:__mcChildErr -and (Test-Path -LiteralPath $script:__mcChildErr) -and $code -ne 0) {
                Write-WatchLog "--- mediacrawler-child.stderr.log (tail 45) ---"
                Get-Content -LiteralPath $script:__mcChildErr -Tail 45 -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-WatchLog $_ }
            }
            if ($GiveUpAfterQuickExits -gt 0 -and $consecutiveQuickExits -ge $GiveUpAfterQuickExits) {
                Write-WatchLog "GiveUpAfterQuickExits=$GiveUpAfterQuickExits : 已达连续快速退出上限，停止自动重启（避免浏览器反复关开）。请根据上方 stderr 或 mediacrawler-child.stderr.log 修 MediaCrawler。"
                break
            }
            if ($NoAutoRestart) {
                Write-WatchLog "NoAutoRestart: exiting watch script."
                break
            }
            $delay = Get-RestartDelaySeconds
            Write-WatchLog "Restarting after ${delay}s (backoff; fix root cause if this repeats)."
            Start-Sleep -Seconds $delay
            $stableSig = Get-WatchSignature $KeywordsFile $McRoot
            $pendingSince = $null
            $pendingSig = $null
            $line = Read-KeywordsLine $KeywordsFile
            $child = Start-CrawlerProcess $line
            $childStartUtc = [DateTime]::UtcNow
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

        if ($KeywordChangeMinGapMinutes -gt 0 -and $null -ne $lastKeywordSwitchUtc) {
            $sinceLast = ([DateTime]::UtcNow - $lastKeywordSwitchUtc).TotalMinutes
            if ($sinceLast -lt $KeywordChangeMinGapMinutes) {
                if ($cooldownNotifiedForSig -ne $sig2) {
                    $remain = [Math]::Ceiling($KeywordChangeMinGapMinutes - $sinceLast)
                    Write-WatchLog ("Keywords changed (sig prefix {0}...) but cooldown active: last switch {1:N1} min ago < {2} min gap. Skipping restart; will re-apply when eligible. (account-safety)" -f $pfx, $sinceLast, $KeywordChangeMinGapMinutes)
                    $cooldownNotifiedForSig = $sig2
                }
                $pendingSince = $null
                $pendingSig = $null
                continue
            }
        }

        Write-Host "Keywords or MediaCrawler config changed (sig prefix $pfx...). Restarting crawler..." -ForegroundColor Cyan
        Write-WatchLog ("Keywords or MediaCrawler config changed (sig prefix {0}...). Restarting crawler." -f $pfx)
        Stop-CrawlerTree $child $GracefulStopSeconds
        $child = $null
        Start-Sleep -Seconds 2

        $stableSig = $sig2
        $lastKeywordSwitchUtc = [DateTime]::UtcNow
        $cooldownNotifiedForSig = $null
        $pendingSince = $null
        $pendingSig = $null
        $line = Read-KeywordsLine $KeywordsFile
        $child = Start-CrawlerProcess $line
        $childStartUtc = [DateTime]::UtcNow
    }
}
finally {
    Stop-CrawlerTree $child $GracefulStopSeconds
}
