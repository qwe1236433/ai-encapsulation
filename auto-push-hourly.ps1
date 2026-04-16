#Requires -Version 5.1
<#
.SYNOPSIS
  由计划任务按间隔触发（默认每 2 小时）：有改动则 git add/commit，再 push 当前分支。

.NOTES
  - 依赖本机已配置好的 remote 与代理（如 SakuraCat + git http.proxy）。
  - 无改动时仍会 git push（同步未推送的提交）。
  - 间隔在 install-hourly-push-task.ps1 的 -IntervalMinutes 中配置。
  - 若 push 失败请查看 .local\auto-push-hourly.log。
#>
$ErrorActionPreference = "Stop"

$RepoRoot = if ($env:AI_FENGZHUANG_ROOT) {
    $env:AI_FENGZHUANG_ROOT.Trim()
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$LogDir = Join-Path $RepoRoot ".local"
$LogFile = Join-Path $LogDir "auto-push-hourly.log"
$MaxLogLines = 2000

function Write-Log([string]$msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) {
        Write-Log "ERROR: not a git repo: $RepoRoot"
        exit 1
    }
    Set-Location -LiteralPath $RepoRoot
    $env:GIT_TERMINAL_PROMPT = "0"

    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if (-not $branch) {
        Write-Log "ERROR: cannot detect branch"
        exit 1
    }

    $dirty = git status --porcelain
    if ($dirty) {
        git add -A
        $msg = "chore: auto sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        git commit -m $msg
        Write-Log "Committed: $msg"
    }
    else {
        Write-Log "No local changes to commit."
    }

    $pushOut = git push origin $branch 2>&1
    foreach ($line in $pushOut) { Write-Log ([string]$line) }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: git push failed (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
    Write-Log "OK: pushed branch $branch"

    if (Test-Path -LiteralPath $LogFile) {
        $lines = Get-Content -LiteralPath $LogFile -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($lines -and $lines.Count -gt $MaxLogLines) {
            $tail = $lines[-$MaxLogLines..-1]
            Set-Content -LiteralPath $LogFile -Value $tail -Encoding UTF8
        }
    }
}
catch {
    Write-Log ("ERROR: " + $_.Exception.Message)
    exit 1
}
