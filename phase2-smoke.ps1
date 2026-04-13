# Phase 2: full TAVC smoke via POST /task/sync (assert final_pass).
# Default goal uses predict_traffic path with boost keyword for easy hard pass.
#
# Usage:
#   .\phase2-smoke.ps1
#   .\phase2-smoke.ps1 -RestartHermesNoLlm
#   .\phase2-smoke.ps1 -Goal "custom" -MaxAttempts 5

param(
    [string] $Goal = "",
    [int] $MaxAttempts = 5,
    [string] $HermesBase = "http://127.0.0.1:8080",
    [string] $OutFile = "",
    [switch] $RestartHermesNoLlm
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
. (Join-Path $here "utf8-http.ps1")

if (-not $OutFile) {
    $OutFile = Join-Path $here "phase2-last-task.json"
}

if (-not $Goal.Trim()) {
    $gf = Join-Path $here "phase2-goal.default.txt"
    $Goal = [System.IO.File]::ReadAllText($gf, [System.Text.Encoding]::UTF8).Trim()
    if (-not $Goal) { throw "Empty phase2-goal.default.txt" }
}

if ($RestartHermesNoLlm) {
    Push-Location $here
    try {
        $env:HERMES_LLM_ENABLED = 'false'
        docker compose up -d hermes
        Start-Sleep -Seconds 4
    }
    finally {
        Pop-Location
    }
}

$payloadObj = @{ goal = $Goal.Trim(); max_attempts = $MaxAttempts }
$payload = $payloadObj | ConvertTo-Json -Compress -Depth 5
$url = ($HermesBase.TrimEnd("/")) + "/task/sync"

Write-Host "=== Phase2: POST /task/sync (timeout 300s) ===" -ForegroundColor Cyan
Write-Host ("goal=" + $Goal.Trim() + " max_attempts=" + $MaxAttempts) -ForegroundColor Gray

$raw = Invoke-HttpUtf8 -Method Post -Uri $url -JsonBody $payload -TimeoutSec 300
Write-JsonUtf8File -Path $OutFile -JsonText $raw

$j = $raw | ConvertFrom-Json
$pass = [bool]$j.final_pass
$status = [string]$j.final_status
$n = 0
if ($j.trajectory) { $n = @($j.trajectory).Count }

Write-Host ""
Write-Host ("final_pass=" + $pass + " final_status=" + $status + " trajectory_steps=" + $n) -ForegroundColor $(if ($pass) { "Green" } else { "Red" })
Write-Host ("Wrote: " + $OutFile) -ForegroundColor Green

if ($RestartHermesNoLlm) {
    Push-Location $here
    try {
        Remove-Item Env:\HERMES_LLM_ENABLED -ErrorAction SilentlyContinue
        docker compose up -d hermes
        Write-Host "Restored hermes container (HERMES_LLM_ENABLED env cleared)." -ForegroundColor Yellow
    }
    finally {
        Pop-Location
    }
}

if (-not $pass) {
    Write-Host "PHASE2 FAIL: final_pass is false. See last_reason in output file." -ForegroundColor Red
    exit 1
}

Write-Host "PHASE2 PASS." -ForegroundColor Green
exit 0
