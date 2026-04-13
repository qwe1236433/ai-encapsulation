#Requires -Version 5.1
<#
.SYNOPSIS
  从 Hermes 会话 JSON（sessions/*.json）生成人类可读的 TAVC 总结报告（控制台 +可选写入文本文件）。

.PARAMETER SessionPath
  会话文件路径，例如 hermes\sessions\<task_id>.json

.PARAMETER OutFile
  可选；将报告写入 UTF-8 文本文件（不含 Markdown专有语法，纯文本）。

.EXAMPLE
  .\summarize-tavc-session.ps1 -SessionPath ".\hermes\sessions\1add5c42-c0be-4880-ad2b-48864cb8648d.json"

.EXAMPLE
  .\summarize-tavc-session.ps1 -SessionPath ".\hermes\sessions\1add5c42-c0be-4880-ad2b-48864cb8648d.json" -OutFile ".\reports\run-fail.txt"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $SessionPath,
    [string] $OutFile = ""
)

$ErrorActionPreference = "Stop"

$p = $SessionPath
if (-not [System.IO.Path]::IsPathRooted($p)) {
    $p = Join-Path $PSScriptRoot $p
}
if (-not (Test-Path -LiteralPath $p)) {
    throw "Session file not found: $p"
}

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        chcp 65001 | Out-Null
    }
}
catch { }

$raw = Get-Content -LiteralPath $p -Raw -Encoding UTF8
$j = $raw | ConvertFrom-Json

$sb = New-Object System.Text.StringBuilder

function Add-Line { param([string]$s) { [void]$sb.AppendLine($s) } }

Add-Line "========== TAVC 会话总结 =========="
Add-Line "文件: $p"
Add-Line "task_id: $($j.task_id)"
Add-Line "goal: $($j.goal)"
Add-Line "status / lifecycle: $($j.status) / $($j.lifecycle_phase)"
Add-Line "final_pass: $($j.final_pass)"
Add-Line "final_status: $($j.final_status)"
Add-Line "updated_at: $($j.updated_at)"
Add-Line ""
Add-Line "--- last_reason（聚合自最后一次 Verify）---"
Add-Line ($j.last_reason -replace "`r`n", " ")
Add-Line ""

$tr = @($j.trajectory)
if ($tr.Count -eq 0) {
    Add-Line "(无 trajectory)"
}
else {
    Add-Line "--- 轨迹按步摘要 ---"
    $step = 0
    foreach ($t in $tr) {
        $step++
        $ph = $t.phase
        if ($ph -eq "think") {
            $pl = $t.plan
            $mode = $pl.think_mode
            $act = $pl.action
            $ids = @($pl.retrieved_case_ids)
            Add-Line "[$step] THINK  mode=$mode  action=$act  retrieved_cases=$($ids.Count)"
        }
        elseif ($ph -eq "act") {
            $a = $t.attempt
            $va = $t.variant_id
            $env = $t.envelope
            $action = $env.action
            $oc = $env.openclaw
            $snippet = ""
            if ($oc -and $oc.result) {
                $r = $oc.result
                if ($r.headline) { $snippet = "headline=$($r.headline)" }
                elseif ($r.PSObject.Properties.Name -contains "predicted_likes") {
                    $snippet = "predicted_likes=$($r.predicted_likes) boost_hit=$($r.boost_keyword_hit)"
                }
            }
            Add-Line "[$step] ACT attempt=$a variant=$va action=$action  $snippet"
        }
        elseif ($ph -eq "verify") {
            $a = $t.attempt
            $ok = $t.pass
            $hp = $t.hard_pass
            $sp = $t.soft_pass
            $m = $t.metrics
            $ml = if ($m) { $m.likes } else { "" }
            $mc = if ($m) { $m.ctr_pct } else { "" }
            Add-Line "[$step] VERIFY attempt=$a pass=$ok hard=$hp soft=$sp  metrics likes=$ml ctr_pct=$mc"
            Add-Line " hard_reason: $($t.hard_reason)"
            Add-Line "      soft_reason: $($t.soft_reason)"
        }
        elseif ($ph -eq "correct") {
            $rev = $t.revision
            Add-Line "[$step] CORRECT attempt=$($t.attempt) -> action=$($rev.action) mode=$($rev.correct_mode) variant=$($rev.variant_id)"
        }
        else {
            Add-Line "[$step] $($ph)"
        }
    }
}

Add-Line ""
Add-Line "--- 负样本池（本会话内）---"
$np = $j.negative_sample_pool
if ($np) {
    $n = @($np).Count
    Add-Line "entries: $n"
    $i = 0
    foreach ($e in $np) {
        $i++
        Add-Line "  [$i] $($e.action) strike=$($e.strike_count) fp=$($e.fp) sev=$($e.failure_severity) reason=$($e.hard_reason)"
    }
}
else {
    Add-Line "(无)"
}

$sum = $j.obs_negative_pool_summary
if ($sum) {
    Add-Line ""
    Add-Line "--- obs_negative_pool_summary ---"
    Add-Line ($sum | ConvertTo-Json -Compress -Depth 5)
}

Add-Line ""
Add-Line "========== 报告结束 =========="

$text = $sb.ToString()
Write-Host $text

if ($OutFile) {
    $of = $OutFile
    if (-not [System.IO.Path]::IsPathRooted($of)) {
        $of = Join-Path $PSScriptRoot $of
    }
    $dir = Split-Path -Parent $of
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($of, $text, $utf8NoBom)
    Write-Host ""
    Write-Host "Wrote: $of" -ForegroundColor Green
}
