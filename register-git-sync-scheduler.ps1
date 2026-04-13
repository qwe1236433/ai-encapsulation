# 以「当前用户」注册 Windows 计划任务：每 5 分钟执行 git-github-sync.ps1
# 需要：以管理员打开 PowerShell（部分环境创建/覆盖任务需要提升权限）
# 说明：任务推送的是「本机该目录当前检出分支」上的改动；`任务进程与结果总结.md` 等与代码同一提交。
#       若希望默认都进 GitHub 的 main，请在本机仓库保持 `git checkout main`。
#
# 用法（管理员 PowerShell）:
#   Set-Location -LiteralPath "D:\ai封装"
#   .\register-git-sync-scheduler.ps1
#
# 卸载:
#   Unregister-ScheduledTask -TaskName "AiFengzhuang-GitHub-Sync" -Confirm:$false

param(
    [string] $RepoPath = "",
    [int] $IntervalMinutes = 5,
    [string] $TaskName = "AiFengzhuang-GitHub-Sync"
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) {
    $RepoPath = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
}
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
$scriptPath = Join-Path $RepoPath "git-github-sync.ps1"

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Missing: $scriptPath"
}

$ps = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -RepoPath `"$RepoPath`""

$action = New-ScheduledTaskAction -Execute $ps -Argument $arg
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration ([TimeSpan]::FromDays(3650))
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered: $TaskName (every $IntervalMinutes min)" -ForegroundColor Green
Write-Host "Log: $RepoPath\.local\git-sync.log"
Write-Host "Test: Start-ScheduledTask -TaskName '$TaskName'"
