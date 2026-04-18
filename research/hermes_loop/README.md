# `research/hermes_loop/` — Hermes 爬虫调参环

> 2026-04-18 从 `hermes/` 目录下迁出，与 `hermes/` 里的 FastAPI 服务形 **TAVC 主体**（内容生成/发布）**物理分家**。本目录只管"爬虫怎么扒"，与"生成什么内容"无关。

## 两套 Hermes 的边界

| 维度 | `hermes/`（主 TAVC） | `research/hermes_loop/`（本目录） |
|---|---|---|
| **形态** | FastAPI HTTP 服务 | 纯 Python 模块，可直接 import |
| **入口** | `hermes/main.py::app` + `routes.py` | `research.hermes_loop.cycle::trigger_cycle` |
| **调度** | 跟着 dispatch/task HTTP 请求 | 30 分钟一次 tick（窗 D） + `api/main.py` `/api/hermes/cycle` + `schedule_hermes_cycle.py` |
| **决策对象** | "下一条小红书笔记生成什么内容" | "爬虫下一轮扒什么关键词、用什么阈值" |
| **核心文件** | `brain.py / runner.py / memory.py / scoring.py / verify.py / metrics.py / negative_pool.py / client.py / models.py / storage.py / settings.py / routes.py / main.py` | `cycle.py / tuner.py / auditor.py / crawler_trigger.py` |
| **产物目录** | 主 TAVC 自己的 memory 目录（不在本目录管辖） | `research/artifacts/{approved_tunings,rejected_tunings,approved_prompts,cycle_log,keyword_pool_active}*` |
| **LLM 调用** | `hermes._minimax.call_minimax` | `hermes._minimax.call_minimax`（共用 helper） |
| **是否读对方产物** | ❌ 不读本目录任何文件 | ❌ 不读主 TAVC 任何文件 |

## 为什么两套不会冲突

1. **代码层**：零共享状态（只共享 `_minimax.py` 这个 HTTP 封装 helper）。
2. **文件层**：各写各的目录，互不覆盖。
3. **进程层**：主 TAVC 是常驻 HTTP 服务；本目录是周期性调用 / HTTP 路由直调，按需触发。
4. **限流层**：若 MiniMax 并发限流，各自 HTTP 拿 429 自己退避。

## 本目录内部的重入保护（新增 2026-04-18）

`cycle.trigger_cycle` 用 `research/artifacts/.hermes_loop.lock` 作为**进程级文件锁**，防止：
- 窗 D（`scripts/hermes_closed_loop_tick.py`）还没跑完，
- 又有人打了 `/api/hermes/cycle` 或在终端跑了 `python scripts/schedule_hermes_cycle.py`

重入时直接 `RuntimeError('hermes_loop cycle already running, pid=...')`，不排队、不沉默吃掉。

## 文件索引

| 文件 | 作用 |
|---|---|
| `cycle.py` | 一次 cycle 的编排：snapshot → propose → audit → commit → archive |
| `tuner.py` | LLM 微调提议器（把 snapshot 扔给 MiniMax 拿回 TuningProposal） |
| `auditor.py` | 硬门禁（α/β/γ/δ 统计规则）+ 软门禁（ε 语义 LLM 复核） |
| `crawler_trigger.py` | 可选：真 subprocess 启爬虫 / 降级写 `crawler_requests.jsonl` |

## 调用入口

```python
from research.hermes_loop.cycle import trigger_cycle

report = trigger_cycle(reason="manual", max_rounds=3)
print(report.to_dict())
```

或 CLI：

```powershell
python scripts\schedule_hermes_cycle.py --reason manual
python scripts\hermes_closed_loop_tick.py --reason closed_loop_tick --force
```

或 HTTP：

```
POST /api/hermes/cycle  { "reason": "...", "max_rounds": 3, ... }
```
