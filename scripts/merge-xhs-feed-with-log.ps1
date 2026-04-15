# 调用 merge-xhs-feed.ps1 并将全程输出追加到 logs/merge-xhs-feed.log（供计划任务使用）
# 用法: .\scripts\merge-xhs-feed-with-log.ps1
#       .\scripts\merge-xhs-feed-with-log.ps1 -LogDir "D:\ai封装\logs"

param(
    [string] $LogDir = ""
)

$here = $PSScriptRoot
$root = Split-Path $here -Parent
if (-not $LogDir) {
    $LogDir = Join-Path $root "logs"
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir "merge-xhs-feed.log"

Start-Transcript -LiteralPath $logFile -Append
try {
    $mergeScript = Join-Path $here "merge-xhs-feed.ps1"
    & $mergeScript
    exit $LASTEXITCODE
}
finally {
    Stop-Transcript
}
