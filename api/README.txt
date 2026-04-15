流程控制台（本机）

- 依赖：pip install -r api/requirements.txt
- 启动（仓库根）：scripts\start-flow-api.ps1 或 python -m uvicorn api.main:app --host 127.0.0.1 --port 8099
- 页面：http://127.0.0.1:8099/ API 文档：/docs
- 可选配置：根目录 .env 中 FLOW_API_*（见 .env.example）

仅用于本地 127.0.0.1，勿对公网暴露。
