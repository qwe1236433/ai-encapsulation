"""
从 features_v0.csv 生成轻量描述统计 JSON（批次/时间/互动/标签覆盖率），供流水线与人工审阅。

用法（仓库根）:

  python scripts/compute_feed_metrics_v0.py --features research/features_v0.csv
  python scripts/compute_feed_metrics_v0.py --features ... --out research/runtime/feed_quality_metrics.json
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_published_ok(s: str) -> bool:
    s = (s or "").strip()
    if not s or s.lower() == "nan":
        return False
    try:
        s_iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        datetime.fromisoformat(s_iso)
        return True
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute descriptive feed metrics from features CSV")
    ap.add_argument("--features", type=str, default="research/features_v0.csv")
    ap.add_argument("--out", type=str, default="research/runtime/feed_quality_metrics.json")
    args = ap.parse_args()

    feat_path = Path(args.features).expanduser().resolve()
    if not feat_path.is_file():
        print(f"找不到特征文件: {feat_path}", flush=True)
        return 2

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_rows = 0
    n_pub_nonempty = 0
    n_pub_parse_ok = 0
    likes: list[int] = []
    y_vals: list[int] = []
    y_alt_vals: list[int] = []
    batch_ids: set[str] = set()
    digests: set[str] = set()

    with feat_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("CSV 无表头", flush=True)
            return 2
        hdr = reader.fieldnames
        has_pub = "published_at" in hdr
        has_like = "like_proxy" in hdr
        has_y = "y_rule" in hdr
        has_y_alt = "y_rule_alt" in hdr
        has_batch = "batch_id" in hdr
        has_digest = "feed_digest_sha256" in hdr
        for row in reader:
            n_rows += 1
            if has_pub:
                pa = str(row.get("published_at", "") or "")
                if pa.strip():
                    n_pub_nonempty += 1
                    if _parse_published_ok(pa):
                        n_pub_parse_ok += 1
            if has_like:
                lk = _to_int(str(row.get("like_proxy", "") or ""))
                if lk is not None:
                    likes.append(max(0, lk))
            if has_y:
                yv = _to_int(str(row.get("y_rule", "") or ""))
                if yv is not None and yv in (0, 1):
                    y_vals.append(yv)
            if has_y_alt:
                ya = _to_int(str(row.get("y_rule_alt", "") or ""))
                if ya is not None and ya in (0, 1):
                    y_alt_vals.append(ya)
            if has_batch:
                b = str(row.get("batch_id", "") or "").strip()
                if b:
                    batch_ids.add(b)
            if has_digest:
                d = str(row.get("feed_digest_sha256", "") or "").strip()
                if d:
                    digests.add(d)

    def _like_summary(xs: list[int]) -> dict[str, Any]:
        if not xs:
            return {"count": 0}
        xs_sorted = sorted(xs)
        mid = xs_sorted[len(xs_sorted) // 2]
        return {
            "count": len(xs),
            "min": xs_sorted[0],
            "max": xs_sorted[-1],
            "median": mid,
            "zeros": sum(1 for v in xs if v == 0),
        }

    def _label_rate(vals: list[int]) -> dict[str, Any]:
        if not vals:
            return {"labeled_rows": 0, "positive_rate": None}
        return {
            "labeled_rows": len(vals),
            "positive_rate": round(sum(vals) / len(vals), 6),
        }

    payload: dict[str, Any] = {
        "schema": "feed_quality_metrics_v1",
        "generated_at_utc": _utc_now_iso(),
        "features_path": str(feat_path),
        "n_rows": n_rows,
        "columns_present": {
            "published_at": has_pub,
            "like_proxy": has_like,
            "y_rule": has_y,
            "y_rule_alt": has_y_alt,
            "batch_id": has_batch,
            "feed_digest_sha256": has_digest,
        },
        "published_at": {
            "nonempty_count": n_pub_nonempty,
            "iso_parse_ok_count": n_pub_parse_ok,
            "nonempty_fraction": round(n_pub_nonempty / n_rows, 6) if n_rows else None,
            "parse_ok_fraction_of_rows": round(n_pub_parse_ok / n_rows, 6) if n_rows else None,
        },
        "like_proxy": _like_summary(likes),
        "y_rule": _label_rate(y_vals),
        "y_rule_alt": _label_rate(y_alt_vals),
        "batch_id_distinct_non_empty": len(batch_ids),
        "feed_digest_sha256_distinct_non_empty": len(digests),
    }

    warnings: list[str] = []
    if n_rows > 0:
        if len(batch_ids) <= 1:
            warnings.append(
                "single_or_no_batch_id_snapshot: 对外勿写跨批次泛化；结论须写 digest/batch 范围"
            )
        if len(digests) <= 1:
            warnings.append(
                "single_or_no_feed_digest_snapshot: 多 digest 对照前勿当全站结论"
            )
        if likes:
            n100 = sum(1 for v in likes if v == 100)
            f100 = n100 / len(likes)
            if f100 >= 0.35:
                warnings.append(
                    "high_fraction_like_proxy_is_100: 可能含合并默认值或真实低赞混杂，建议抽检 jsonl liked_count 与「万」解析"
                )
        if has_pub and n_rows and (n_pub_parse_ok / n_rows) < 0.5:
            warnings.append(
                "low_published_at_parse_rate: 时间序验证与稿龄特征可信度低"
            )
    payload["warnings"] = warnings

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    if warnings:
        print(f"warnings: {'; '.join(warnings)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
