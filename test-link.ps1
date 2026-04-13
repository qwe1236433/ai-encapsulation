# 宿主机验证：请求两个服务的 /test-link（需先 docker compose up）
# 使用 utf8-http.ps1，避免 JSON 中文在无 charset 时被误解码。
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\utf8-http.ps1"

$hermes = "http://127.0.0.1:8080/test-link"
$openclaw = "http://127.0.0.1:3000/test-link"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        chcp 65001 | Out-Null
    }
}
catch { }

Write-Host "=== Hermes -> OpenClaw ===" -ForegroundColor Cyan
$h = Invoke-HttpUtf8 -Method Get -Uri $hermes
($h | ConvertFrom-Json) | ConvertTo-Json -Depth 6 | Write-Host

Write-Host "`n=== OpenClaw -> Hermes ===" -ForegroundColor Cyan
$o = Invoke-HttpUtf8 -Method Get -Uri $openclaw
($o | ConvertFrom-Json) | ConvertTo-Json -Depth 6 | Write-Host

Write-Host "`nPASS: both 'ok' fields should be true (Compose DNS + env vars OK)." -ForegroundColor Green
