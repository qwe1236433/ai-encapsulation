#Requires -Version 5.1
<#
.SYNOPSIS
  注册 / 卸载 Windows 计划任务：按间隔运行 scripts/merge-xhs-feed-with-log.ps1（合并爬虫 -> samples.json）。
  日志中可能出现 merge_stats、validate_stats（默认 validate=report 时）：属正常质量报告，非错误。

.PARAMETER IntervalMinutes
  重复间隔（分钟），默认 120。

.PARAMETER Uninstall
  移除计划任务

.PARAMETER RepoRoot
  仓库根目录（默认为本脚本所在目录）

.EXAMPLE
  .\install-merge-xhs-feed-scheduled-task.ps1

.EXAMPLE
  .\install-merge-xhs-feed-scheduled-task.ps1 -IntervalMinutes 60

.EXAMPLE
  .\install-merge-xhs-feed-scheduled-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch] $Uninstall,
    [ValidateRange(1, 1440)]
    [int] $IntervalMinutes = 120,
    [string] $RepoRoot = ""
)

if (-not $RepoRoot) {
    $RepoRoot = $PSScriptRoot
}

$TaskName = "AiFengzhuang-MergeXhsFeed"
$ScriptPath = Join-Path $RepoRoot "scripts\merge-xhs-feed-with-log.ps1"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Script not found: $ScriptPath"
}

$exe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh.exe" } else { "powershell.exe" }
$argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

$action = New-ScheduledTaskAction -Execute $exe -Argument $argLine
$start = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

$logPath = Join-Path $RepoRoot "logs\merge-xhs-feed.log"
Write-Host "Registered: $TaskName (every $IntervalMinutes min)" -ForegroundColor Green
Write-Host "Log: $logPath"
Write-Host "提示：日志里的 validate_stats / merge_stats 为数据质量输出；若需关闭校验可在 .env 设 FLOW_API_EXPORT_VALIDATE_MODE=none 或对 merge 传 -ValidateMode none" -ForegroundColor DarkGray
Write-Host "Uninstall: .\install-merge-xhs-feed-scheduled-task.ps1 -Uninstall"
