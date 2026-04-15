# Hermes POST /task/sync (XHS factory chain).
# Each successful run also writes a UTF-8 .txt under outputs\xhs-runs\ and appends to outputs\xhs-articles-log.txt
# Usage: .\bench-hermes-xhs-sync.ps1
#        .\bench-hermes-xhs-sync.ps1 -Goal "your goal" -MaxAttempts 6
#        .\bench-hermes-xhs-sync.ps1 -GoalPath ".\scripts\bench-goal-example.txt"
#        .\bench-hermes-xhs-sync.ps1 -NoExport   # only print JSON, no txt files

param(
    [string] $BaseUrl = "http://127.0.0.1:8080",
    [string] $Goal = "XHS note: side hustle review, viral title and body structure",
    [string] $GoalPath = "",
    [int] $MaxAttempts = 6,
    [switch] $NoExport
)

$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if ($GoalPath -and $GoalPath.Trim()) {
    $gp = $GoalPath.Trim()
    if (-not (Test-Path -LiteralPath $gp)) {
        Write-Host "GoalPath not found: $gp" -ForegroundColor Red
        exit 1
    }
    $Goal = [System.IO.File]::ReadAllText($gp, [System.Text.UTF8Encoding]::new($false)).Trim()
}
# Hermes 仅在 goal 含「小红书 / xhs」等时才走 XHS 工厂链（extract→…→prepare_xhs_post）。
# 本脚本专用于小红书 bench；若用户只写主题未带平台词，会误走 predict_traffic 等通用算子，导出里只有 mock 流量、无正文。
$gNorm = $Goal.Trim()
if ($gNorm.Length -gt 0) {
    $gl = $gNorm.ToLowerInvariant()
    $hasXhs = ($gNorm.Contains('小红书')) -or ($gNorm.Contains('小红薯')) -or ($gl.Contains('xhs'))
    if (-not $hasXhs) {
        $Goal = '小红书：' + $gNorm
    }
}
$uri = $BaseUrl.TrimEnd('/') + '/task/sync'
$payload = @{ goal = $Goal; max_attempts = $MaxAttempts } | ConvertTo-Json -Compress
$ctype = 'application/json; charset=utf-8'
. (Join-Path $here 'utf8-http.ps1')

function Get-XhsArticleFromResponse {
    param($resp)
    $article = $null
    if ($resp.final_envelope -and $resp.final_envelope.openclaw -and $resp.final_envelope.openclaw.result) {
        $article = $resp.final_envelope.openclaw.result
    }
    if (-not $article -and $resp.trajectory) {
        foreach ($step in $resp.trajectory) {
            if ($step.phase -eq 'act' -and $step.envelope -and $step.envelope.action -eq 'prepare_xhs_post') {
                $oc = $step.envelope.openclaw
                if ($oc -and $oc.result) { $article = $oc.result }
            }
        }
    }
    return $article
}

function Format-HashtagsLine {
    param($tags)
    if (-not $tags) { return '' }
    if ($tags -is [string]) { return $tags }
    $parts = @()
    foreach ($t in $tags) {
        $s = [string]$t
        if ($s.Length -eq 0) { continue }
        if ($s.StartsWith('#')) { $parts += $s }
        else { $parts += ('#' + $s) }
    }
    return ($parts -join ' ')
}

function Export-XhsRunToTxt {
    param(
        [object] $resp,
        [string] $rootDir,
        [string] $hermesBase
 )
    $outRuns = Join-Path $rootDir 'outputs\xhs-runs'
    $master = Join-Path $rootDir 'outputs\xhs-articles-log.txt'
    if (-not (Test-Path -LiteralPath $outRuns)) {
        New-Item -ItemType Directory -Force -Path $outRuns | Out-Null
    }

    $tid = [string]$resp.task_id
    $tid8 = if ($tid.Length -ge 8) { $tid.Substring(0, 8) } else { $tid }
    $ts = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $perFile = Join-Path $outRuns ($ts + '_' + $tid8 + '.txt')

    $art = Get-XhsArticleFromResponse $resp
    $goal = [string]$resp.goal
    $fs = [string]$resp.final_status
    $lp = [string]$resp.lifecycle_phase
    $poll = $hermesBase.TrimEnd('/') + '/task/' + $tid

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('========== XHS factory export ==========')
    [void]$sb.AppendLine(('Time: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
    [void]$sb.AppendLine(('task_id: ' + $tid))
    [void]$sb.AppendLine(('goal: ' + $goal))
    [void]$sb.AppendLine(('final_status: ' + $fs))
    [void]$sb.AppendLine(('lifecycle_phase: ' + $lp))
    [void]$sb.AppendLine(('Poll full JSON: GET ' + $poll))
    [void]$sb.AppendLine('')

    if ($art) {
        $hl = [string]$art.headline
        $bd = [string]$art.body
        $ip = [string]$art.image_prompt
        $nt = [string]$art.notes
        $ht = Format-HashtagsLine $art.hashtags
        [void]$sb.AppendLine('[headline]')
        [void]$sb.AppendLine($hl)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('[body]')
        [void]$sb.AppendLine($bd)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('[hashtags]')
        [void]$sb.AppendLine($ht)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('[image_prompt]')
        [void]$sb.AppendLine($ip)
        [void]$sb.AppendLine('')
        if ($nt) {
            [void]$sb.AppendLine('[notes]')
            [void]$sb.AppendLine($nt)
            [void]$sb.AppendLine('')
        }
    }
    else {
        [void]$sb.AppendLine('(No prepare_xhs_post result; poll GET URL above for full JSON.)')
        [void]$sb.AppendLine('')
    }
    [void]$sb.AppendLine('========== end ==========')
    [void]$sb.AppendLine('')

    $text = $sb.ToString()
    $enc = New-Object System.Text.UTF8Encoding $true
    [System.IO.File]::WriteAllText($perFile, $text, $enc)
    [System.IO.File]::AppendAllText($master, $text, $enc)
    Write-Host "Exported txt: $perFile" -ForegroundColor Green
    Write-Host "Appended to log: $master" -ForegroundColor Green
}

Write-Host "POST $uri" -ForegroundColor Cyan
Write-Host "goal=$Goal max_attempts=$MaxAttempts" -ForegroundColor Gray

try {
    $jsonText = Invoke-HttpUtf8 -Method Post -Uri $uri -JsonBody $payload -TimeoutSec 600
    $r = $jsonText | ConvertFrom-Json
    $r | ConvertTo-Json -Depth 12
    if (-not $NoExport) {
        Export-XhsRunToTxt -resp $r -rootDir $here -hermesBase $BaseUrl
    }
}
catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
