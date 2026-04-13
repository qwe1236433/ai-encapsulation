# Phase 3: LLM path smoke — Hermes with Ollama (S1 think_mode=llm, S3 soft_reason contains llm_soft).
# Restarts hermes with HERMES_LLM_ENABLED=true and HERMES_LLM_VERIFY_MODE=always, then POST /task/sync.
#
# Usage:
#   .\phase3-smoke.ps1
#   .\phase3-smoke.ps1 -MaxAttempts 5 -OutFile .\phase3-last-task.json

param(
    [string] $Goal = "",
    [int] $MaxAttempts = 5,
    [string] $HermesBase = "http://127.0.0.1:8080",
    [string] $OutFile = ""
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
. (Join-Path $here "utf8-http.ps1")

if (-not $OutFile) {
    $OutFile = Join-Path $here "phase3-last-task.json"
}

if (-not $Goal.Trim()) {
    $gf = Join-Path $here "phase2-goal.default.txt"
    $Goal = [System.IO.File]::ReadAllText($gf, [System.Text.Encoding]::UTF8).Trim()
    if (-not $Goal) { throw "Empty phase2-goal.default.txt" }
}

Push-Location $here
try {
    $env:HERMES_LLM_ENABLED = 'true'
    $env:HERMES_LLM_VERIFY_MODE = 'always'
    docker compose build hermes
    docker compose up -d --force-recreate hermes
    Start-Sleep -Seconds 6
}
finally {
    Pop-Location
}

$payloadObj = @{ goal = $Goal.Trim(); max_attempts = $MaxAttempts }
$payload = $payloadObj | ConvertTo-Json -Compress -Depth 5
$url = ($HermesBase.TrimEnd("/")) + "/task/sync"

Write-Host "=== Phase3: LLM on, POST /task/sync (timeout 300s) ===" -ForegroundColor Cyan
Write-Host ("goal=" + $Goal.Trim() + " max_attempts=" + $MaxAttempts) -ForegroundColor Gray

$raw = Invoke-HttpUtf8 -Method Post -Uri $url -JsonBody $payload -TimeoutSec 300
Write-JsonUtf8File -Path $OutFile -JsonText $raw

$j = $raw | ConvertFrom-Json
$pass = [bool]$j.final_pass
$tr = @()
if ($j.trajectory) { $tr = @($j.trajectory) }

$thinkStep = $tr | Where-Object { $_.phase -eq 'think' } | Select-Object -First 1
$thinkMode = $null
if ($thinkStep -and $thinkStep.plan) {
    $thinkMode = [string]$thinkStep.plan.think_mode
}

$hasLlmSoft = $false
foreach ($v in ($tr | Where-Object { $_.phase -eq 'verify' })) {
    $sr = [string]$v.soft_reason
    if ($sr -like '*llm_soft*' -and $sr -notlike '*llm_soft_error_skip*') {
        $hasLlmSoft = $true
        break
    }
}

Write-Host ""
Write-Host ("final_pass=" + $pass + " think_mode=" + $thinkMode + " has_llm_soft=" + $hasLlmSoft) -ForegroundColor Gray
Write-Host ("Wrote: " + $OutFile) -ForegroundColor Green

Push-Location $here
try {
    Remove-Item Env:\HERMES_LLM_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:\HERMES_LLM_VERIFY_MODE -ErrorAction SilentlyContinue
    docker compose up -d hermes
    Write-Host "Restored hermes (cleared HERMES_LLM_* env for compose)." -ForegroundColor Yellow
}
finally {
    Pop-Location
}

$ok = ($thinkMode -eq 'llm') -and $hasLlmSoft
if (-not $ok) {
    Write-Host "PHASE3 FAIL: need think_mode=llm and llm_soft in verify. Got think_mode=$thinkMode llm_soft=$hasLlmSoft" -ForegroundColor Red
    Write-Host "Check Ollama from container: host.docker.internal:11434 /api/tags" -ForegroundColor Yellow
    exit 1
}

if (-not $pass) {
    Write-Host "PHASE3 NOTE: final_pass=false (hard fail or LLM soft FAIL). LLM integration still verified above." -ForegroundColor Yellow
}

Write-Host "PHASE3 PASS (S1 llm + S3 llm_soft OK)." -ForegroundColor Green
exit 0
