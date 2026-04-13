# Hermes POST /task/sync (XHS factory chain).
# Each successful run also writes a UTF-8 .txt under outputs\xhs-runs\ and appends to outputs\xhs-articles-log.txt
# Usage: .\bench-hermes-xhs-sync.ps1
#        .\bench-hermes-xhs-sync.ps1 -Goal "your goal" -MaxAttempts 6
#        .\bench-hermes-xhs-sync.ps1 -NoExport   # only print JSON, no txt files

param(
    [string] $BaseUrl = "http://127.0.0.1:8080",
    [string] $Goal = "XHS note: side hustle review, viral title and body structure",
    [int] $MaxAttempts = 6,
    [switch] $NoExport
)

$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$uri = $BaseUrl.TrimEnd('/') + '/task/sync'
$payload = @{ goal = $Goal; max_attempts = $MaxAttempts } | ConvertTo-Json -Compress
$ctype = 'application/json; charset=utf-8'

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
        [string] $hermesBase    )
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
    [void]$sb.AppendLine(('时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
    [void]$sb.AppendLine(('任务ID(task_id): ' + $tid))
    [void]$sb.AppendLine(('目标(goal): ' + $goal))
    [void]$sb.AppendLine(('final_status: ' + $fs))
    [void]$sb.AppendLine(('lifecycle_phase: ' + $lp))
    [void]$sb.AppendLine(('查询完整JSON: GET ' + $poll))
    [void]$sb.AppendLine('')

    if ($art) {
        $hl = [string]$art.headline
        $bd = [string]$art.body
        $ip = [string]$art.image_prompt
        $nt = [string]$art.notes
        $ht = Format-HashtagsLine $art.hashtags
        [void]$sb.AppendLine('【标题 headline】')
        [void]$sb.AppendLine($hl)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('【正文 body】')
        [void]$sb.AppendLine($bd)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('【话题标签 hashtags】')
        [void]$sb.AppendLine($ht)
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('【配图描述 image_prompt】')
        [void]$sb.AppendLine($ip)
        [void]$sb.AppendLine('')
        if ($nt) {
            [void]$sb.AppendLine('【备注 notes】')
            [void]$sb.AppendLine($nt)
            [void]$sb.AppendLine('')
        }
    }
    else {
        [void]$sb.AppendLine('（未找到 prepare_xhs_post 结果：可能停在中间步骤，请用上方 GET 地址查看完整 JSON。）')
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
    $r = Invoke-RestMethod -Uri $uri -Method Post -Body $payload -ContentType $ctype -TimeoutSec 600
    $r | ConvertTo-Json -Depth 12
    if (-not $NoExport) {
        Export-XhsRunToTxt -resp $r -rootDir $here -hermesBase $BaseUrl
    }
}
catch {
    Write-Host $_ -ForegroundColor Red
    if ($_.Exception.Response) {
        $reader = [System.IO.StreamReader]::new($_.Exception.Response.GetResponseStream())
        Write-Host $reader.ReadToEnd() -ForegroundColor Yellow
    }
    exit 1
}
