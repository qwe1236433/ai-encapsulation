# 在 D:\MediaCrawler 启动小红书关键词搜索（二维码登录，需本机人工扫码）
# 用法：在 D:\ai封装 下 .\scripts\run-mediacrawler-xhs-search.ps1

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
$mc = "D:\MediaCrawler"
if (-not (Test-Path -LiteralPath "$mc\venv\Scripts\python.exe")) {
    Write-Host "未找到 $mc\venv ，请先按 MediaCrawler联调步骤.md 完成安装。" -ForegroundColor Red
    exit 1
}
Set-Location -LiteralPath $mc
$env:PYTHONUTF8 = "1"
Write-Host "MediaCrawler: 请在弹出窗口 120 秒内用小红书 App 扫码登录。" -ForegroundColor Cyan
& "$mc\venv\Scripts\python.exe" main.py --platform xhs --lt qrcode --type search
