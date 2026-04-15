# 将当前仓库变更拉取、提交并推送到 origin（供计划任务每 N 分钟调用）。
# 默认尽量「全量」同步：除 .gitignore 中密钥/会话等项外，对 bench 输出等历史被 ignore 的路径用 git add -f 纳入。
# 仍不会提交：.env、.local/、hermes/sessions/、openclaw/data/（见 .gitignore）。
#
# 手动： Set-Location D:\ai封装; .\git-github-sync.ps1
# 干跑：  .\git-github-sync.ps1 -DryRun

param(
    [string] $RepoPath = "",
    [string] $Remote = "origin",
    [switch] $DryRun,
    [switch] $NoForceIgnoredArtifacts
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) {
    $RepoPath = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
}
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path

$localDir = Join-Path $RepoPath ".local"
$lockFile = Join-Path $localDir "git-sync.lock"
$logFile = Join-Path $localDir "git-sync.log"

if (Test-Path -LiteralPath $lockFile) {
    $age = (Get-Date) - (Get-Item -LiteralPath $lockFile).LastWriteTime
    if ($age.TotalMinutes -gt 30) {
        Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
    }
}

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    if (-not (Test-Path -LiteralPath $localDir)) {
        New-Item -ItemType Directory -Force -Path $localDir | Out-Null
    }
    Add-Content -LiteralPath $logFile -Value $line -Encoding utf8
    Write-Host $line
}

try {
    $fs = [System.IO.File]::Open(
        $lockFile,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
}
catch {
    Write-Log "skip: another sync is running (lock)"
    exit 0
}

$prevEap = $ErrorActionPreference
try {
    Push-Location $RepoPath
    # git 会把提示写到 stderr；Stop 时 stderr 可能被当成异常，日志只剩第一行。
    $ErrorActionPreference = "Continue"

    git rev-parse --git-dir 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: not a git repository: $RepoPath"
        exit 1
    }

    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if (-not $branch) {
        Write-Log "error: cannot detect branch"
        exit 1
    }

    Write-Log "branch=$branch pull --rebase $Remote/$branch"

    if ($DryRun) {
        git status --short
        exit 0
    }

    # 不要对 git 使用管道写日志：管道末尾会覆盖 $LASTEXITCODE，导致误判成功、后面既不报错也不写 ok。
    $fetchOut = git fetch $Remote 2>&1
    foreach ($line in @($fetchOut)) { Write-Log "$line" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: git fetch failed exit=$LASTEXITCODE"
        exit 1
    }

    $pullOut = git pull --rebase --autostash $Remote $branch 2>&1
    foreach ($line in @($pullOut)) { Write-Log "$line" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: pull --rebase failed exit=$LASTEXITCODE (resolve conflicts manually)"
        exit 1
    }

    # Do not exit early on empty porcelain: ignored artifacts can still need a commit.
    $addAllOut = git add -A 2>&1
    foreach ($line in @($addAllOut)) { Write-Log "$line" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: git add -A failed exit=$LASTEXITCODE"
        exit 1
    }

    if (-not $NoForceIgnoredArtifacts) {
        $forceRelPaths = @(
            "outputs/xhs-runs",
            "outputs/xhs-articles-log.txt",
            "reports",
            "bench-last-run.json",
            "last-task-sync.json",
            "auto-push-hourly.ps1",
            "install-hourly-push-task.ps1"
        )
        foreach ($rel in $forceRelPaths) {
            $abs = Join-Path $RepoPath $rel
            if (Test-Path -LiteralPath $abs) {
                $addOut = git add -f -- $rel 2>&1
                foreach ($line in @($addOut)) { Write-Log "$line" }
                if ($LASTEXITCODE -ne 0) {
                    Write-Log "error: git add -f failed for $rel exit=$LASTEXITCODE"
                    exit 1
                }
            }
        }
        $lastTaskFiles = @(Get-ChildItem -LiteralPath $RepoPath -Filter "*-last-task.json" -File -ErrorAction SilentlyContinue)
        foreach ($taskFile in $lastTaskFiles) {
            $rel = $taskFile.Name
            $addOut = git add -f -- $rel 2>&1
            foreach ($line in @($addOut)) { Write-Log "$line" }
            if ($LASTEXITCODE -ne 0) {
                Write-Log "error: git add -f failed for $rel exit=$LASTEXITCODE"
                exit 1
            }
        }
    }

    # 与「主工程」并列、常改的说明类文档：显式再 add 一次。
    $docRelPaths = @(
        "任务进程与结果总结.md",
        "使用说明书.md",
        "EVOLUTION.md"
    )
    foreach ($rel in $docRelPaths) {
        $abs = Join-Path $RepoPath $rel
        if (Test-Path -LiteralPath $abs) {
            $addOut = git add -- $rel 2>&1
            foreach ($line in @($addOut)) { Write-Log "$line" }
            if ($LASTEXITCODE -ne 0) {
                Write-Log "error: git add failed for $rel exit=$LASTEXITCODE"
                exit 1
            }
        }
    }

    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Log "ok: nothing to commit"
        exit 0
    }

    $msg = "chore(auto): sync {0:yyyy-MM-dd HH:mm:ss}" -f (Get-Date)
    git commit -m $msg
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: git commit failed"
        exit 1
    }

    $pushOut = git push $Remote $branch 2>&1
    foreach ($line in @($pushOut)) { Write-Log "$line" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "error: git push failed exit=$LASTEXITCODE"
        exit 1
    }
    Write-Log "ok: pushed $branch"
    exit 0
}
finally {
    $ErrorActionPreference = $prevEap
    Pop-Location
    if ($fs) { $fs.Close(); $fs.Dispose() }
}
