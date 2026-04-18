"""
Hermes 闭环的一次"滴答"（tick）。

本脚本是窗 D（hermes-closed-loop.ps1）的干活体；它 **只** 做一件事：
    「如果 v2 数据就绪 → 触发一次 Hermes cycle → 若通过 keyword_pool 提案，
      则把新关键词池写入 research/keyword_candidates_for_cli.txt，
      让窗 A 的 run-mediacrawler-xhs-keywords-watch.ps1 自己重启爬虫」

为什么不直接 subprocess 爬虫：
  - 爬虫常驻在窗 A 里（SAVE_LOGIN_STATE=True、浏览器登录态），我们不想自己再起一份；
  - 窗 A 已经稳定工作多轮，直接复用它的监视链是最少风险的接入。

与窗 C 的 suggest_keywords_from_feed.py 的分工（由用户决策 "heuristic_first"）：
  - 窗 C 周期性写入启发式候选词（**打底**）；
  - Hermes tick 只在 LLM 提案通过硬+软门禁后 **覆盖一次** CLI 文件；
  - 下一轮窗 C 的启发式再把它压回来，形成 "Hermes 瞬时干预 + 启发式长期锚定" 的节律。

退出码（被 PS 调度器读取）：
  0  = 正常跑完一轮（无论是否有提案通过；都是"健康的 tick"）
  2  = 前置条件缺失（features_v2.csv 或 baseline_v2.json 未就绪）
  3  = 未捕获异常（详见日志）

日志：logs/hermes-closed-loop.log（每 tick 追加一行 JSON）
状态：logs/hermes-closed-loop-state.json（最近一次使用的 baseline sha256 等）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

FEATURES_V2 = REPO_ROOT / "research" / "features_v2.csv"
BASELINE_V2 = REPO_ROOT / "research" / "artifacts" / "baseline_v2.json"
KEYWORD_POOL_ACTIVE = REPO_ROOT / "research" / "artifacts" / "keyword_pool_active.json"
CLI_KEYWORDS_FILE = REPO_ROOT / "research" / "keyword_candidates_for_cli.txt"

LOG_DIR = REPO_ROOT / "logs"
TICK_LOG = LOG_DIR / "hermes-closed-loop.log"
STATE_FILE = LOG_DIR / "hermes-closed-loop-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_log(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with TICK_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hermes 闭环一次 tick")
    p.add_argument(
        "--reason",
        default="closed_loop_tick",
        help="本次触发的审计原因，写入 cycle_log.jsonl 和 tick 日志",
    )
    p.add_argument(
        "--force-kind",
        choices=["threshold", "keyword_pool", "prompt"],
        default=None,
        help="强制 tuner 只提某类提案；不填则自由发挥",
    )
    p.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="tuner→auditor 的最大重试轮数",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="即使 baseline_v2 sha256 与上次一致也强制跑（调试用）",
    )
    p.add_argument(
        "--skip-bridge",
        action="store_true",
        help="即使 keyword_pool 被通过，也不覆盖 keyword_candidates_for_cli.txt（dry-run 观察用）",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="stdout 只打一行 JSON 摘要，便于调度器抓",
    )
    return p


def _precheck_ready() -> tuple[bool, str, str]:
    """确认 v2 数据就绪；返回 (ok, reason, baseline_sha256)。"""
    missing: list[str] = []
    if not FEATURES_V2.is_file():
        missing.append(str(FEATURES_V2.relative_to(REPO_ROOT)))
    if not BASELINE_V2.is_file():
        missing.append(str(BASELINE_V2.relative_to(REPO_ROOT)))
    if missing:
        return False, "missing: " + ", ".join(missing), ""
    try:
        sha = _sha256_file(BASELINE_V2)
    except OSError as e:
        return False, f"baseline_v2 unreadable: {e}", ""
    return True, "ok", sha


def _bridge_pool_to_cli(new_pool: list[str]) -> tuple[bool, str]:
    """把 keyword_pool（list[str]）写成一行英文逗号分隔，覆盖 CLI 文件。
    返回 (wrote, note)；only `wrote=True` 说明文件真被改了（含新旧差异的判断）。"""
    clean = [str(x).strip() for x in new_pool]
    clean = [x for x in clean if x]
    if not clean:
        return False, "new_pool empty after normalization"
    line = ",".join(clean)
    existing = ""
    if CLI_KEYWORDS_FILE.is_file():
        try:
            existing = CLI_KEYWORDS_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
    if existing == line:
        return False, "CLI file already identical; no-op"
    CLI_KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLI_KEYWORDS_FILE.write_text(line + "\n", encoding="utf-8")
    try:
        display_path = str(CLI_KEYWORDS_FILE.relative_to(REPO_ROOT))
    except ValueError:
        display_path = str(CLI_KEYWORDS_FILE)
    return True, f"overwrote {display_path} ({len(clean)} kws)"


def _read_active_pool() -> list[str] | None:
    if not KEYWORD_POOL_ACTIVE.is_file():
        return None
    try:
        obj = json.loads(KEYWORD_POOL_ACTIVE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    kw = obj.get("keywords")
    if not isinstance(kw, list):
        return None
    return [str(x) for x in kw]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ts = _now_iso()
    tick_entry: dict[str, Any] = {
        "ts_utc": ts,
        "reason": args.reason,
        "force_kind": args.force_kind,
        "force": bool(args.force),
        "skip_bridge": bool(args.skip_bridge),
    }

    ok, reason, sha = _precheck_ready()
    tick_entry["precheck"] = {"ok": ok, "reason": reason, "baseline_sha256": sha[:16] if sha else ""}
    if not ok:
        tick_entry["outcome"] = "precheck_failed"
        _append_log(tick_entry)
        if not args.quiet:
            print(json.dumps(tick_entry, ensure_ascii=False, indent=2))
        return 2

    state = _load_state()
    last_sha = str(state.get("last_baseline_sha256") or "")
    if not args.force and sha == last_sha:
        tick_entry["outcome"] = "skipped_same_baseline"
        tick_entry["note"] = "baseline_v2.json 与上次 tick 一致；未触发 LLM（加 --force 可强跑）"
        _append_log(tick_entry)
        if not args.quiet:
            print(json.dumps(tick_entry, ensure_ascii=False, indent=2))
        return 0

    # 只在真正要跑的时候才 import hermes —— 避免 precheck 失败时白进 import
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from research.hermes_loop.cycle import trigger_cycle  # type: ignore
    except Exception as e:  # noqa: BLE001
        tick_entry["outcome"] = "import_error"
        tick_entry["error"] = f"{type(e).__name__}: {e}"
        _append_log(tick_entry)
        if not args.quiet:
            print(json.dumps(tick_entry, ensure_ascii=False, indent=2))
        return 3

    try:
        report = trigger_cycle(
            reason=args.reason,
            force_kind=args.force_kind,
            max_rounds=args.max_rounds,
            trigger_crawl_on_keyword_approval=False,
        )
    except Exception as e:  # noqa: BLE001
        tick_entry["outcome"] = "cycle_exception"
        tick_entry["error"] = f"{type(e).__name__}: {e}"
        tick_entry["trace"] = traceback.format_exc(limit=6)
        _append_log(tick_entry)
        if not args.quiet:
            print(json.dumps(tick_entry, ensure_ascii=False, indent=2))
        return 3

    rdict = report.to_dict()
    tick_entry["cycle_id"] = rdict.get("cycle_id")
    tick_entry["sample_count"] = rdict.get("sample_count")
    tick_entry["baseline_auc"] = rdict.get("baseline_auc")
    tick_entry["verdict"] = rdict.get("final_verdict")
    ap = rdict.get("approved_proposal") or {}
    tick_entry["approved_kind"] = ap.get("kind") if ap else None

    bridged = False
    bridge_note = ""
    if (
        rdict.get("final_verdict") == "APPROVED_AND_COMMITTED"
        and ap.get("kind") == "keyword_pool"
        and not args.skip_bridge
    ):
        pool = _read_active_pool()
        if pool is None:
            bridge_note = "cycle approved keyword_pool but keyword_pool_active.json unreadable; skip"
        else:
            bridged, bridge_note = _bridge_pool_to_cli(pool)
    tick_entry["bridged_to_cli_file"] = bridged
    tick_entry["bridge_note"] = bridge_note
    tick_entry["outcome"] = "ran"

    state["last_baseline_sha256"] = sha
    state["last_tick_ts_utc"] = ts
    state["last_verdict"] = rdict.get("final_verdict")
    state["last_bridged"] = bridged
    _save_state(state)
    _append_log(tick_entry)

    if not args.quiet:
        print(json.dumps(tick_entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
