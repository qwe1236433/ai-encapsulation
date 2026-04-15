# MediaCrawler 联调（已与 `D:\MediaCrawler` 做过第一步）

本页对应你本机 **`D:\MediaCrawler`**（与 **`D:\ai封装`** 分开）。下面写「已经替你做过什么」和「你要点哪一步」。

---

## 已完成的机器侧准备（无需你再装一遍）

1. **仓库**：已从 GitHub 克隆 **NanmiCoder/MediaCrawler** 到 `D:\MediaCrawler`。  
2. **Python 3.11**：已通过 `winget` 安装（若重装系统需再来一次）。  
3. **虚拟环境**：`D:\MediaCrawler\venv`，并已 `pip install -r requirements.txt`。  
4. **Playwright**：已在 venv 内执行 `playwright install chromium`。  
5. **配置**（`D:\MediaCrawler\config\base_config.py`）：  
   - 关键词 **`牛排熟度`**，每条链路最多 **3** 条笔记；  
   - **关闭 CDP**，用标准 Playwright（免先开 Chrome 远程调试）；  
   - **关闭评论**，首跑更快；  
   - 保存格式仍为 **jsonl**（默认）。  
6. **Cursor 双根目录**：`dev-workspace.code-workspace` 里第二项已改为 **`D:/MediaCrawler`**。

---

## 你要做的唯一关键一步：扫码登录

1. 用 PowerShell 执行（或运行 `D:\ai封装\scripts\run-mediacrawler-xhs-search.ps1`）：  

   ```powershell
   Set-Location -LiteralPath "D:\MediaCrawler"
   $env:PYTHONUTF8 = "1"
   .\venv\Scripts\python.exe main.py --platform xhs --lt qrcode --type search
   ```

2. 会弹出浏览器窗口，日志里会出现 **waiting for scan code login, remaining time is 120s**。  
3. 用 **小红书 App** 在 **120 秒内** 扫码登录。  
4. 登录成功后，程序会继续搜「牛排熟度」并写入数据。

---

## 数据会出现在哪里？

默认目录（无自定义 `SAVE_DATA_PATH` 时）：

- `D:\MediaCrawler\data\xhs\jsonl\`  
- 常见文件：`contents.jsonl`（笔记正文等，以你当前版本为准）。

若文件夹还不存在：**说明还没登录成功或还没爬到写入时机**，先完成扫码。

---

## 接到 `ai封装` 工厂（第二步）

在 **`D:\ai封装`** 下：

```powershell
Set-Location -LiteralPath "D:\ai封装"
python scripts\export_to_xhs_feed.py --in "D:\MediaCrawler\data\xhs\jsonl" --out "openclaw\data\xhs-feed\samples.json"
```

（若本机 `python` 不可用，把命令里的 `python` 换成 **`C:\Users\你的用户名\AppData\Local\Programs\Python\Python311\python.exe`**。）

然后确认 `.env` 里 OpenClaw 使用容器路径，例如：

`XHS_FACTORY_SAMPLES_PATH=/app/data/xhs-feed/samples.json`

再 `docker compose up -d`（若改过 compose / 镜像再按需 `build`）。

---

## 想恢复「官方推荐」CDP 模式时

编辑 `D:\MediaCrawler\config\base_config.py`：

- `ENABLE_CDP_MODE = True`  
- `CDP_CONNECT_EXISTING = True`  
- 按 MediaCrawler README 打开 Chrome **远程调试**（端口 **9222**）。

---

## 合规提醒

仅作学习与研究用途，控制频率与条数，遵守平台规则与当地法律。
