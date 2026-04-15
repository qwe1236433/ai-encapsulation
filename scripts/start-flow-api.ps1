# 启动流程控制台：FastAPI（api/）+ 静态页（web/）默认 http://127.0.0.1:8099
# 用法：.\scripts\start-flow-api.ps1
#       .\scripts\start-flow-api.ps1 -Port 8099

param([int] $Port = 8099)

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root

Write-Host "安装/更新 api 依赖…" -ForegroundColor Gray
python -m pip install -q -r api\requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install 失败" -ForegroundColor Red
    exit 1
}

Write-Host "流程页: http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "API 文档: http://127.0.0.1:$Port/docs" -ForegroundColor Gray
Write-Host "Ctrl+C 停止" -ForegroundColor Gray

python -m uvicorn api.main:app --host 127.0.0.1 --port $Port --reload
