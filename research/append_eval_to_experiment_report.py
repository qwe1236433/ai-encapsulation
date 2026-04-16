"""
将 evaluate_baseline_weights 产出的 JSON 摘要追加到 EXPERIMENT_REPORT.md（可审计、少叙事）。

用法（仓库根）:

  python research/append_eval_to_experiment_report.py \\
    --digest-sha abc123... \\
    --eval research/artifacts/eval_auto_baseline_v0.json \\
    --eval research/artifacts/eval_auto_baseline_v1.json

在 `EXPERIMENT_REPORT.md` 中查找占位符 `<!-- AUTO_EVAL_TAIL -->`（模板已预置）；在其上方插入本次摘要。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MARKER = "<!-- AUTO_EVAL_TAIL -->"


def _fmt_coef(coef: dict[str, float] | None, max_keys: int = 8) -> str:
    if not coef:
        return "—"
    items = sorted(coef.items(), key=lambda x: -abs(x[1]))[:max_keys]
    return ", ".join(f"{k}={v:.3f}" for k, v in items)


def _rel_repo(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _one_eval_block(path: Path, repo: Path) -> str:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    warns = data.get("warnings") or []
    coef_z = data.get("standardized_coefficients_full_sample") or {}
    auc = data.get("artifact_holdout_roc_auc")
    auc_s = f"{float(auc):.4f}" if isinstance(auc, (int, float)) else "—"
    null = data.get("null_permutation_single_run") or {}
    n_h = null.get("n_holdout", "—")
    auc_n = null.get("holdout_roc_auc")
    auc_n_s = f"{float(auc_n):.4f}" if isinstance(auc_n, (int, float)) else "nan"
    lines = [
        f"- **eval**: `{_rel_repo(path, repo)}`",
        f"- **n_samples**: {data.get('n_samples', '—')}; **artifact holdout ROC-AUC**: {auc_s}",
        f"- **warnings**: `{', '.join(warns) if warns else 'none'}`",
        f"- **std coef (top by |coef|)**: {_fmt_coef(coef_z if isinstance(coef_z, dict) else None)}",
        f"- **null perm AUC** (single run, n_holdout={n_h}): {auc_n_s}",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Append eval summary to EXPERIMENT_REPORT.md")
    ap.add_argument("--report", type=str, default="research/EXPERIMENT_REPORT.md")
    ap.add_argument("--digest-sha", type=str, default="", dest="digest_sha")
    ap.add_argument("--eval", type=str, action="append", dest="eval_paths", default=[])
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    report = (repo / args.report).resolve() if not Path(args.report).is_absolute() else Path(args.report)
    if not report.is_file():
        print(f"missing report: {report}", flush=True)
        return 2
    report_text_probe = report.read_text(encoding="utf-8")
    if MARKER not in report_text_probe:
        print(
            f"missing {MARKER} in {report}; add section 七 + marker per template",
            flush=True,
        )
        return 3

    paths = [Path(p).expanduser().resolve() for p in (args.eval_paths or [])]
    for p in paths:
        if not p.is_file():
            print(f"missing eval json: {p}", flush=True)
            return 2

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    digest = (args.digest_sha or "").strip()
    if len(digest) >= 16:
        digest_line = f"- **feed digest sha256**: `{digest[:16]}…`（全长 {len(digest)} hex）"
    elif digest:
        digest_line = f"- **feed digest sha256**: `{digest}`"
    else:
        digest_line = ""

    body_parts = [
        f"### AUTO-EVAL {now}",
    ]
    if digest_line:
        body_parts.append(digest_line)
    for p in paths:
        body_parts.append(_one_eval_block(p, repo))
    body_parts.append("")  # blank before marker
    block = "\n".join(body_parts) + "\n"

    text = report_text_probe
    text = text.replace(MARKER, block + "\n" + MARKER, 1)

    report.write_text(text, encoding="utf-8")
    print(f"Appended to {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
