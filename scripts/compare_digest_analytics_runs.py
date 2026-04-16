"""
对比 research/analytics_history/run_* 下最近若干次数分数分快照，并可选并入当前 research/artifacts 结果。

产出 Markdown报告（默认 research/runtime/digest_comparison_report.md），便于写结论时看 AUC/系数是否同向。

用法（仓库根）:

  python scripts/compare_digest_analytics_runs.py --last 3
  python scripts/compare_digest_analytics_runs.py --last 3 --include-current
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _top_coefs(coef_map: dict[str, Any], k: int = 3) -> str:
    if not coef_map:
        return ""
    items: list[tuple[str, float]] = []
    for name, v in coef_map.items():
        try:
            items.append((str(name), float(v)))
        except (TypeError, ValueError):
            continue
    items.sort(key=lambda x: abs(x[1]), reverse=True)
    parts = [f"{n}={x:+.3f}" for n, x in items[:k]]
    return "; ".join(parts)


def _summarize_eval(path: Path) -> dict[str, Any]:
    j = _load_json(path) or {}
    th = j.get("time_ordered_holdout") or {}
    time_auc = None
    if isinstance(th, dict) and not th.get("skipped"):
        ta = th.get("roc_auc")
        if isinstance(ta, (int, float)):
            time_auc = float(ta)
    coefs = j.get("standardized_coefficients_full_sample") or {}
    if not isinstance(coefs, dict):
        coefs = {}
    return {
        "eval_artifact_auc": j.get("artifact_holdout_roc_auc"),
        "eval_time_auc": time_auc,
        "n_samples": j.get("n_samples"),
        "warnings": j.get("warnings") if isinstance(j.get("warnings"), list) else [],
        "coef_top": _top_coefs(coefs, 3),
    }


def _summarize_train(path: Path) -> dict[str, Any]:
    j = _load_json(path) or {}
    return {
        "train_holdout_auc": j.get("holdout_roc_auc"),
        "n_samples": j.get("n_samples"),
        "target_column": j.get("target_column"),
    }


def _run_dirs(history: Path) -> list[Path]:
    if not history.is_dir():
        return []
    runs = [p for p in history.iterdir() if p.is_dir() and p.name.startswith("run_")]
    runs.sort(key=lambda p: p.name, reverse=True)
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare last N analytics_history snapshots (+ optional current artifacts)")
    ap.add_argument("--repo-root", type=str, default="")
    ap.add_argument("--history-dir", type=str, default="research/analytics_history")
    ap.add_argument("--last", type=int, default=3, help="最近 N 个 run_* 目录")
    ap.add_argument("--include-current", action="store_true", help="在表首追加当前 research/artifacts 一行（若存在）")
    ap.add_argument("--out", type=str, default="research/runtime/digest_comparison_report.md")
    args = ap.parse_args()

    root = Path(args.repo_root).expanduser().resolve() if (args.repo_root or "").strip() else Path.cwd().resolve()
    hist = (root / args.history_dir).resolve()
    out_path = (root / args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    runs = _run_dirs(hist)[: max(1, int(args.last))]

    rows: list[dict[str, Any]] = []

    if args.include_current:
        ev1 = root / "research" / "artifacts" / "eval_auto_baseline_v1.json"
        tr1 = root / "research" / "artifacts" / "auto_baseline_v1.json"
        prov = root / "research" / "runtime" / "features_export_provenance.json"
        pv = _load_json(prov) if prov.is_file() else {}
        se = _summarize_eval(ev1)
        st = _summarize_train(tr1)
        rows.append(
            {
                "label": "**当前工作区**（非历史快照）",
                "digest_short": (str(pv.get("feed_digest_sha256") or "")[:12] or "—"),
                "batch": str(pv.get("batch_id_resolved") or "—"),
                "feature_rows": pv.get("feature_row_count"),
                "train_auc": st.get("train_holdout_auc"),
                "eval_artifact_auc": se.get("eval_artifact_auc"),
                "eval_time_auc": se.get("eval_time_auc"),
                "n_eval": se.get("n_samples"),
                "warnings_n": len(se.get("warnings") or []),
                "coef_top": se.get("coef_top") or "",
            }
        )

    for rd in runs:
        man = _load_json(rd / "manifest.json") or {}
        digest = str(man.get("digest_sha256") or "")
        ev1 = rd / "eval_auto_baseline_v1.json"
        tr1 = rd / "auto_baseline_v1.json"
        se = _summarize_eval(ev1)
        st = _summarize_train(tr1)
        rows.append(
            {
                "label": rd.name,
                "digest_short": digest[:12] if digest else "—",
                "batch": "—",
                "feature_rows": man.get("feature_rows_excl_header"),
                "train_auc": st.get("train_holdout_auc"),
                "eval_artifact_auc": se.get("eval_artifact_auc"),
                "eval_time_auc": se.get("eval_time_auc"),
                "n_eval": se.get("n_samples"),
                "warnings_n": len(se.get("warnings") or []),
                "coef_top": se.get("coef_top") or "",
            }
        )

    lines: list[str] = []
    lines.append("# Digest / 数分快照对比（近 N 次）")
    lines.append("")
    lines.append(f"- 生成 UTC: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 历史目录: `{hist}`（共扫描到 {len(_run_dirs(hist))} 个 run_*，表中取最近 {len(runs)} 个）")
    lines.append("- **结论写法**：看 **train_holdout_auc / eval 中 artifact AUC** 与 **time_ordered AUC** 是否同向；单次绝对值噪声大，以趋势为主。")
    lines.append("")
    if len(rows) < 2 and not runs:
        lines.append("> 尚无 `run_*` 历史目录；请先跑一键脚本或 `continuous-xhs-analytics` 成功归档，或使用 `--include-current`只看当前 artifact。")
        lines.append("")
    lines.append("| 来源 | digest(12) | feature_rows | train_AUC | eval_artifact_AUC | time_hold_AUC | n_eval | warnings | coef_top(标准化)|")
    lines.append("|------|------------|--------------|-----------|-------------------|---------------|--------|----------|----------------|")
    for r in rows:
        def _cell(x: Any) -> str:
            if x is None:
                return "—"
            if isinstance(x, float):
                return f"{x:.4f}"
            return str(x)

        lines.append(
            "| {label} | {d} | {fr} | {ta} | {ea} | {tt} | {ne} | {wn} | {coef} |".format(
                label=r["label"].replace("|", "\\|"),
                d=r["digest_short"],
                fr=_cell(r.get("feature_rows")),
                ta=_cell(r.get("train_auc")),
                ea=_cell(r.get("eval_artifact_auc")),
                tt=_cell(r.get("eval_time_auc")),
                ne=_cell(r.get("n_eval")),
                wn=r.get("warnings_n"),
                coef=(r.get("coef_top") or "—")[:80],
            )
        )
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- `train_AUC` 来自 `auto_baseline_v1.json` 的 `holdout_roc_auc`（随机分层 hold-out）。")
    lines.append("- `eval_artifact_AUC` / `time_hold_AUC` 来自 `evaluate_baseline_weights` 产出（后者依赖评估时是否开启时间序 hold-out）。")
    lines.append("- 历史行来自 `analytics_history/run_*/`；当前行来自 `--include-current`。")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
