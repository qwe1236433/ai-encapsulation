"""
合并后 Feed（归一条目 list[dict]）的健康度指标与可配置门禁。

供 export_to_xhs_feed.py 在写出 samples.json 前调用；指标落盘便于与训练侧 feed_quality_metrics 对照。

门禁 JSON 示例见 scripts/feed_ingest_health.example.json。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_from_published_cell(s: Any) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, float) and math.isnan(s):
        return None
    s = str(s).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        s_iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def thresholds_from_labels_spec(raw: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not raw or not isinstance(raw, dict):
        return None, None
    t_main: int | None = None
    for k in ("viral_like_threshold", "viral_threshold"):
        v = raw.get(k)
        if v is None:
            continue
        try:
            t_main = int(v)
            break
        except (TypeError, ValueError):
            continue
    t_alt: int | None = None
    v2 = raw.get("viral_like_threshold_alt")
    if v2 is not None:
        try:
            t_alt = int(v2)
        except (TypeError, ValueError):
            t_alt = None
    return t_main, t_alt


def load_labels_spec(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_health_gates(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def compute_ingest_health(
    items: list[dict[str, Any]],
    *,
    viral_threshold: int | None = None,
    viral_threshold_alt: int | None = None,
) -> dict[str, Any]:
    n = len(items)
    #可选扩展字段在归一后可能缺失
    opt_keys = (
        "published_at",
        "comment_proxy",
        "collect_proxy",
        "share_proxy",
    )
    key_present = {k: 0 for k in opt_keys}
    empty_or_missing_pa = 0
    unparseable_pa = 0
    parseable_dts: list[datetime] = []
    likes: list[int] = []

    pos_main = 0
    pos_alt = 0
    counted_main = 0
    counted_alt = 0

    for it in items:
        if not isinstance(it, dict):
            continue
        for k in opt_keys:
            if k in it and it[k] is not None and str(it[k]).strip() != "":
                key_present[k] += 1

        lp = it.get("like_proxy", 0)
        try:
            lk = int(lp) if lp is not None else 0
        except (TypeError, ValueError):
            lk = 0
        lk = max(0, lk)
        likes.append(lk)

        if viral_threshold is not None:
            counted_main += 1
            if lk >= int(viral_threshold):
                pos_main += 1
        if viral_threshold_alt is not None:
            counted_alt += 1
            if lk >= int(viral_threshold_alt):
                pos_alt += 1

        pa = it.get("published_at", "")
        if not isinstance(pa, str) or not pa.strip():
            empty_or_missing_pa += 1
            continue
        dt = _utc_from_published_cell(pa)
        if dt is None:
            unparseable_pa += 1
            continue
        parseable_dts.append(dt)

    parseable_n = len(parseable_dts)
    span_days: float | None = None
    min_utc: str | None = None
    max_utc: str | None = None
    if parseable_n >= 2:
        t_min = min(parseable_dts)
        t_max = max(parseable_dts)
        min_utc = t_min.isoformat()
        max_utc = t_max.isoformat()
        span_days = max(0.0, (t_max - t_min).total_seconds() / 86400.0)
    elif parseable_n == 1:
        min_utc = max_utc = parseable_dts[0].isoformat()
        span_days = 0.0

    def _frac(num: int) -> float | None:
        return round(num / n, 6) if n else None

    likes_sorted = sorted(likes) if likes else []
    mid = likes_sorted[len(likes_sorted) // 2] if likes_sorted else None

    return {
        "schema": "feed_ingest_health_v1",
        "n_items": n,
        "optional_field_presence_fraction": {k: _frac(key_present[k]) for k in opt_keys},
        "published_at": {
            "empty_or_missing_count": empty_or_missing_pa,
            "empty_or_missing_fraction": _frac(empty_or_missing_pa),
            "unparseable_non_empty_count": unparseable_pa,
            "unparseable_fraction": _frac(unparseable_pa),
            "parseable_count": parseable_n,
            "parseable_fraction": _frac(parseable_n),
            "span_days": span_days,
            "min_utc": min_utc,
            "max_utc": max_utc,
        },
        "like_proxy": {
            "min": likes_sorted[0] if likes_sorted else None,
            "max": likes_sorted[-1] if likes_sorted else None,
            "median": mid,
        },
        "label_counts_if_threshold_applied": {
            "viral_like_threshold": viral_threshold,
            "positive_y_rule": pos_main if viral_threshold is not None else None,
            "rows_used_for_y_rule": counted_main if viral_threshold is not None else None,
            "viral_like_threshold_alt": viral_threshold_alt,
            "positive_y_rule_alt": pos_alt if viral_threshold_alt is not None else None,
            "rows_used_for_y_rule_alt": counted_alt if viral_threshold_alt is not None else None,
        },
    }


def evaluate_gates(
    metrics: dict[str, Any],
    gates: dict[str, Any],
    *,
    viral_threshold: int | None,
    viral_threshold_alt: int | None,
) -> tuple[bool, list[str]]:
    """若 gates 为空对象，视为全部通过。"""
    reasons: list[str] = []
    if not gates:
        return True, reasons

    n = int(metrics.get("n_items") or 0)
    min_items = int(gates.get("min_items") or 0)
    if min_items > 0 and n < min_items:
        reasons.append("min_items_not_met")

    pa_block = metrics.get("published_at") or {}
    parseable_n = int(pa_block.get("parseable_count") or 0)
    min_parseable = int(gates.get("min_parseable_published_at_count") or 0)
    if min_parseable > 0 and parseable_n < min_parseable:
        reasons.append("min_parseable_published_at_count_not_met")

    min_pf = gates.get("min_parseable_published_at_fraction")
    if min_pf is not None and n > 0:
        try:
            need = float(min_pf)
        except (TypeError, ValueError):
            need = 0.0
        if need > 0 and (parseable_n / n) < need:
            reasons.append("min_parseable_published_at_fraction_not_met")

    max_miss = gates.get("max_missing_published_at_fraction")
    if max_miss is not None and n > 0:
        try:
            allow_miss = float(max_miss)
        except (TypeError, ValueError):
            allow_miss = 1.0
        missing_frac = 1.0 - (parseable_n / n)
        if missing_frac > allow_miss + 1e-9:
            reasons.append("max_missing_published_at_fraction_exceeded")

    min_span = gates.get("min_published_at_span_days")
    if min_span is not None:
        try:
            need_span = float(min_span)
        except (TypeError, ValueError):
            need_span = 0.0
        if need_span > 0:
            span = pa_block.get("span_days")
            if span is None:
                reasons.append("published_at_span_unavailable")
            elif float(span) < need_span:
                reasons.append("min_published_at_span_days_not_met")

    min_pos = int(gates.get("min_positive_y_rule") or 0)
    if min_pos > 0:
        if viral_threshold is None:
            reasons.append("min_positive_y_rule_requires_health_labels_spec")
        else:
            lc = metrics.get("label_counts_if_threshold_applied") or {}
            p = lc.get("positive_y_rule")
            if p is None or int(p) < min_pos:
                reasons.append("min_positive_y_rule_not_met")

    min_pos_alt = int(gates.get("min_positive_y_rule_alt") or 0)
    if min_pos_alt > 0:
        if viral_threshold_alt is None:
            reasons.append("min_positive_y_rule_alt_requires_alt_threshold_in_labels_spec")
        else:
            lc = metrics.get("label_counts_if_threshold_applied") or {}
            p = lc.get("positive_y_rule_alt")
            if p is None or int(p) < min_pos_alt:
                reasons.append("min_positive_y_rule_alt_not_met")

    return (len(reasons) == 0), reasons


def build_health_report_payload(
    metrics: dict[str, Any],
    gates_path: str | None,
    gates: dict[str, Any],
    gates_ok: bool,
    fail_reasons: list[str],
) -> dict[str, Any]:
    return {
        "schema": "feed_ingest_health_report_v1",
        "metrics": metrics,
        "gates_path": gates_path,
        "gates_effective": gates,
        "gates_ok": gates_ok,
        "fail_reasons": fail_reasons,
    }
