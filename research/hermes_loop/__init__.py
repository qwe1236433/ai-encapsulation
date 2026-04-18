"""Hermes 爬虫调参环（crawler-tuning loop）。

**这不是** `hermes/` 下那套 FastAPI HTTP 服务形态的 Hermes TAVC
（内容生成/发布编排；brain/runner/memory/scoring/verify 那一堆）。

这里是一个**独立的内部工具**：
  - 纯 Python 模块，可直接 import 调用，不走 HTTP
  - 读 research/features_v2.csv + research/artifacts/baseline_v2.json
  - 调 MiniMax（复用 hermes._minimax.call_minimax）提 "微调提案"：
      threshold / keyword_pool / prompt
  - 硬门禁（统计学）+ 软门禁（LLM 语义）审核
  - 通过的提案写 research/artifacts/{approved_tunings,rejected_tunings,
    approved_prompts,cycle_log,keyword_pool_active}.{jsonl,json}
  - 通过的 keyword_pool 由 scripts/hermes_closed_loop_tick.py 桥接到
    research/keyword_candidates_for_cli.txt 让窗 A 重启爬虫

架构上与主 Hermes TAVC 的关系：
  1. 不共享状态：主 TAVC 不读本目录产物，本目录不读主 TAVC 产物
  2. 不共享进程：主 TAVC 是 HTTP 服务；本目录是周期性脚本/API 直调
  3. 只共享一个 helper：hermes._minimax.call_minimax （LLM HTTP 封装）
  4. 若 MiniMax 限流冲突 → 两套会各自报错、各自重试，不会互相踩
"""

from research.hermes_loop.cycle import CycleReport, trigger_cycle  # noqa: F401
