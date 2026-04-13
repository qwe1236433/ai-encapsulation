"""S3 Verify：规则层 + 硬指标前置的数据形态校验 + 启发式软审 + 可选 Ollama软审。"""

from __future__ import annotations

import json
import logging
from typing import Any

import metrics
import settings

logger = logging.getLogger(__name__)


def verify_openclaw_response(action: str, openclaw_body: dict[str, Any] | None) -> bool:
    if not openclaw_body or openclaw_body.get("status") != "success":
        return False
    r = openclaw_body.get("result") or {}
    if action == "analyze_trends":
        return "trend" in r and "series_tail" in r
    if action == "generate_headline":
        return "headline" in r and "engagement_preview" in r
    if action == "generate_hook_lines":
        lines = r.get("lines")
        return isinstance(lines, list) and len(lines) > 0
    if action == "predict_traffic":
        return "predicted_likes" in r
    if action == "competitor_audit":
        return isinstance(r.get("examples"), list) and len(r["examples"]) > 0
    if action == "gen_body":
        return "body" in r and len(str(r.get("body", ""))) >= 4
    if action == "tuning_params":
        return "tuning" in r and isinstance(r.get("tuning"), dict)
    if action == "simulated_publish":
        return "publish_id" in r and "published_at" in r
    if action == "fetch_metrics":
        return "predicted_likes" in r
    if action == "publish_and_monitor":
        return (
            isinstance(r.get("publish"), dict)
            and isinstance(r.get("metrics"), dict)
            and "publish_id" in r["publish"]
            and "predicted_likes" in r["metrics"]
        )
    if action == "extract_viral_patterns":
        return all(k in r for k in ("viral_sop", "core_hook", "target_emotion", "topic"))
    if action == "recreate_content":
        rc = r.get("recreated")
        return isinstance(rc, dict) and all(k in rc for k in ("title", "body", "tags"))
    if action == "predict_viral_score":
        return all(k in r for k in ("predicted_score", "confidence", "risk_factor"))
    if action == "prepare_xhs_post":
        return (
            "headline" in r
            and "body" in r
            and "experiment_variant_id" in r
            and isinstance(r.get("post_checklist"), dict)
        )
    if action == "sync_manual_result":
        return r.get("bound") is True and bool(r.get("real_note_id"))
    if action == "fetch_xhs_metrics":
        return "predicted_likes" in r
    if action == "flow_echo":
        return "echo" in r
    return True


def verify_flow_rules(action: str, openclaw_body: dict[str, Any] | None) -> tuple[bool, str]:
    if not openclaw_body or openclaw_body.get("status") != "success":
        return False, "executor not success"
    r = openclaw_body.get("result") or {}
    if action == "analyze_trends":
        if r.get("data_quality") == "bad" or r.get("trend") == "invalid":
            return False, "feed_quality_bad"
        if "confidence" not in r:
            return False, "missing confidence"
        try:
            c = float(r["confidence"])
        except (TypeError, ValueError):
            return False, "confidence not numeric"
        if not 0.0 <= c <= 1.0:
            return False, f"confidence out of [0,1]: {c}"
        return True, "rules_ok"
    if action == "generate_headline":
        headline = str(r.get("headline", ""))
        if len(headline) < 4:
            return False, "headline too short"
        ep = r.get("engagement_preview")
        if not isinstance(ep, dict) or "predicted_likes" not in ep:
            return False, "engagement_preview missing"
        try:
            pl = int(ep["predicted_likes"])
        except (TypeError, ValueError):
            return False, "predicted_likes invalid"
        sim = metrics.simulate_traffic_for_text(headline)
        if pl != int(sim["predicted_likes"]):
            return False, f"engagement mismatch headline vs sim: {pl} != {sim['predicted_likes']}"
        bundle = metrics.mock_engagement_bundle(headline)
        try:
            ctr_ep = float(ep["ctr_pct"])
        except (KeyError, TypeError, ValueError):
            return False, "ctr_pct missing"
        if abs(ctr_ep - float(bundle["ctr_pct"])) > 0.001:
            return False, f"ctr mismatch: {ctr_ep} != {bundle['ctr_pct']}"
        try:
            im_ep = int(ep["impressions"])
        except (KeyError, TypeError, ValueError):
            return False, "impressions missing"
        if im_ep != int(bundle["impressions"]):
            return False, f"impressions mismatch: {im_ep} != {bundle['impressions']}"
        return True, "rules_ok"
    if action == "generate_hook_lines":
        lines = r.get("lines")
        if not isinstance(lines, list) or len(lines) < 1:
            return False, "lines missing"
        if not all(len(str(x)) >= 2 for x in lines):
            return False, "line too short"
        return True, "rules_ok"
    if action == "predict_traffic":
        try:
            int(r["predicted_likes"])
        except (KeyError, TypeError, ValueError):
            return False, "predicted_likes invalid"
        return True, "rules_ok"
    if action == "competitor_audit":
        for ex in r.get("examples") or []:
            if not isinstance(ex, dict) or "title" not in ex:
                return False, "bad competitor example"
        return True, "rules_ok"
    if action == "gen_body":
        if len(str(r.get("body", ""))) < 8:
            return False, "body too short"
        return True, "rules_ok"
    if action == "tuning_params":
        return True, "rules_ok"
    if action == "simulated_publish":
        return True, "rules_ok"
    if action == "fetch_metrics":
        try:
            int(r["predicted_likes"])
        except (KeyError, TypeError, ValueError):
            return False, "predicted_likes invalid"
        return True, "rules_ok"
    if action == "publish_and_monitor":
        pub = r.get("publish")
        met = r.get("metrics")
        if not isinstance(pub, dict) or not isinstance(met, dict):
            return False, "compound_bad_shape"
        if "publish_id" not in pub:
            return False, "compound_missing_publish"
        try:
            int(met["predicted_likes"])
        except (KeyError, TypeError, ValueError):
            return False, "compound_metrics_invalid"
        return True, "rules_ok"
    if action == "extract_viral_patterns":
        for k in ("viral_sop", "core_hook", "target_emotion"):
            if len(str(r.get(k, "")).strip()) < 2:
                return False, f"extract_field_too_short:{k}"
        try:
            n = int(r.get("sample_size_effective") or 0)
        except (TypeError, ValueError):
            return False, "extract_sample_invalid"
        if n < 1:
            return False, "extract_no_samples"
        return True, "rules_ok"
    if action == "recreate_content":
        rc = r.get("recreated") if isinstance(r.get("recreated"), dict) else {}
        mt = settings.recreate_min_title_len()
        mb = settings.recreate_min_body_len()
        if len(str(rc.get("title", "")).strip()) < mt:
            return False, "recreate_title_too_short"
        if len(str(rc.get("body", "")).strip()) < mb:
            return False, "recreate_body_too_short"
        return True, "rules_ok"
    if action == "predict_viral_score":
        try:
            ps = float(r["predicted_score"])
            cf = float(r["confidence"])
        except (KeyError, TypeError, ValueError):
            return False, "predict_score_invalid"
        if not 0.0 <= ps <= 1.0:
            return False, "predicted_score_oob"
        if not 0.0 <= cf <= 1.0:
            return False, "confidence_oob"
        return True, "rules_ok"
    if action == "prepare_xhs_post":
        if len(str(r.get("headline", "")).strip()) < 4:
            return False, "xhs_headline_too_short"
        if len(str(r.get("body", "")).strip()) < 20:
            return False, "xhs_body_too_short"
        if not isinstance(r.get("hashtags"), list):
            return False, "xhs_hashtags_missing"
        return True, "rules_ok"
    if action == "sync_manual_result":
        if not r.get("bound"):
            return False, "xhs_sync_not_bound"
        return True, "rules_ok"
    if action == "fetch_xhs_metrics":
        try:
            int(r["predicted_likes"])
        except (KeyError, TypeError, ValueError):
            return False, "xhs_likes_invalid"
        return True, "rules_ok"
    ok = verify_openclaw_response(action, openclaw_body)
    return ok, "schema_ok" if ok else "schema_fail"


def _should_run_llm_soft_verify() -> bool:
    if not settings.llm_enabled():
        return False
    mode = settings.llm_verify_mode()
    if mode == "never":
        return False
    if mode == "if_soft":
        return settings.soft_verify_enabled()
    return True


def _llm_soft_verify(action: str, goal: str, result: dict[str, Any]) -> tuple[bool, str]:
    """Ollama 单行 PASS / FAIL 软裁决；异常时放行以免卡死任务。"""
    try:
        from llm import LLMManager

        mgr = LLMManager()
        system = (
            "You are a strict content/strategy reviewer. Output exactly one line: "
            "PASS or FAIL: <short reason>. English or Chinese ok."
        )
        excerpt = json.dumps(result, ensure_ascii=False)[:2800]
        user = f"User goal:\n{goal}\n\nOpenClaw action: {action}\n\nResult JSON:\n{excerpt}"
        text = mgr.chat(user, system)
        line = (text.strip().splitlines() or [""])[0].strip()
        u = line.upper()
        if u.startswith("PASS"):
            return True, f"llm_soft:{line[:160]}"
        if u.startswith("FAIL"):
            return False, f"llm_soft:{line[:160]}"
        logger.warning("llm soft verify unclear reply: %s", line[:200])
        return True, f"llm_soft_unclear_pass:{line[:120]}"
    except Exception as e:
        logger.warning("llm soft verify error (pass-through): %s", e)
        return True, f"llm_soft_error_skip:{e}"


def soft_review(action: str, goal: str, openclaw_body: dict[str, Any]) -> tuple[bool, str]:
    r = openclaw_body.get("result") or {}
    if not isinstance(r, dict):
        r = {}

    ok = True
    reason = "soft_skip"

    if action == "analyze_trends":
        if settings.verify_goal_alignment():
            if r.get("trend") == "flat" and any(k in goal for k in ("急", "冲", "拉升")):
                ok, reason = False, "goal_mismatch: trend flat vs urgent goal"
            elif r.get("trend") == "down" and any(k in goal for k in ("做多", "上攻")):
                ok, reason = False, "goal_mismatch: down trend vs long bias wording"
            else:
                reason = "soft_trend_ok"
        else:
            reason = "soft_trend_ok"
    elif action in (
        "generate_headline",
        "predict_traffic",
        "fetch_metrics",
        "fetch_xhs_metrics",
        "gen_body",
        "publish_and_monitor",
        "extract_viral_patterns",
        "recreate_content",
        "predict_viral_score",
    ):
        reason = "soft_defer_hard_metrics"
    elif action == "generate_hook_lines":
        if settings.soft_verify_enabled():
            lines = r.get("lines")
            if isinstance(lines, list) and lines and len(str(lines[0]).strip()) < 4:
                ok, reason = False, "soft: primary hook too thin"
            else:
                reason = "soft_hook_ok"
        else:
            reason = "soft_hook_ok"
    else:
        reason = "soft_skip"

    if not ok:
        return ok, reason
    if _should_run_llm_soft_verify():
        l_ok, l_reason = _llm_soft_verify(action, goal, r)
        if not l_ok:
            return False, f"{reason}; {l_reason}"
        return True, f"{reason}; {l_reason}"
    return ok, reason
