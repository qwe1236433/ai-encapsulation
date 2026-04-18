"""
激活 Hermes 一次工作循环的 CLI。

使用场景：
  - 爬虫脚本跑完一批数据后，最后一行调：
      python scripts/schedule_hermes_cycle.py --reason crawler_batch_done
  - Windows 任务计划 / Linux cron 周期性激活：
      python scripts/schedule_hermes_cycle.py --reason cron_hourly
  - 排查/手动触发：
      python scripts/schedule_hermes_cycle.py --reason manual --force-kind threshold

两种工作模式（互斥，优先级：--via-api > 直连）：
  1. 直连（默认）：直接 import hermes.cycle.trigger_cycle 本地运行。
     适合 Hermes 还没起 API 服务 / 爬虫在同一机器上。
  2. --via-api：POST 到 /api/hermes/cycle。
     适合 Hermes 服务已起、爬虫在另一台机器、或想走 API 审计日志。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="激活 Hermes 一次工作循环。")
    p.add_argument("--reason", default="manual", help="本次触发的原因；审计用")
    p.add_argument(
        "--force-kind",
        choices=["threshold", "keyword_pool", "prompt"],
        default=None,
        help="强制 tuner 只提某类提案；不填则自由发挥",
    )
    p.add_argument("--max-rounds", type=int, default=3, help="tuner→auditor 重试轮数上限（1-5）")
    p.add_argument(
        "--trigger-crawl",
        action="store_true",
        help="通过 keyword_pool 提案后，立即申请启动爬虫（降级模式则写 intent）",
    )
    p.add_argument(
        "--via-api",
        default=None,
        metavar="URL",
        help="走 HTTP 调用，例：http://127.0.0.1:8099 ；不填则本地 import 直连",
    )
    p.add_argument(
        "--quiet", action="store_true", help="只输出 JSON 结果，不打印可读摘要"
    )
    return p


def _print_human(report: dict) -> None:
    print(f"=== CycleReport {report.get('cycle_id')} ===")
    print(f"  ts       = {report.get('ts_utc')}")
    print(f"  reason   = {report.get('reason')}")
    print(f"  verdict  = {report.get('final_verdict')}")
    print(f"  sample_n = {report.get('sample_count')}  baseline_auc = {report.get('baseline_auc'):.4f}")
    ap = report.get("approved_proposal")
    if ap:
        print(f"  approved = [{ap.get('kind')}] {ap.get('target')}")
        print(f"             {ap.get('before')} → {ap.get('after')}")
    print(f"  rounds   = {len(report.get('rounds', []))}")
    effs = report.get("side_effects") or []
    if effs:
        print(f"  side_effects ({len(effs)}):")
        for e in effs:
            print(f"    - {json.dumps(e, ensure_ascii=False)}")


def _call_direct(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from hermes.cycle import trigger_cycle

    report = trigger_cycle(
        reason=args.reason,
        force_kind=args.force_kind,
        max_rounds=args.max_rounds,
        trigger_crawl_on_keyword_approval=args.trigger_crawl,
    )
    return report.to_dict()


def _call_api(args: argparse.Namespace) -> dict:
    url = args.via_api.rstrip("/") + "/api/hermes/cycle"
    body = {
        "reason": args.reason,
        "force_kind": args.force_kind,
        "max_rounds": args.max_rounds,
        "trigger_crawl_on_keyword_approval": args.trigger_crawl,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=300).read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"API 调用失败 HTTP_{e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    return json.loads(r.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = _call_api(args) if args.via_api else _call_direct(args)
    if args.quiet:
        print(json.dumps(report, ensure_ascii=False))
    else:
        _print_human(report)
        print()
        print("--- full JSON ---")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("final_verdict") in ("APPROVED_AND_COMMITTED",) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
