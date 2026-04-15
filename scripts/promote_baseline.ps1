<#
.SYNOPSIS
  将评估产出的 baseline JSON 晋升到工厂可读路径（先备份目标文件）。

.DESCRIPTION
  与 POST /api/model/evaluate 解耦：API 只训练与记录指标；本脚本负责受控复制。
  复制后请在 .env / docker-compose 中确认 XHS_FACTORY_BASELINE_JSON 指向 $Target（或重启容器）。

.PARAMETER Source
  源 artifact，例如 research\artifacts\api_eval_xxxxxxxx.json

.PARAMETER Target
  目标路径，例如 openclaw\data\artifacts\baseline_v0.json

.PARAMETER BackupDir
  可选；若设置且 Target 已存在，则先备份到 BackupDir（带时间戳后缀的 .bak.json）

.PARAMETER AllowOverwrite
  若 Target 已存在且未设 BackupDir，加此开关则直接覆盖（仍建议配合 BackupDir）。
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $Source,
    [Parameter(Mandatory = $true)]
    [string] $Target,
    [string] $BackupDir = "",
    [switch] $AllowOverwrite
)

$ErrorActionPreference = "Stop"
$src = Get-Item -LiteralPath $Source
$tgt = $Target
$tgtDir = Split-Path -Parent $tgt
if ($tgtDir -and -not (Test-Path -LiteralPath $tgtDir)) {
    New-Item -ItemType Directory -Path $tgtDir -Force | Out-Null
}
if (Test-Path -LiteralPath $tgt) {
    if ($BackupDir) {
        if (-not (Test-Path -LiteralPath $BackupDir)) {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
        }
        $name = [System.IO.Path]::GetFileName($tgt)
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        $bak = Join-Path $BackupDir ("{0}.{1}.bak.json" -f $name, $stamp)
        Copy-Item -LiteralPath $tgt -Destination $bak -Force
        Write-Host "Backed up existing target to $bak"
    } elseif (-not $AllowOverwrite) {
        Write-Error "Target exists: $tgt — use -BackupDir 或 -AllowOverwrite"
    }
}
Copy-Item -LiteralPath $src.FullName -Destination $tgt -Force
Write-Host "Promoted $($src.FullName) -> $tgt"
