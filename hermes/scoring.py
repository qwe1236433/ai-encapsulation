"""ScoringEngine：对数刻度 + 动态 P90 天花板（显著性分数）。"""

from __future__ import annotations

import math
import os
from typing import Any

import settings


def score_use_log() -> bool:
    return (os.environ.get("TRAFFIC_SCORE_USE_LOG") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def traffic_score_log_ceil_likes() -> float:
    try:
        raw = os.environ.get("TRAFFIC_SCORE_LOG_CEIL_LIKES")
        if raw is not None and str(raw).strip() != "":
            return max(100.0, float(raw))
    except ValueError:
        pass
    return float(
        max(
            settings.traffic_pass_min() * 4,
            settings.traffic_likes_if_hit() * 2,
            5000,
        )
    )


def log_norm_component(value: float, floor: float, ceiling: float) -> float:
    value = max(0.0, float(value))
    floor = max(0.0, float(floor))
    ceiling = max(floor + 1.0, float(ceiling))
    lo = math.log1p(floor)
    hi = math.log1p(ceiling)
    v = math.log1p(value)
    if hi <= lo:
        return 1.0 if value >= floor else 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def quantile_vals(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    q = max(0.0, min(1.0, q))
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return float(s[idx])


def score_dynamic_ceil_enabled() -> bool:
    return (os.environ.get("TRAFFIC_SCORE_DYNAMIC_CEIL") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def score_dynamic_k() -> int:
    try:
        return max(3, min(200, int(os.environ.get("TRAFFIC_SCORE_DYNAMIC_K") or "24")))
    except ValueError:
        return 24


def score_dynamic_min_n() -> int:
    try:
        return max(2, min(100, int(os.environ.get("TRAFFIC_SCORE_DYNAMIC_MIN_N") or "4")))
    except ValueError:
        return 4


def resolve_log_ceilings(
    history: list[dict[str, Any]] | None,
) -> tuple[float, float, dict[str, Any]]:
    """对数评分上沿：静态保底 + 可选按近期成功分布（分位数）抬高，缓解后期分数贴顶。"""
    static_l = traffic_score_log_ceil_likes()
    try:
        static_c = float(os.environ.get("TRAFFIC_SCORE_LOG_CEIL_CTR") or "12.0")
    except ValueError:
        static_c = 12.0
    meta: dict[str, Any] = {
        "dynamic": False,
        "history_len": len(history or []),
        "p90_likes": None,
        "p90_ctr": None,
        "likes_ceil_static": static_l,
        "ctr_ceil_static": static_c,
    }
    if not score_dynamic_ceil_enabled() or not history:
        return static_l, static_c, meta

    k = score_dynamic_k()
    tail = history[-k:]
    likes: list[float] = []
    ctrs: list[float] = []
    for x in tail:
        if x.get("likes") is not None:
            try:
                likes.append(float(x["likes"]))
            except (TypeError, ValueError):
                pass
        if x.get("ctr_pct") is not None:
            try:
                ctrs.append(float(x["ctr_pct"]))
            except (TypeError, ValueError):
                pass

    min_n = score_dynamic_min_n()
    try:
        q = float(os.environ.get("TRAFFIC_SCORE_DYNAMIC_QUANTILE") or "0.9")
    except ValueError:
        q = 0.9
    q = max(0.5, min(0.99, q))
    try:
        mult = float(os.environ.get("TRAFFIC_SCORE_DYNAMIC_CEIL_MULT") or "1.12")
    except ValueError:
        mult = 1.12
    try:
        w = float(os.environ.get("TRAFFIC_SCORE_DYNAMIC_STATIC_WEIGHT") or "0.55")
    except ValueError:
        w = 0.55
    w = max(0.15, min(1.0, w))

    floor_l = max(float(settings.traffic_pass_min()), float(settings.traffic_likes_else()) * 5.0)
    likes_ceil = static_l
    ctr_ceil = static_c

    if len(likes) >= min_n:
        p_l = quantile_vals(likes, q)
        dyn_l = max(floor_l, p_l * mult)
        likes_ceil = max(floor_l, dyn_l, static_l * w)
        meta["dynamic"] = True
        meta["p90_likes"] = p_l

    if len(ctrs) >= min_n:
        try:
            ctr_floor = float(os.environ.get("TRAFFIC_SCORE_LOG_FLOOR_CTR") or "0.15")
        except ValueError:
            ctr_floor = 0.15
        p_c = quantile_vals(ctrs, q)
        dyn_c = max(ctr_floor * 2.0, p_c * mult)
        ctr_ceil = max(dyn_c, static_c * w)
        meta["dynamic"] = True
        meta["p90_ctr"] = p_c

    return likes_ceil, ctr_ceil, meta


def compute_verify_score(
    metrics: dict[str, Any],
    *,
    hard_ok: bool,
    success_history: list[dict[str, Any]] | None = None,
) -> tuple[float, dict[str, Any]]:
    """0~1 分数；对数刻度 + 可选动态上沿（见 success_history）。"""
    pm = max(settings.traffic_pass_min(), 1)
    ctr_min = settings.traffic_ctr_pass_min() or 3.0
    use_log = score_use_log()
    likes_ceil, ctr_ceil, ceil_meta = resolve_log_ceilings(success_history if use_log else None)
    ctx: dict[str, Any] = {
        "likes_ceil": likes_ceil,
        "ctr_ceil": ctr_ceil,
        "ceil_meta": ceil_meta,
    }
    try:
        likes = float(metrics.get("likes") or 0)
    except (TypeError, ValueError):
        likes = 0.0
    if use_log:
        likes_floor = float(settings.traffic_likes_else())
        s_l = log_norm_component(likes, likes_floor, likes_ceil)
    else:
        s_l = max(0.0, min(1.0, likes / float(pm)))
    ctr = metrics.get("ctr_pct")
    if ctr is not None:
        try:
            c = float(ctr)
            if use_log:
                try:
                    ctr_floor = float(os.environ.get("TRAFFIC_SCORE_LOG_FLOOR_CTR") or "0.15")
                except ValueError:
                    ctr_floor = 0.15
                s_c = log_norm_component(c, ctr_floor, ctr_ceil)
            else:
                s_c = max(0.0, min(1.0, c / float(ctr_min)))
            base = 0.55 * s_l + 0.45 * s_c
        except (TypeError, ValueError):
            base = s_l
    else:
        base = s_l
    if not hard_ok:
        base *= 0.45
    return round(base, 4), ctx
