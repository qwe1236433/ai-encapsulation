#Requires -Version 5.1
<#
.SYNOPSIS
  注册 / 卸载 Windows 计划任务：按固定间隔运行 auto-push-hourly.ps1（默认每 2 小时 / 120 分钟）。

.PARAMETER IntervalMinutes
  重复间隔（分钟），默认 120。

.PARAMETER Uninstall
  移除计划任务

.EXAMPLE
  .\install-hourly-push-task.ps1

.EXAMPLE
  .\install-hourly-push-task.ps1 -IntervalMinutes 10

.EXAMPLE
  .\install-hourly-push-task.ps1 -Uninstall
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

$TaskName = "AiFengzhuang-HourlyGitPush"
$ScriptPath = Join-Path $RepoRoot "auto-push-hourly.ps1"

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

if ($IntervalMinutes -ge 60 -and ($IntervalMinutes % 60) -eq 0) {
    $hrs = $IntervalMinutes / 60
    Write-Host "Registered: $TaskName (every $hrs hour(s), $IntervalMinutes min)" -ForegroundColor Green
}
else {
    Write-Host "Registered: $TaskName (every $IntervalMinutes min)" -ForegroundColor Green
}
Write-Host "Log: $RepoRoot\.local\auto-push-hourly.log"
Write-Host "Uninstall: .\install-hourly-push-task.ps1 -Uninstall"
