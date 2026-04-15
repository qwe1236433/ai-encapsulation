# After manual XHS publish: bind real_note_id and resume Hermes monitoring.
# Usage:
#   .\bench-xhs-post-sync.ps1 -TaskId "uuid-from-bench" -RealNoteId "note-id-from-xhs-app"

param(
    [Parameter(Mandatory = $true)]
    [string] $TaskId,
    [Parameter(Mandatory = $true)]
    [string] $RealNoteId,
    [string] $BaseUrl = "http://127.0.0.1:8080"
)

$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
. (Join-Path $root 'utf8-http.ps1')

$uri = $BaseUrl.TrimEnd('/') + '/task/' + $TaskId.Trim() + '/xhs-sync'
$o = @{ real_note_id = $RealNoteId.Trim() } | ConvertTo-Json -Compress

Write-Host "POST $uri" -ForegroundColor Cyan
Write-Host "real_note_id=$RealNoteId" -ForegroundColor Gray

try {
    $jsonText = Invoke-HttpUtf8 -Method Post -Uri $uri -JsonBody $o -TimeoutSec 600
    $r = $jsonText | ConvertFrom-Json
    $r | ConvertTo-Json -Depth 14
}
catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
