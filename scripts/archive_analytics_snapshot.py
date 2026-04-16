"""
将当前 research 产出复制到 research/analytics_history/run_*，与 continuous-xhs-analytics 快照结构一致，供 compare_digest_analytics_runs 使用。

用法（仓库根）:

  python scripts/archive_analytics_snapshot.py --digest openclaw/data/xhs-feed/samples.digest.json
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_digest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Archive current analytics outputs to analytics_history/run_*")
    ap.add_argument("--repo-root", type=str, default="")
    ap.add_argument("--digest", type=str, default="openclaw/data/xhs-feed/samples.digest.json")
    ap.add_argument("--digest-generation", type=int, default=0, help="写入 manifest；与 continuous 代数不一致时可手改 manifest或传 digest 内扩展字段")
    ap.add_argument("--feature-rows", type=int, default=-1, help="特征行数；-1 则从 features_v0.csv 数行")
    args = ap.parse_args()

    root = Path(args.repo_root).expanduser().resolve() if (args.repo_root or "").strip() else Path.cwd().resolve()
    dig_path = (root / args.digest).expanduser().resolve()
    if not dig_path.is_file():
        print(f"找不到 digest: {dig_path}", flush=True)
        return 2
    dig = _load_digest(dig_path)
    sha = str(dig.get("sha256") or "")
    short = sha[:12] if len(sha) >= 12 else sha or "nodigest"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    gen = int(args.digest_generation)
    dir_name = f"run_{stamp}_g{gen}_{short}"
    dest = (root / "research" / "analytics_history" / dir_name).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    copy_pairs: list[tuple[Path, str]] = [
        (root / "research" / "keyword_candidates.json", "keyword_candidates.json"),
        (root / "research" / "keyword_candidates.txt", "keyword_candidates.txt"),
        (root / "research" / "keyword_candidates_for_cli.txt", "keyword_candidates_for_cli.txt"),
        (root / "research" / "features_v0.csv", "features_v0.csv"),
        (root / "research" / "artifacts" / "auto_baseline_v0.json", "auto_baseline_v0.json"),
        (root / "research" / "artifacts" / "auto_baseline_v1.json", "auto_baseline_v1.json"),
        (root / "research" / "artifacts" / "eval_auto_baseline_v0.json", "eval_auto_baseline_v0.json"),
        (root / "research" / "artifacts" / "eval_auto_baseline_v1.json", "eval_auto_baseline_v1.json"),
        (root / "research" / "runtime" / "factory_baseline.env", "factory_baseline.env"),
        (root / "research" / "runtime" / "mediacrawler_base_config.json", "mediacrawler_base_config.json"),
        (root / "research" / "runtime" / "mediacrawler_eval_patch_rules.json", "mediacrawler_eval_patch_rules.json"),
        (root / "research" / "runtime" / "feed_quality_metrics.json", "feed_quality_metrics.json"),
        (root / "research" / "runtime" / "features_export_provenance.json", "features_export_provenance.json"),
        (root / "research" / "runtime" / "feed_ingest_health.json", "feed_ingest_health.json"),
    ]
    for src, name in copy_pairs:
        if src.is_file():
            shutil.copy2(src, dest / name)
    shutil.copy2(dig_path, dest / "samples.digest.json")

    feat_csv = root / "research" / "features_v0.csv"
    feat_rows = int(args.feature_rows)
    if feat_rows < 0 and feat_csv.is_file():
        n = sum(1 for _ in feat_csv.open(encoding="utf-8", newline="")) - 1
        feat_rows = max(0, n)

    manifest: dict[str, Any] = {
        "schema": "analytics_snapshot_v1",
        "saved_at_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "digest_sha256": sha,
        "digest_generation": gen,
        "analytics_every_n_digests": None,
        "top_keywords_param": None,
        "feature_rows_excl_header": feat_rows,
        "steps": {
            "export_features": True,
            "verify_spec": True,
            "suggest_keywords": True,
            "train_v0": (root / "research" / "artifacts" / "auto_baseline_v0.json").is_file(),
            "train_v1": (root / "research" / "artifacts" / "auto_baseline_v1.json").is_file(),
            "eval_v0": (root / "research" / "artifacts" / "eval_auto_baseline_v0.json").is_file(),
            "eval_v1": (root / "research" / "artifacts" / "eval_auto_baseline_v1.json").is_file(),
        },
        "note": "Archived by scripts/archive_analytics_snapshot.py (one-click or manual).",
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Archived -> {dest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
