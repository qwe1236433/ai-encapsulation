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

$uri = $BaseUrl.TrimEnd('/') + '/task/' + $TaskId.Trim() + '/xhs-sync'
$o = @{ real_note_id = $RealNoteId.Trim() } | ConvertTo-Json -Compress
$ctype = 'application/json; charset=utf-8'

Write-Host "POST $uri" -ForegroundColor Cyan
Write-Host "real_note_id=$RealNoteId" -ForegroundColor Gray

try {
    $r = Invoke-RestMethod -Uri $uri -Method Post -Body $o -ContentType $ctype -TimeoutSec 600
    $r | ConvertTo-Json -Depth 14
}
catch {
    Write-Host $_ -ForegroundColor Red
    if ($_.Exception.Response) {
        $reader = [System.IO.StreamReader]::new($_.Exception.Response.GetResponseStream())
        Write-Host $reader.ReadToEnd() -ForegroundColor Yellow
    }
    exit 1
}
