# POST /task/sync: UTF-8 request body + UTF-8 response. Writes last-task-sync.json (raw API body).
# Default goal is read from task-sync-goal.default.txt (UTF-8) so PS5.x does not mangle CJK in script literals.
#
# Usage:
#   .\task-sync.ps1
#   .\task-sync.ps1 -GoalFile ".\my-goal.txt"
#   .\task-sync.ps1 -Goal "explicit goal" -MaxAttempts 3
param(
    [string] $Goal = "",
    [string] $GoalFile = "",
    [int] $MaxAttempts = 3,
    [string] $HermesBase = "http://127.0.0.1:8080",
    [string] $OutFile = "",
    [switch] $Pretty
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
. (Join-Path $here "utf8-http.ps1")

if (-not $OutFile) {
    $OutFile = Join-Path $here "last-task-sync.json"
}

if (-not $GoalFile) {
    $GoalFile = Join-Path $here "task-sync-goal.default.txt"
}

$g = $Goal.Trim()
if (-not $g) {
    $g = [System.IO.File]::ReadAllText($GoalFile, [System.Text.Encoding]::UTF8).Trim()
    if (-not $g) {
        throw "Goal is empty. Set -Goal, or put text in $GoalFile"
    }
}

$payloadObj = @{ goal = $g; max_attempts = $MaxAttempts }
$payload = $payloadObj | ConvertTo-Json -Compress -Depth 5
$url = ($HermesBase.TrimEnd("/")) + "/task/sync"

$raw = Invoke-HttpUtf8 -Method Post -Uri $url -JsonBody $payload -TimeoutSec 300

if ($Pretty) {
    $prettyText = ($raw | ConvertFrom-Json) | ConvertTo-Json -Depth 30
    Write-JsonUtf8File -Path $OutFile -JsonText $prettyText
}
else {
    Write-JsonUtf8File -Path $OutFile -JsonText $raw
}

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        chcp 65001 | Out-Null
    }
}
catch { }

Write-Host "Wrote: $OutFile" -ForegroundColor Green
if ($Pretty) {
    Write-Host $prettyText
}
else {
    ($raw | ConvertFrom-Json) | ConvertTo-Json -Depth 30 | Write-Host
}
