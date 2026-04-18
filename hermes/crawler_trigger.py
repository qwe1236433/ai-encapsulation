"""
Hermes 的"启动爬虫"能力。

两种模式（由环境变量 HERMES_CRAWLER_CMD 决定）：
  1. 真模式（已配）：subprocess 调用外部爬虫命令，关键词通过 HERMES_CRAWLER_KEYWORDS 环境变量传入
  2. 降级模式（未配）：只把"想扒什么"写到 research/artifacts/crawler_requests.jsonl
     作为 intent 留痕，运维/脚本可据此手动触发真爬虫

设计原则：
  - 不编造"爬虫已经接好"的假象 —— 降级模式输出的是审计意图，不是"已执行"
  - 不阻塞调用方 —— 真模式下 subprocess 不等完成，只确认它起得来
  - 可追溯 —— 无论真降，都写 crawler_requests.jsonl
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
CRAWLER_REQUEST_LOG = REPO_ROOT / "research" / "artifacts" / "crawler_requests.jsonl"

ENV_CMD = "HERMES_CRAWLER_CMD"
ENV_CWD = "HERMES_CRAWLER_CWD"
ENV_TIMEOUT = "HERMES_CRAWLER_TIMEOUT_SEC"


@dataclass
class CrawlerTriggerResult:
    mode: Literal["real", "dryrun"]
    ok: bool
    keywords: list[str]
    reason: str
    pid: int | None = None
    cmd: str | None = None
    intent_logged_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append_intent(entry: dict[str, Any]) -> str:
    CRAWLER_REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CRAWLER_REQUEST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["ts_utc"]


def trigger_crawler(
    keywords: list[str],
    reason: str,
    *,
    batch_size: int | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CrawlerTriggerResult:
    """Hermes 申请启动一次爬取。先写 intent，再决定真跑还是降级。

    参数:
      keywords: 本次要扒的关键词池（通常来自 auditor 通过的 keyword_pool 提案的 after 字段）
      reason:   触发原因，审计用（如 "tuner_approved_keyword_pool"）
      batch_size: 建议爬取条数；交给真爬虫消费，降级模式仅记录
      extra_meta: 附加信息，写入 intent 留痕
    """
    ts = _now_iso()
    intent = {
        "ts_utc": ts,
        "reason": reason,
        "keywords": list(keywords),
        "batch_size": batch_size,
        "mode_requested": "real" if os.environ.get(ENV_CMD) else "dryrun",
        "meta": extra_meta or {},
    }
    _append_intent(intent)

    cmd_str = os.environ.get(ENV_CMD, "").strip()
    if not cmd_str:
        return CrawlerTriggerResult(
            mode="dryrun",
            ok=True,
            keywords=keywords,
            reason=reason,
            intent_logged_at=ts,
        )

    try:
        cwd = os.environ.get(ENV_CWD, str(REPO_ROOT))
        timeout = int(os.environ.get(ENV_TIMEOUT, "10"))
        cmd_parts = shlex.split(cmd_str, posix=(os.name != "nt"))
        env = dict(os.environ)
        env["HERMES_CRAWLER_KEYWORDS"] = ",".join(keywords)
        env["HERMES_CRAWLER_REASON"] = reason
        if batch_size:
            env["HERMES_CRAWLER_BATCH_SIZE"] = str(batch_size)
        proc = subprocess.Popen(
            cmd_parts,
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            rc = proc.wait(timeout=min(timeout, 3))
            if rc != 0:
                return CrawlerTriggerResult(
                    mode="real",
                    ok=False,
                    keywords=keywords,
                    reason=reason,
                    cmd=cmd_str,
                    pid=proc.pid,
                    intent_logged_at=ts,
                    error=f"crawler exited immediately with code={rc}",
                )
        except subprocess.TimeoutExpired:
            pass
        return CrawlerTriggerResult(
            mode="real",
            ok=True,
            keywords=keywords,
            reason=reason,
            cmd=cmd_str,
            pid=proc.pid,
            intent_logged_at=ts,
        )
    except FileNotFoundError as e:
        return CrawlerTriggerResult(
            mode="real",
            ok=False,
            keywords=keywords,
            reason=reason,
            cmd=cmd_str,
            intent_logged_at=ts,
            error=f"crawler cmd not found: {e}",
        )
    except Exception as e:  # noqa: BLE001
        return CrawlerTriggerResult(
            mode="real",
            ok=False,
            keywords=keywords,
            reason=reason,
            cmd=cmd_str,
            intent_logged_at=ts,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


if __name__ == "__main__":
    r = trigger_crawler(
        keywords=["减脂餐", "低卡食谱", "健康餐"],
        reason="cli_smoke_test",
        batch_size=50,
    )
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
