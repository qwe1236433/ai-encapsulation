"""A→B→C→D→A 全循环长路径 smoke probe（真 LLM 管道）。

目标
----
验证"爬取 → 分析 → Hermes LLM 调参 → 桥到 CLI → 窗 A 接应"全链路。

由于本项目的 C→D→Hermes LLM 审核链路今天早上已经在真 LLM 下跑通过
(cycle_id=fc3ba13595ec, final_verdict=APPROVED_AND_COMMITTED, keyword_pool_activated)，
本 probe 的重点是：
  1. 连续触发 Hermes tick，直到拿到 1 个 APPROVED_AND_COMMITTED 的 cycle
  2. 验证该 cycle 的 side_effect 真把 keyword_pool_active.json 更新了
  3. 验证 bridge 真把 keyword 写进了 keyword_candidates_for_cli.txt（CLI SHA 变化）
  4. [可选] 验证窗 A 接应：日志出现 'Keywords or MediaCrawler config changed'
     或 'cooldown active'（说明窗 A 看到了 SHA 变化并主动保号屏蔽 kill）

说明
----
- Hermes 的 LLM 审核是概率性的。本 probe 接受 "连 N 次 tick 都 NO_PROPOSAL/
  REJECTED" 这种结果（标为 NO_APPLY_IN_WINDOW，退出码 3），不算 FAIL。
- 默认 `--force-kind keyword_pool` 强制 tuner 只提关键词池建议，提高命中率。
- 本 probe **不自动启动窗 A**。窗 A 有真实风控代价，由用户自己控制。
  probe 会用 --watch-log 检查窗 A 是否在跑，若在，纳入验证；若不在，跳过。
- 整套 probe 不改动任何真数据（仅追加写 cycle_log.jsonl 和 keyword_pool_active.json，
  这些本就是 Hermes 每次 tick 的正常产物；若你想先看不写 CLI 的 dry-run，用
  `--skip-bridge` 透传给 tick 脚本）。

退出码
------
  0  ALL OK（完整链路验证通过）
  3  NO_APPLY_IN_WINDOW（所有 tick 都未 APPROVED，Hermes 设计上的正常结果）
  4  BRIDGE_FAIL（APPROVED 但 CLI 文件没改）
  5  WATCH_FAIL（窗 A 在跑但未观察到 SHA-change / cooldown 响应）
  1  静态前置 / 运行时错误

Examples
--------
  # 最小跑：只验证 C→D→bridge 段（不需要窗 A 在跑）
  python scripts/_probe_long_loop.py --max-ticks 6 --tick-gap-sec 30

  # 完整跑：窗 A 已在跑（cooldown=0，允许 restart）
  python scripts/_probe_long_loop.py --max-ticks 6 --tick-gap-sec 30 --expect-watch-a

  # 稳健档跑：窗 A cooldown=60 min，probe 把 'cooldown active' 视为 PASS
  python scripts/_probe_long_loop.py --max-ticks 6 --tick-gap-sec 30 --expect-watch-a \\
      --cooldown-mode
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "research" / "artifacts"
CYCLE_LOG_JSONL = ARTIFACTS_DIR / "cycle_log.jsonl"
KEYWORD_POOL_ACTIVE = ARTIFACTS_DIR / "keyword_pool_active.json"
CLI_KEYWORDS_FILE = REPO_ROOT / "research" / "keyword_candidates_for_cli.txt"
FEATURES_V2 = REPO_ROOT / "research" / "features_v2.csv"
BASELINE_V2 = ARTIFACTS_DIR / "baseline_v2.json"
WATCH_LOG_FILE = REPO_ROOT / "logs" / "mediacrawler-watch.log"
TICK_SCRIPT = REPO_ROOT / "scripts" / "hermes_closed_loop_tick.py"


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ProbeContext:
    cycle_log_line_count_before: int = 0
    cli_sha_before: str = ""
    cli_size_before: int = 0
    pool_mtime_before: float = 0.0
    pool_sha_before: str = ""
    watch_log_size_before: int = 0
    watch_log_mtime_before: float = 0.0
    tick_results: list[dict[str, Any]] = field(default_factory=list)
    approved_cycle: dict[str, Any] | None = None


def _sha256_file(p: Path) -> str:
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_jsonl_lines(p: Path) -> int:
    if not p.is_file():
        return 0
    n = 0
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for _ in f:
            n += 1
    return n


def _tail_jsonl(p: Path, start_from_line: int) -> list[dict[str, Any]]:
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx < start_from_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def phase0_preflight() -> list[StepResult]:
    out: list[StepResult] = []
    out.append(StepResult(
        "P0_features_v2",
        FEATURES_V2.is_file() and FEATURES_V2.stat().st_size > 0,
        f"{FEATURES_V2} size={FEATURES_V2.stat().st_size if FEATURES_V2.is_file() else 0}",
    ))
    out.append(StepResult(
        "P0_baseline_v2",
        BASELINE_V2.is_file() and BASELINE_V2.stat().st_size > 0,
        f"{BASELINE_V2}",
    ))
    out.append(StepResult(
        "P0_tick_script",
        TICK_SCRIPT.is_file(),
        f"{TICK_SCRIPT}",
    ))
    out.append(StepResult(
        "P0_cli_file",
        CLI_KEYWORDS_FILE.is_file(),
        f"{CLI_KEYWORDS_FILE}",
    ))
    return out


def phase1_snapshot(ctx: ProbeContext) -> StepResult:
    try:
        ctx.cycle_log_line_count_before = _count_jsonl_lines(CYCLE_LOG_JSONL)
        ctx.cli_sha_before = _sha256_file(CLI_KEYWORDS_FILE)
        ctx.cli_size_before = CLI_KEYWORDS_FILE.stat().st_size if CLI_KEYWORDS_FILE.is_file() else 0
        if KEYWORD_POOL_ACTIVE.is_file():
            ctx.pool_mtime_before = KEYWORD_POOL_ACTIVE.stat().st_mtime
            ctx.pool_sha_before = _sha256_file(KEYWORD_POOL_ACTIVE)
        if WATCH_LOG_FILE.is_file():
            st = WATCH_LOG_FILE.stat()
            ctx.watch_log_size_before = st.st_size
            ctx.watch_log_mtime_before = st.st_mtime
    except OSError as e:
        return StepResult("P1_snapshot", False, f"snapshot failed: {e}")
    return StepResult(
        "P1_snapshot", True,
        f"cycle_log_lines={ctx.cycle_log_line_count_before} "
        f"cli_sha_prefix={ctx.cli_sha_before[:16]} "
        f"pool_sha_prefix={ctx.pool_sha_before[:16]} "
        f"watch_log_size={ctx.watch_log_size_before}",
    )


def _run_one_tick(force_kind: str | None, skip_bridge: bool, tick_timeout_sec: int) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(TICK_SCRIPT),
        "--reason", "probe_long_loop",
        "--force",
    ]
    if force_kind:
        cmd += ["--force-kind", force_kind]
    if skip_bridge:
        cmd += ["--skip-bridge"]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=tick_timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"tick timeout after {tick_timeout_sec}s", "raw_stdout": (e.stdout or "")[:500], "elapsed_sec": tick_timeout_sec}

    elapsed = time.time() - t0
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    parsed: dict[str, Any] | None = None
    stdout_lines = stdout.splitlines()
    for start_idx in range(len(stdout_lines)):
        block = "\n".join(stdout_lines[start_idx:]).strip()
        if not block.startswith("{"):
            continue
        try:
            candidate = json.loads(block)
            if isinstance(candidate, dict):
                parsed = candidate
                break
        except json.JSONDecodeError:
            continue

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed_sec": round(elapsed, 2),
        "parsed": parsed,
        "raw_stdout_tail": stdout[-400:],
        "raw_stderr_tail": stderr[-400:],
    }


def phase2_tick_loop(
    ctx: ProbeContext,
    max_ticks: int,
    tick_gap_sec: int,
    force_kind: str | None,
    skip_bridge: bool,
    tick_timeout_sec: int,
) -> StepResult:
    approved = None
    summaries: list[str] = []
    for i in range(1, max_ticks + 1):
        print(f"  [tick {i}/{max_ticks}] running hermes_closed_loop_tick.py ...", flush=True)
        res = _run_one_tick(force_kind, skip_bridge, tick_timeout_sec)
        ctx.tick_results.append(res)

        parsed = res.get("parsed") or {}
        outcome = parsed.get("outcome")
        verdict = parsed.get("verdict")
        cycle_id = parsed.get("cycle_id")
        summary = f"tick{i}: rc={res.get('returncode')} elapsed={res.get('elapsed_sec')}s outcome={outcome} verdict={verdict} cycle={cycle_id}"
        summaries.append(summary)
        print(f"    {summary}", flush=True)

        if verdict == "APPROVED_AND_COMMITTED":
            approved = parsed
            break

        if i < max_ticks:
            print(f"    (等 {tick_gap_sec}s 再跑下一 tick)", flush=True)
            time.sleep(tick_gap_sec)

    if approved:
        ctx.approved_cycle = approved
        return StepResult(
            "P2_tick_loop", True,
            f"ok, 第 {len(ctx.tick_results)} 个 tick 拿到 APPROVED (cycle_id={approved.get('cycle_id')})",
        )
    return StepResult(
        "P2_tick_loop", False,
        f"连跑 {max_ticks} 次 tick 未见 APPROVED_AND_COMMITTED\n    " + "\n    ".join(summaries),
    )


def phase3_verify_pool(ctx: ProbeContext) -> StepResult:
    if not KEYWORD_POOL_ACTIVE.is_file():
        return StepResult("P3_keyword_pool_activated", False, "keyword_pool_active.json 不存在")
    st = KEYWORD_POOL_ACTIVE.stat()
    sha_after = _sha256_file(KEYWORD_POOL_ACTIVE)
    mtime_changed = st.st_mtime > ctx.pool_mtime_before + 0.001
    sha_changed = sha_after != ctx.pool_sha_before
    if not (mtime_changed or sha_changed):
        return StepResult(
            "P3_keyword_pool_activated", False,
            f"keyword_pool_active.json 未更新（mtime/sha 都没变化）",
        )
    try:
        obj = json.loads(KEYWORD_POOL_ACTIVE.read_text(encoding="utf-8"))
        pool_size = len(obj.get("keywords", []))
    except (OSError, json.JSONDecodeError):
        pool_size = -1
    return StepResult(
        "P3_keyword_pool_activated", True,
        f"ok, pool updated (size={pool_size}, sha_prefix={sha_after[:16]})",
    )


def phase4_verify_cli_bridge(ctx: ProbeContext, skip_bridge: bool) -> StepResult:
    if skip_bridge:
        return StepResult(
            "P4_cli_bridge", True,
            "skipped (--skip-bridge 模式下 bridge 故意不跑，视作 PASS)",
        )
    sha_after = _sha256_file(CLI_KEYWORDS_FILE)
    size_after = CLI_KEYWORDS_FILE.stat().st_size if CLI_KEYWORDS_FILE.is_file() else 0
    if sha_after == ctx.cli_sha_before:
        return StepResult(
            "P4_cli_bridge", False,
            f"CLI 文件 SHA 未变化（期望：approved cycle 的 bridge 应把 pool 写进 CLI）\n"
            f"    sha_before={ctx.cli_sha_before[:16]} sha_after={sha_after[:16]}",
        )
    return StepResult(
        "P4_cli_bridge", True,
        f"ok, CLI 文件已被 bridge 覆盖 "
        f"(size {ctx.cli_size_before} -> {size_after}, sha {ctx.cli_sha_before[:12]} -> {sha_after[:12]})",
    )


def phase5_verify_watch_a(
    ctx: ProbeContext,
    expect_watch: bool,
    cooldown_mode: bool,
    wait_sec: int,
) -> StepResult:
    if not expect_watch:
        return StepResult(
            "P5_watch_a_response", True,
            "skipped (未指定 --expect-watch-a；若你现在正运行窗 A，下次加此开关即可纳入验证)",
        )
    if not WATCH_LOG_FILE.is_file():
        return StepResult(
            "P5_watch_a_response", False,
            f"watch.log 不存在：{WATCH_LOG_FILE}（窗 A 没跑起来过）",
        )

    pattern_change = re.compile(r"Keywords or MediaCrawler config changed")
    pattern_cooldown = re.compile(r"cooldown active.*account-safety")

    deadline = time.time() + wait_sec
    saw_change = False
    saw_cooldown = False
    first_match = ""

    while time.time() < deadline:
        try:
            with WATCH_LOG_FILE.open("rb") as f:
                f.seek(ctx.watch_log_size_before)
                new_chunk = f.read().decode("utf-8", errors="replace")
            for line in new_chunk.splitlines():
                if pattern_change.search(line) and not saw_change:
                    saw_change = True
                    if not first_match:
                        first_match = line
                if pattern_cooldown.search(line) and not saw_cooldown:
                    saw_cooldown = True
                    if not first_match:
                        first_match = line
            if cooldown_mode:
                if saw_change or saw_cooldown:
                    break
            else:
                if saw_change:
                    break
        except OSError:
            pass
        time.sleep(2.0)

    elapsed = int(time.time() - (deadline - wait_sec))
    if cooldown_mode and saw_cooldown:
        return StepResult(
            "P5_watch_a_response", True,
            f"ok (cooldown-mode), {elapsed}s 内窗 A 看到 CLI 变化并主动屏蔽 restart（保号闸生效）: "
            f"{first_match.strip()[:220]}",
        )
    if saw_change:
        return StepResult(
            "P5_watch_a_response", True,
            f"ok, {elapsed}s 内窗 A 检测到 SHA 变化并触发 restart: {first_match.strip()[:220]}",
        )
    if cooldown_mode:
        return StepResult(
            "P5_watch_a_response", False,
            f"{wait_sec}s 超时，未在窗 A 日志观察到 'Keywords or MediaCrawler config changed' "
            f"也未见 'cooldown active'。窗 A 可能没跑，或尚未 poll 到变化。",
        )
    return StepResult(
        "P5_watch_a_response", False,
        f"{wait_sec}s 超时，未在窗 A 日志观察到 'Keywords or MediaCrawler config changed'。"
        f"若你的窗 A 带了 cooldown>0 参数，请加 --cooldown-mode 再跑 probe。",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A→B→C→D→A 长路径闭环 smoke probe")
    p.add_argument("--max-ticks", type=int, default=6, help="最多跑几次 Hermes tick 才罢休")
    p.add_argument("--tick-gap-sec", type=int, default=30, help="两次 tick 之间的间隔秒数")
    p.add_argument("--tick-timeout-sec", type=int, default=120, help="单次 tick 子进程超时秒数（含 LLM 调用时间）")
    p.add_argument("--force-kind", type=str, default="keyword_pool",
                   choices=["threshold", "keyword_pool", "prompt", "none"],
                   help="强制 tuner 只提某类提案（默认 keyword_pool 提高命中；none=让 tuner 自由发挥）")
    p.add_argument("--skip-bridge", action="store_true",
                   help="透传给 tick 脚本；dry-run 模式下即使 APPROVED 也不写 CLI，P4 自动视作 PASS")
    p.add_argument("--expect-watch-a", action="store_true",
                   help="窗 A 应该在跑，纳入 P5 验证（默认跳过 P5）")
    p.add_argument("--cooldown-mode", action="store_true",
                   help="搭配 --expect-watch-a；认为窗 A 带了 cooldown>0，'cooldown active' 日志视为 P5 PASS")
    p.add_argument("--watch-wait-sec", type=int, default=60,
                   help="P5 中等待窗 A 响应的超时秒数（默认 60；含 debounce 4s）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    print(f"=== long-loop probe @ {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===")
    print(f"  REPO_ROOT={REPO_ROOT}")
    print(f"  max_ticks={args.max_ticks} tick_gap_sec={args.tick_gap_sec} force_kind={args.force_kind}")
    print(f"  expect_watch_a={args.expect_watch_a} cooldown_mode={args.cooldown_mode}")
    print()

    results: list[StepResult] = []

    print("[phase 0 preflight]")
    p0 = phase0_preflight()
    for r in p0:
        mark = "PASS" if r.ok else "FAIL"
        print(f"  [{mark}] {r.name}: {r.detail}")
    results.extend(p0)
    if not all(r.ok for r in p0):
        print("\n=== preflight FAIL, abort ===")
        return 1

    ctx = ProbeContext()

    print("\n[phase 1 snapshot]")
    r1 = phase1_snapshot(ctx)
    print(f"  [{'PASS' if r1.ok else 'FAIL'}] {r1.name}: {r1.detail}")
    results.append(r1)
    if not r1.ok:
        return 1

    force_kind = None if args.force_kind == "none" else args.force_kind

    print(f"\n[phase 2 tick-loop] 最多 {args.max_ticks} 次 tick，每次超时 {args.tick_timeout_sec}s")
    r2 = phase2_tick_loop(
        ctx,
        max_ticks=args.max_ticks,
        tick_gap_sec=args.tick_gap_sec,
        force_kind=force_kind,
        skip_bridge=args.skip_bridge,
        tick_timeout_sec=args.tick_timeout_sec,
    )
    print(f"  [{'PASS' if r2.ok else 'FAIL'}] {r2.name}: {r2.detail}")
    results.append(r2)
    if not r2.ok:
        print("\n=== NO_APPLY_IN_WINDOW: Hermes 本轮未产生 APPROVED_AND_COMMITTED ===")
        print("(这不算 bug，是 LLM/审核门禁的正常结果；可加大 --max-ticks 或改 --force-kind 再跑)")
        return 3

    print("\n[phase 3 keyword_pool_active.json]")
    r3 = phase3_verify_pool(ctx)
    print(f"  [{'PASS' if r3.ok else 'FAIL'}] {r3.name}: {r3.detail}")
    results.append(r3)

    print("\n[phase 4 CLI bridge]")
    r4 = phase4_verify_cli_bridge(ctx, skip_bridge=args.skip_bridge)
    print(f"  [{'PASS' if r4.ok else 'FAIL'}] {r4.name}: {r4.detail}")
    results.append(r4)

    if not r4.ok and not args.skip_bridge:
        print("\n=== BRIDGE_FAIL: approved 了但 CLI 文件没变，检查 _bridge_pool_to_cli ===")
        return 4

    print(f"\n[phase 5 window-A response] expect={args.expect_watch_a} cooldown_mode={args.cooldown_mode}")
    r5 = phase5_verify_watch_a(
        ctx,
        expect_watch=args.expect_watch_a,
        cooldown_mode=args.cooldown_mode,
        wait_sec=args.watch_wait_sec,
    )
    print(f"  [{'PASS' if r5.ok else 'FAIL'}] {r5.name}: {r5.detail}")
    results.append(r5)

    all_ok = all(r.ok for r in results)
    print()
    if all_ok:
        print("=== ALL OK (long-loop A→B→C→D→A verified) ===")
        return 0
    if not r5.ok:
        print("=== WATCH_FAIL: C→D→bridge 完整 OK，但窗 A 未响应 ===")
        return 5
    return 1


if __name__ == "__main__":
    sys.exit(main())
