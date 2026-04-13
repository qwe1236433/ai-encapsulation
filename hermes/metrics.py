"""执行结果指标与硬门槛（与 OpenClaw 返回对齐的量化校验）。"""

from __future__ import annotations

from typing import Any

import settings


def mock_engagement_bundle(text: str) -> dict[str, Any]:
    """与 OpenClaw mock_engagement_bundle 同构，用于 Hermes 复算 CTR/曝光。"""
    sim = simulate_traffic_for_text(text)
    likes = int(sim["predicted_likes"])
    hit = bool(sim["boost_keyword_hit"])
    s = sum(ord(c) for c in (text or "")[:120]) % 1000 / 1000.0
    ctr = round(4.5 + s * 3.0, 2) if hit else round(0.35 + s * 1.55, 2)
    jitter = (sum(ord(c) for c in (text or "")[:40]) % 500) / 25.0
    imps = max(200, int(likes * (60.0 + jitter)))
    out = dict(sim)
    out["ctr_pct"] = ctr
    out["impressions"] = imps
    return out


def simulate_traffic_for_text(text: str) -> dict[str, Any]:
    """与 OpenClaw `simulate_traffic_for_text` 同规则（环境变量同名）。"""
    kw = settings.traffic_boost_keyword()
    hit = kw in (text or "")
    likes = settings.traffic_likes_if_hit() if hit else settings.traffic_likes_else()
    return {
        "predicted_likes": likes,
        "boost_keyword": kw,
        "boost_keyword_hit": hit,
        "rule": f"contains '{kw}' -> {settings.traffic_likes_if_hit()} else {settings.traffic_likes_else()}",
    }


def metrics_from_executor(action: str, r: dict[str, Any]) -> dict[str, Any]:
    """从算子结果抽出用于硬校验的指标（可扩展）。"""
    if action == "generate_headline":
        ep = r.get("engagement_preview")
        if isinstance(ep, dict):
            return {
                "likes": ep.get("predicted_likes"),
                "ctr_pct": ep.get("ctr_pct"),
                "impressions": ep.get("impressions"),
            }
    if action in ("predict_traffic", "fetch_metrics", "fetch_xhs_metrics"):
        return {
            "likes": r.get("predicted_likes"),
            "ctr_pct": r.get("ctr_pct"),
            "impressions": r.get("impressions"),
            "defer_verify": bool(r.get("verify_defer")),
        }
    if action == "gen_body":
        body = str(r.get("body", ""))
        if len(body) >= 8:
            b = mock_engagement_bundle(body)
            return {"likes": b["predicted_likes"], "ctr_pct": b["ctr_pct"], "impressions": b["impressions"]}
        return {"likes": 0, "ctr_pct": 0.0, "impressions": 0}
    if action == "publish_and_monitor":
        m = r.get("metrics")
        if isinstance(m, dict):
            return {
                "likes": m.get("predicted_likes"),
                "ctr_pct": m.get("ctr_pct"),
                "impressions": m.get("impressions"),
            }
    return {}


def hard_verify_metrics(
    action: str,
    metrics: dict[str, Any],
) -> tuple[bool, str]:
    """硬指标：点赞、CTR（若可观测）。"""
    if action in (
        "generate_headline",
        "predict_traffic",
        "fetch_metrics",
        "fetch_xhs_metrics",
        "gen_body",
        "publish_and_monitor",
    ):
        if action == "fetch_xhs_metrics" and metrics.get("defer_verify"):
            return False, "insufficient_sample_defer"
        pm = settings.traffic_pass_min()
        ctr_min = settings.traffic_ctr_pass_min()
        try:
            likes = int(metrics.get("likes") or 0)
        except (TypeError, ValueError):
            return False, "likes invalid"
        if likes < pm:
            return False, f"hard_fail likes={likes} < pass_min={pm}"
        if ctr_min is not None and metrics.get("ctr_pct") is not None:
            try:
                ctr = float(metrics["ctr_pct"])
            except (TypeError, ValueError):
                return False, "ctr invalid"
            if ctr < float(ctr_min):
                return False, f"hard_fail ctr_pct={ctr} < pass_min={ctr_min}"
        return True, "hard_ok"
    return True, "hard_skip"
