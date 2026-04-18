"""
Hermes Cycle —— 周期性工作循环编排器。

（已从 hermes/ 迁出到 research/hermes_loop/，见本目录 README.md 的边界说明。）

完整链路：
  trigger_cycle(reason)
    1. collect_analytics_snapshot()   # 读最新 features + 生效的 keyword_pool
    2. propose_and_audit_loop()       # tuner → auditor 全链路（含软门禁 ε）
    3. commit_approved(proposal)      # 通过的提案物化落盘 + 生效
         - threshold:    append approved_tunings.jsonl
         - keyword_pool: 覆盖写 keyword_pool_active.json + （可选）trigger_crawler
         - prompt:       append approved_prompts.jsonl
    4. archive_rejected(rounds)       # 驳回的提案 append rejected_tunings.jsonl（审计留痕）
    5. 返回 CycleReport —— 上游（爬虫/dashboard）可据此决定下一步

语义：
  - 一次 cycle 只"提议 + 审核 + 落盘"一条提案；"多条提案"通过多次 cycle 实现
  - 通过的 threshold 提案 **不会自动覆盖 CURRENT_RULES**，而是 append 到 approved_tunings.jsonl
    → 是否应用到线上规则由人工（或后续专用脚本）审核后再 merge，保持"微调需二次确认"
  - 通过的 keyword_pool 提案 **会覆盖写 keyword_pool_active.json**
    → 因为"改关键词池"本身是低风险的运营操作；下一轮爬虫 / snapshot 立刻生效
  - 通过的 prompt 提案 **不会自动替换线上文案**，只 append，等人工或定时任务合并

重入保护（2026-04-18 新增，见 README "线性不冲突" 章节）：
  trigger_cycle 获取 research/artifacts/.hermes_loop.lock 作为进程级互斥锁。
  若检测到活锁（持有者 PID 仍存活且未过期）→ 直接 raise RuntimeError，不排队也不沉默。
  锁过期阈值默认 20 分钟（一次正常 cycle 绝不会超过），防止进程崩溃遗留死锁。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.hermes_loop.crawler_trigger import trigger_crawler  # noqa: E402
from research.hermes_loop.tuner import (  # noqa: E402
    collect_analytics_snapshot,
    propose_and_audit_loop,
)

ARTIFACTS_DIR = REPO_ROOT / "research" / "artifacts"
APPROVED_TUNINGS_JSONL = ARTIFACTS_DIR / "approved_tunings.jsonl"
APPROVED_PROMPTS_JSONL = ARTIFACTS_DIR / "approved_prompts.jsonl"
REJECTED_TUNINGS_JSONL = ARTIFACTS_DIR / "rejected_tunings.jsonl"
KEYWORD_POOL_ACTIVE_JSON = ARTIFACTS_DIR / "keyword_pool_active.json"
CYCLE_LOG_JSONL = ARTIFACTS_DIR / "cycle_log.jsonl"

LOCK_FILE = ARTIFACTS_DIR / ".hermes_loop.lock"
LOCK_STALE_SEC = 20 * 60  # 20 分钟；一次 cycle 实际最多约 3 分钟


@dataclass
class CycleReport:
    cycle_id: str
    ts_utc: str
    reason: str
    sample_count: int
    baseline_auc: float
    force_kind: str | None
    rounds: list[dict[str, Any]] = field(default_factory=list)
    final_verdict: str = "NO_PROPOSAL"
    approved_proposal: dict[str, Any] | None = None
    side_effects: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


CLI_KEYWORDS_FILE = REPO_ROOT / "research" / "keyword_candidates_for_cli.txt"
CLI_POOL_FALLBACK_TOPN = 20


def load_active_keyword_pool() -> list[str] | None:
    """读最新生效的 keyword_pool。

    真实管道优先级（不走任何硬编码虚拟数据）：
      1) Hermes 上一轮通过的 keyword_pool_active.json（最新 LLM 结论）
      2) 窗 A/C 实际在使用的 keyword_candidates_for_cli.txt（启发式产的真实爬池，
         取前 N 条作为 Hermes 本轮看到的"当前关注面"）
      3) 都没有 → None（由 tuner 兜底；生产环境下应避免走到这一步）
    """
    if KEYWORD_POOL_ACTIVE_JSON.is_file():
        try:
            obj = json.loads(KEYWORD_POOL_ACTIVE_JSON.read_text(encoding="utf-8"))
            kw = obj.get("keywords") or []
            if isinstance(kw, list) and kw:
                return [str(x) for x in kw]
        except Exception:  # noqa: BLE001
            pass

    if CLI_KEYWORDS_FILE.is_file():
        try:
            line = CLI_KEYWORDS_FILE.read_text(encoding="utf-8").strip()
            if line:
                parts = [p.strip() for p in line.split(",")]
                parts = [p for p in parts if p]
                if parts:
                    return parts[:CLI_POOL_FALLBACK_TOPN]
        except Exception:  # noqa: BLE001
            pass

    return None


def _pid_alive(pid: int) -> bool:
    """跨平台判断 pid 是否还活着（Windows + POSIX）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _CycleLock:
    """文件级互斥锁，防止 cycle 被多处同时触发。

    取锁策略（非阻塞）：
      1. 如果 lock 不存在 → 直接写入 pid/ts，持锁
      2. 如果 lock 存在：
         a. 解析失败 → 视为 stale，接管（写日志警告）
         b. 解析成功但 lock 年龄 > LOCK_STALE_SEC → 视为 stale，接管
         c. 解析成功且 pid 还活着且年龄 <= LOCK_STALE_SEC → raise RuntimeError
         d. 解析成功但 pid 已死 → 视为 stale，接管
    """

    def __init__(self, path: Path = LOCK_FILE) -> None:
        self.path = path

    def _is_stale(self, info: dict[str, Any]) -> bool:
        ts = info.get("started_at_unix")
        if isinstance(ts, (int, float)) and (time.time() - float(ts)) > LOCK_STALE_SEC:
            return True
        pid = info.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            return True
        return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            try:
                info = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                info = {}
            if info and not self._is_stale(info):
                raise RuntimeError(
                    "hermes_loop cycle already running "
                    f"(pid={info.get('pid')}, started_at={info.get('ts_utc')}); "
                    "refuse to re-enter. 若确认是死锁，请手动删除 "
                    f"{self.path.relative_to(REPO_ROOT)}"
                )
        payload = {
            "pid": os.getpid(),
            "ts_utc": _now_iso(),
            "started_at_unix": time.time(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def release(self) -> None:
        try:
            if self.path.is_file():
                self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "_CycleLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _commit_threshold(
    cycle_id: str, proposal: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    entry = {
        "cycle_id": cycle_id,
        "ts_utc": _now_iso(),
        "proposal": proposal,
        "audit": audit,
        "applied_to_runtime": False,
        "note": "Append only. Manual review needed before merging into CURRENT_RULES.",
    }
    _append_jsonl(APPROVED_TUNINGS_JSONL, entry)
    return {
        "type": "threshold_committed",
        "path": str(APPROVED_TUNINGS_JSONL.relative_to(REPO_ROOT)),
        "applied_to_runtime": False,
    }


def _commit_keyword_pool(
    cycle_id: str,
    proposal: dict[str, Any],
    audit: dict[str, Any],
    *,
    trigger_crawl: bool,
) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    after = proposal.get("after")
    if not isinstance(after, list):
        effects.append({"type": "keyword_pool_skipped", "reason": f"after not list: {type(after).__name__}"})
        return effects
    new_pool = [str(x) for x in after]
    payload = {
        "cycle_id": cycle_id,
        "ts_utc": _now_iso(),
        "keywords": new_pool,
        "source_proposal": proposal,
    }
    KEYWORD_POOL_ACTIVE_JSON.parent.mkdir(parents=True, exist_ok=True)
    KEYWORD_POOL_ACTIVE_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    effects.append(
        {
            "type": "keyword_pool_activated",
            "path": str(KEYWORD_POOL_ACTIVE_JSON.relative_to(REPO_ROOT)),
            "pool_size": len(new_pool),
        }
    )
    _append_jsonl(
        APPROVED_TUNINGS_JSONL,
        {
            "cycle_id": cycle_id,
            "ts_utc": _now_iso(),
            "proposal": proposal,
            "audit": audit,
            "applied_to_runtime": True,
            "note": "keyword_pool already activated; next snapshot/crawler will see it.",
        },
    )
    if trigger_crawl:
        r = trigger_crawler(
            keywords=new_pool,
            reason=f"cycle:{cycle_id}:keyword_pool_approved",
            extra_meta={"proposal_target": proposal.get("target")},
        )
        effects.append({"type": "crawler_triggered", "detail": r.to_dict()})
    return effects


def _commit_prompt(cycle_id: str, proposal: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "cycle_id": cycle_id,
        "ts_utc": _now_iso(),
        "proposal": proposal,
        "audit": audit,
        "applied_to_runtime": False,
        "note": "Append only. Manual review needed before merging into _BLOGGER_ACTION.",
    }
    _append_jsonl(APPROVED_PROMPTS_JSONL, entry)
    return {
        "type": "prompt_committed",
        "path": str(APPROVED_PROMPTS_JSONL.relative_to(REPO_ROOT)),
        "applied_to_runtime": False,
    }


def _archive_rejected(cycle_id: str, rounds: list[dict[str, Any]]) -> int:
    count = 0
    for row in rounds:
        if not row.get("proposal"):
            continue
        audit = row.get("audit") or {}
        if audit.get("passed"):
            continue
        failed = [g for g in audit.get("gates", []) if g.get("applicable") and not g.get("passed")]
        _append_jsonl(
            REJECTED_TUNINGS_JSONL,
            {
                "cycle_id": cycle_id,
                "ts_utc": _now_iso(),
                "round": row.get("round"),
                "proposal": row.get("proposal"),
                "verdict": audit.get("verdict"),
                "failed_gates": failed,
            },
        )
        count += 1
    return count


def trigger_cycle(
    reason: str,
    *,
    force_kind: str | None = None,
    max_rounds: int = 3,
    trigger_crawl_on_keyword_approval: bool = False,
) -> CycleReport:
    """一次完整的 Hermes 工作循环。

    参数:
      reason:         谁触发的、为什么（审计留痕用；如 "crawler_batch_done" / "cron_hourly" / "manual"）
      force_kind:     强制 tuner 只提某类（threshold/keyword_pool/prompt），不设则自由发挥
      max_rounds:     tuner→auditor 重试轮数上限
      trigger_crawl_on_keyword_approval: 审核通过 keyword_pool 提案后，是否立即触发爬虫
    """
    with _CycleLock():
        cycle_id = uuid.uuid4().hex[:12]
        ts = _now_iso()

        active_pool = load_active_keyword_pool()
        snapshot = collect_analytics_snapshot(keyword_pool=active_pool)

        rounds = propose_and_audit_loop(snapshot, max_rounds=max_rounds, force_kind=force_kind)

        report = CycleReport(
            cycle_id=cycle_id,
            ts_utc=ts,
            reason=reason,
            sample_count=snapshot.sample_count,
            baseline_auc=snapshot.baseline_auc,
            force_kind=force_kind,
            rounds=rounds,
        )

        approved_row = next((r for r in rounds if (r.get("audit") or {}).get("passed")), None)

        if approved_row is None:
            rejected_count = _archive_rejected(cycle_id, rounds)
            report.final_verdict = "ALL_ROUNDS_REJECTED" if rejected_count > 0 else "NO_PROPOSAL"
            report.side_effects.append(
                {"type": "rejected_archived", "count": rejected_count, "path": str(REJECTED_TUNINGS_JSONL.relative_to(REPO_ROOT))}
            )
        else:
            _archive_rejected(cycle_id, [r for r in rounds if r is not approved_row])
            proposal = approved_row["proposal"]
            audit = approved_row["audit"]
            report.approved_proposal = proposal
            report.final_verdict = "APPROVED_AND_COMMITTED"
            kind = proposal.get("kind")
            if kind == "threshold":
                report.side_effects.append(_commit_threshold(cycle_id, proposal, audit))
            elif kind == "keyword_pool":
                report.side_effects.extend(
                    _commit_keyword_pool(
                        cycle_id, proposal, audit,
                        trigger_crawl=trigger_crawl_on_keyword_approval,
                    )
                )
            elif kind == "prompt":
                report.side_effects.append(_commit_prompt(cycle_id, proposal, audit))
            else:
                report.side_effects.append({"type": "unknown_kind_skipped", "kind": kind})

        _append_jsonl(CYCLE_LOG_JSONL, report.to_dict())
        return report


def _print_report(r: CycleReport) -> None:
    print(f"=== CycleReport cycle_id={r.cycle_id} ===")
    print(f"  ts        = {r.ts_utc}")
    print(f"  reason    = {r.reason}")
    print(f"  force_kind= {r.force_kind}")
    print(f"  snapshot  = n={r.sample_count} baseline_auc={r.baseline_auc:.4f}")
    print(f"  verdict   = {r.final_verdict}")
    if r.approved_proposal:
        p = r.approved_proposal
        print(f"  approved  = [{p.get('kind')}] {p.get('target')}  {p.get('before')} → {p.get('after')}")
    print(f"  rounds    = {len(r.rounds)}")
    for row in r.rounds:
        pr = row.get("proposal")
        au = row.get("audit") or {}
        if pr:
            print(f"    R{row['round']}: {pr['kind']} {pr['target']}  {pr['before']}→{pr['after']}  → {au.get('verdict')}")
        else:
            print(f"    R{row['round']}: (no proposal)")
    print(f"  side_effects ({len(r.side_effects)}):")
    for eff in r.side_effects:
        print(f"    - {eff}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n### 场景 1：自由发挥，不触发爬虫 ###\n")
    report_a = trigger_cycle(reason="demo_free", trigger_crawl_on_keyword_approval=False)
    _print_report(report_a)
    print("\n### 场景 2：强制 threshold，跑完整硬+软门禁 ###\n")
    report_b = trigger_cycle(reason="demo_force_threshold", force_kind="threshold")
    _print_report(report_b)
    print("\n### 场景 3：强制 keyword_pool + 真的申请一次爬虫（降级写 intent）###\n")
    report_c = trigger_cycle(
        reason="demo_force_keyword_with_crawler",
        force_kind="keyword_pool",
        trigger_crawl_on_keyword_approval=True,
    )
    _print_report(report_c)
