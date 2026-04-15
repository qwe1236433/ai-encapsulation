# 已改为走 FastAPI（api/ + web/ 同步）。等价于 start-flow-api.ps1。
# 用法：.\scripts\serve-flow-page.ps1
#       .\scripts\serve-flow-page.ps1 -Port 8765

param([int] $Port = 8099)

& (Join-Path $PSScriptRoot "start-flow-api.ps1") -Port $Port
