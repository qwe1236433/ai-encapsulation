"""S1 Think / S4 Correct：战术意图与 ε-greedy 纠偏；S1 可接案例库 + LLM 规划。"""

from __future__ import annotations

import json
import logging
import random
from typing import Any

import memory
import negative_pool as neg_pool_mod
import settings

logger = logging.getLogger(__name__)

_XHS_FACTORY_ACTIONS = frozenset({"extract_viral_patterns", "recreate_content", "predict_viral_score"})


def is_xhs_factory_goal(goal: str) -> bool:
    g = (goal or "").strip()
    gl = g.lower()
    return any(k in g for k in ("小红书", "小红薯")) or "xhs" in gl


def collect_xhs_factory_results(trajectory: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for step in trajectory:
        if step.get("phase") != "act":
            continue
        env = step.get("envelope") or {}
        act = str(env.get("action") or "")
        if act not in _XHS_FACTORY_ACTIONS:
            continue
        oc = env.get("openclaw")
        if not isinstance(oc, dict) or oc.get("status") != "success":
            continue
        r = oc.get("result")
        if isinstance(r, dict):
            out[act] = r
    return out


def first_think_plan_params(trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    for step in trajectory:
        if step.get("phase") != "think":
            continue
        plan = step.get("plan") or {}
        p = plan.get("params")
        if isinstance(p, dict):
            return p
    return {}


def xhs_pipeline_followup(
    goal: str, trajectory: list[dict[str, Any]], current: dict[str, Any]
) -> dict[str, Any] | None:
    """extract → recreate → predict → prepare_xhs_post；仅在各步 Verify 通过后由 runner 调用。"""
    a = str(current.get("action") or "")
    if a not in _XHS_FACTORY_ACTIONS:
        return None
    ctx = collect_xhs_factory_results(trajectory)
    p0 = dict(current.get("params") or {})
    vid = str(current.get("variant_id") or p0.get("variant_id") or "vXHS.0")
    tone0 = str(first_think_plan_params(trajectory).get("tone") or "steady")

    if a == "extract_viral_patterns":
        gene = ctx.get("extract_viral_patterns") or {}
        orig = str(p0.get("source_text") or goal or "").strip()[:5000]
        return {
            "action": "recreate_content",
            "params": {
                "original_text": orig,
                "gene_sop": {
                    "viral_sop": gene.get("viral_sop"),
                    "core_hook": gene.get("core_hook"),
                    "target_emotion": gene.get("target_emotion"),
                },
                "style": str(p0.get("tone") or "sharp"),
                "variant_id": vid,
            },
            "variant_id": vid,
        }
    if a == "recreate_content":
        gene = ctx.get("extract_viral_patterns") or {}
        rec = ctx.get("recreate_content") or {}
        rt = rec.get("recreated") if isinstance(rec.get("recreated"), dict) else {}
        title = str(rt.get("title") or "")
        body = str(rt.get("body") or "")
        text = f"{title}\n{body}".strip()
        return {
            "action": "predict_viral_score",
            "params": {
                "recreated_text": text,
                "gene_sop": {
                    "viral_sop": gene.get("viral_sop"),
                    "core_hook": gene.get("core_hook"),
                    "target_emotion": gene.get("target_emotion"),
                },
                "case_library": [],
                "variant_id": vid,
            },
            "variant_id": vid,
        }
    if a == "predict_viral_score":
        ext = ctx.get("extract_viral_patterns") or {}
        rec = ctx.get("recreate_content") or {}
        rt = rec.get("recreated") if isinstance(rec.get("recreated"), dict) else {}
        pred = ctx.get("predict_viral_score") or {}
        topic = str(ext.get("topic") or goal[:90] or "流量主题")
        hl = str(rt.get("title") or "")
        body = str(rt.get("body") or "")
        tags = rt.get("tags")
        if isinstance(tags, list) and len(tags) >= 3:
            ht = [str(x).strip()[:24] for x in tags[:12] if str(x).strip()]
        else:
            ht = [topic[:10] or "复盘", "干货分享", tone0[:8] or "成长"]
        return {
            "action": "prepare_xhs_post",
            "params": {
                "topic": topic,
                "tone": tone0,
                "variant_id": vid,
                "headline": hl[:80],
                "body": body[:2000],
                "hashtags": ht,
                "factory_context": {
                    "predicted_score": pred.get("predicted_score"),
                    "confidence": pred.get("confidence"),
                    "risk_factor": pred.get("risk_factor"),
                },
            },
            "variant_id": vid,
        }
    return None


def last_hard_soft_verify(trajectory: list[dict[str, Any]]) -> tuple[str, str]:
    for step in reversed(trajectory):
        if not isinstance(step, dict):
            continue
        if step.get("phase") == "verify":
            return str(step.get("hard_reason") or ""), str(step.get("soft_reason") or "")
    return "", ""


def bold_params_for_action(action: str, goal: str, kw: str) -> dict[str, Any]:
    g = (goal or "").strip()
    if action == "analyze_trends":
        return {
            "symbol": random.choice(["PIVOT", "ALT_TOPIC", "NIGHT_SESSION", "CROSS_DESK"]),
            "window_min": random.choice([5, 20, 45, 90]),
        }
    if action == "generate_headline":
        topic = (g[:55] or "话题").strip()
        if kw in topic:
            topic = (topic.replace(kw, "") or "话题").strip()
        return {
            "topic": topic or "话题",
            "tone": random.choice(["sharp", "steady", "minimal", "absurdist"]),
            "boost": random.choice([True, False]),
        }
    if action == "generate_hook_lines":
        return {
            "topic": g[:60] or "复盘",
            "tone": random.choice(["sharp", "steady", "minimal"]),
            "hook_angle": random.choice(["反转", "对比", "数字", "身份反差", ""]),
        }
    if action == "predict_traffic":
        h = f"{kw}！{g[:90]}" if random.random() < 0.5 else (g[:120] or "空标题")
        return {"headline": h}
    if action == "competitor_audit":
        return {"niche": g[:60] or "通用", "limit": random.choice([3, 5, 7])}
    if action == "gen_body":
        return {
            "headline": g[:80] or "主题",
            "tone": random.choice(["sharp", "steady", "minimal"]),
            "style_hints": {"pace": random.choice(["快", "中速"]), "lead": random.choice(["反差", "共情"])},
        }
    if action == "tuning_params":
        return {
            "base": {"tone": random.choice(["sharp", "steady"]), "energy": 0.5},
            "delta": {"risk": round(random.uniform(0.2, 0.8), 2)},
        }
    if action == "simulated_publish":
        return {
            "variant_id": f"v1.{random.randint(0, 9)}",
            "headline": g[:100] or "mock",
            "channel": random.choice(["mock_dy", "mock_ks", "mock_xhs"]),
        }
    if action == "fetch_metrics":
        return {"headline": g[:120] or "mock", "publish_id": ""}
    if action == "extract_viral_patterns":
        return {
            "topic": g[:100] or "流量主题",
            "sample_size": random.choice([8, 12, 16]),
            "source_text": g[:800],
            "tone": random.choice(["sharp", "steady", "minimal"]),
        }
    if action == "recreate_content":
        return {
            "original_text": g[:500] or "原文占位",
            "gene_sop": {
                "viral_sop": "对照式",
                "core_hook": "反差开场",
                "target_emotion": "共鸣",
            },
            "style": random.choice(["sharp", "steady", "minimal"]),
        }
    if action == "predict_viral_score":
        return {
            "recreated_text": (g[:400] or "标题\n正文占位"),
            "gene_sop": {"viral_sop": "对照式", "core_hook": "", "target_emotion": "共鸣"},
            "case_library": [],
        }
    if action == "prepare_xhs_post":
        return {
            "topic": g[:90] or "流量主题",
            "tone": random.choice(["sharp", "steady", "minimal"]),
            "variant_id": f"vXHS.{random.randint(10, 99)}",
        }
    if action == "fetch_xhs_metrics":
        return {
            "variant_id": f"vXHS.{random.randint(10, 99)}",
            "note_id": "",
            "check_index": 0,
            "metrics_to_track": ["likes", "collects", "comments"],
        }
    if action == "publish_and_monitor":
        return {
            "variant_id": f"v1.{random.randint(0, 9)}",
            "headline": g[:100] or "mock",
            "body": "",
            "channel": random.choice(["mock_dy", "mock_ks", "mock_xhs"]),
            "wait_sec": 2.0,
        }
    return {"symbol": "DEFAULT", "window_min": 15}


def retrieve_cases(goal: str, *, max_cases: int = 2) -> list[dict[str, Any]]:
    """从 case_library 检索成功案例，供 S1 注入（关键词优先，否则随机）。"""
    return memory.retrieve_cases_for_goal(goal, max_cases=max_cases)


def format_cases_for_prompt(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return ""
    lines: list[str] = []
    for c in cases:
        mc = memory.card_effective_market_context(c)
        tw = memory.card_time_weight(c)
        lines.append(
            json.dumps(
                {
                    "task_id": c.get("task_id"),
                    "goal": c.get("goal"),
                    "time_weight": round(float(tw), 4),
                    "market_context": {
                        "fingerprint": mc.get("fingerprint"),
                        "style_tags": mc.get("style_tags"),
                        "boost_keyword": mc.get("boost_keyword"),
                    },
                    "candidate_formula": c.get("candidate_formula"),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def think(goal: str) -> dict[str, Any]:
    """
    S1 统一入口：先检索案例库，再 LLM 规划（若启用），否则 pseudo_think。
    返回的 plan 含 think_mode、case_library_context、retrieved_case_ids（若有）、market_context。
    """
    mctx = memory.market_fingerprint(goal)
    cases = retrieve_cases(goal)
    ctx = format_cases_for_prompt(cases)
    case_ids = [str(c.get("task_id")) for c in cases if c.get("task_id")]

    if settings.llm_enabled():
        try:
            from llm import LLMManager

            plan = LLMManager().plan_tactical_intent(goal, ctx)
            plan["think_mode"] = "llm"
            plan["retrieved_case_ids"] = case_ids
            plan["market_context"] = mctx
            if ctx:
                plan["case_library_context"] = ctx
            return plan
        except Exception as e:
            logger.warning("LLM think failed, fallback to pseudo_think: %s", e)

    plan = pseudo_think(goal)
    plan["think_mode"] = "pseudo"
    plan["retrieved_case_ids"] = case_ids
    plan["market_context"] = mctx
    if ctx:
        plan["case_library_context"] = ctx
    return plan


def pseudo_think(goal: str) -> dict[str, Any]:
    g = (goal or "").strip()
    gl = g.lower()
    kw = settings.traffic_boost_keyword()
    if is_xhs_factory_goal(g):
        vid = f"vXHS.{random.randint(10, 99)}"
        tone = random.choice(["sharp", "steady", "minimal"])
        return {
            "action": "extract_viral_patterns",
            "params": {
                "topic": g[:100] if g else "流量主题",
                "sample_size": random.choice([8, 12, 16]),
                "source_text": g[:800],
                "tone": tone,
                "variant_id": vid,
            },
            "variant_id": vid,
            "s1_pipeline_hint": "Op1 extract_viral_patterns → plan gene_sop → Op2 recreate_content "
            "(runner chains predict_viral_score → prepare_xhs_post)",
        }
    if any(k in g for k in ("文案", "出词", "标题", "话术", "钩子", "爆款")):
        topic0 = (g[:60] if g else "流量主题")
        if kw in topic0:
            topic0 = (topic0.replace(kw, "") or "流量主题").strip() or "流量主题"
        return {
            "action": "generate_headline",
            "params": {
                "topic": topic0,
                "tone": random.choice(["sharp", "steady", "minimal"]),
                "boost": False,
            },
            "variant_id": "v1.1",
        }
    if any(k in g for k in ("预测", "预估", "跑分")) and any(
        k in g for k in ("流量", "点赞", "曝光", "互动")
    ):
        return {
            "action": "predict_traffic",
            "params": {"headline": g[:120] or "空标题"},
            "variant_id": "v1.1",
        }
    if any(x in gl for x in ("trend", "flow", "traffic")) or any(k in g for k in ("趋势", "行情", "流量")):
        return {
            "action": "analyze_trends",
            "params": {
                "symbol": random.choice(["HS_FLOW", "TOPIC_A", "MAIN_BOARD"]),
                "window_min": random.choice([10, 15, 20, 30]),
            },
            "variant_id": "v1.1",
        }
    action = random.choice(settings.FLOW_ACTIONS)
    if action == "analyze_trends":
        return {"action": action, "params": {"symbol": "DEFAULT", "window_min": 15}, "variant_id": "v1.1"}
    if action == "generate_headline":
        t = g[:60] or "复盘"
        if kw in t:
            t = (t.replace(kw, "") or "复盘").strip()
        return {
            "action": action,
            "params": {"topic": t, "tone": "sharp", "boost": False},
            "variant_id": "v1.1",
        }
    if action == "generate_hook_lines":
        return {
            "action": action,
            "params": {
                "topic": g[:60] or "复盘",
                "tone": random.choice(["sharp", "steady"]),
                "hook_angle": "",
            },
            "variant_id": "v1.1",
        }
    return {
        "action": "predict_traffic",
        "params": {"headline": g[:120] or "默认标题"},
        "variant_id": "v1.1",
    }


_TONE_JUMP: dict[str, str] = {
    "sharp": "minimal",
    "steady": "absurdist",
    "minimal": "sharp",
    "debug": "steady",
    "absurdist": "steady",
}


def apply_jump_correction(revision: dict[str, Any], goal: str, kw: str) -> dict[str, Any]:
    """认知冲突提示或熔断后：跳跃式修正参数，避免在同一局部微调。"""
    if not settings.conflict_jump_enabled():
        return revision
    out = dict(revision)
    params = dict(revision.get("params") or {})
    action = str(revision.get("action") or "")
    strength = max(0.2, float(settings.conflict_jump_strength()))
    touched = False

    if action == "generate_headline":
        touched = True
        params["boost"] = not bool(params.get("boost"))
        t = str(params.get("tone") or "")
        params["tone"] = _TONE_JUMP.get(t, random.choice(["sharp", "minimal", "absurdist"]))
        topic0 = str(params.get("topic") or goal[:50] or "主题").strip()
        pivot = random.choice(["对向叙事", "反常识", "高压钩子", "降噪陈述"])
        params["topic"] = (pivot + "·" + topic0)[-75:]
    elif action == "generate_hook_lines":
        touched = True
        an0 = str(params.get("hook_angle") or "")
        alts = [x for x in ("反转", "对比", "数字", "限时", "身份反差") if x != an0]
        params["hook_angle"] = random.choice(alts or ["反转"])
        t = str(params.get("tone") or "")
        params["tone"] = _TONE_JUMP.get(t, "sharp")
        params["topic"] = str(params.get("topic") or goal[:55] or "复盘").strip()[-60:]
    elif action == "analyze_trends":
        touched = True
        wm = int(params.get("window_min") or 15)
        bump = max(10, int(40 * strength))
        params["window_min"] = max(5, int(wm * (1.0 + strength) + random.randint(10, bump)))
        params["symbol"] = random.choice(["PIVOT", "ALT_TOPIC", "CROSS_DESK", "NIGHT_SESSION"])
    elif action == "predict_traffic":
        touched = True
        base = str(params.get("headline") or goal[:120] or "标题")
        if kw and kw not in base:
            base = f"{kw}·{base}"
        flip = random.choice(("结构性改写：", "情绪对撞：", "对立命题："))
        params["headline"] = (flip + base)[:200]
    elif action == "publish_and_monitor":
        touched = True
        params["wait_sec"] = float(params.get("wait_sec") or 2.0) * (1.0 + strength * 2.0)
        ch = str(params.get("channel") or "")
        pool_ch = [c for c in ("mock_dy", "mock_ks", "mock_xhs") if c != ch]
        params["channel"] = random.choice(pool_ch or ["mock_dy"])
        hl = str(params.get("headline") or goal[:100] or "mock")
        params["headline"] = ("跳出测试·" + hl)[-120:]
    elif action == "extract_viral_patterns":
        touched = True
        try:
            n = int(params.get("sample_size") or 12)
        except (TypeError, ValueError):
            n = 12
        params["sample_size"] = max(5, min(40, n + random.choice([4, 8, 12])))
        params["topic"] = ("视角切换·" + str(params.get("topic") or goal[:80] or "主题"))[-100:]
    elif action == "recreate_content":
        touched = True
        params["style"] = _TONE_JUMP.get(str(params.get("style") or ""), random.choice(["sharp", "minimal"]))
        gh = dict(params.get("gene_sop") or {})
        gh["viral_sop"] = random.choice(["对照式", "递进式", "悬念前置", "清单体"])
        params["gene_sop"] = gh
    elif action == "predict_viral_score":
        touched = True
        params["recreated_text"] = ("改写锚点·" + str(params.get("recreated_text") or goal[:200]))[-800:]
    elif action == "gen_body":
        touched = True
        params["tone"] = _TONE_JUMP.get(str(params.get("tone") or ""), "sharp")
        hints = dict(params.get("style_hints") or {})
        hints["pace"] = "快" if str(hints.get("pace")) != "快" else "慢"
        params["style_hints"] = hints

    out["params"] = params
    if touched:
        out["jump_correction"] = True
    return out


def _finalize_s4_revision(
    rev: dict[str, Any],
    goal: str,
    kw: str,
    *,
    cognitive_conflict_hint: str,
    force_explore: bool,
) -> dict[str, Any]:
    if cognitive_conflict_hint and "cognitive_conflict_hint" not in rev:
        rev = dict(rev)
        rev["cognitive_conflict_hint"] = cognitive_conflict_hint
    if not (force_explore or cognitive_conflict_hint):
        return rev
    return apply_jump_correction(rev, goal, kw)


def pick_explore_revision(
    current_action: str,
    goal: str,
    kw: str,
    failed_attempt: int,
    eps: float,
    pool: list[dict[str, Any]],
    *,
    pool_check_round: int,
) -> dict[str, Any]:
    alts = [x for x in settings.FLOW_ACTIONS if x != current_action]
    vid = f"vE{failed_attempt}.{random.randint(10, 99)}"
    for _ in range(28):
        alt = random.choice(alts)
        cand = bold_params_for_action(alt, goal, kw)
        blocked, tag = neg_pool_mod.negative_pool_resolve_action_params(alt, cand, pool, pool_check_round)
        if not blocked:
            out: dict[str, Any] = {
                "action": alt,
                "params": cand,
                "variant_id": vid,
                "correct_mode": "explore",
                "epsilon": eps,
            }
            if tag == "stale_retry":
                out["stale_retry"] = True
            return out
    alt = random.choice(alts)
    return {
        "action": alt,
        "params": bold_params_for_action(alt, goal, kw),
        "variant_id": vid,
        "correct_mode": "explore",
        "epsilon": eps,
    }


def pseudo_correct(
    goal: str,
    trajectory: list[dict[str, Any]],
    current: dict[str, Any],
    *,
    failed_attempt: int = 1,
    pool: list[dict[str, Any]] | None = None,
    pool_check_round: int = 1,
    cognitive_conflict_hint: str = "",
    force_explore: bool = False,
) -> dict[str, Any]:
    """pool：负样本池列表（参数名避免与模块 negative_pool 同名遮蔽）。"""
    neg = pool or []
    a = current.get("action") or "analyze_trends"
    p = dict(current.get("params") or {})
    kw = settings.traffic_boost_keyword()
    eps = settings.explore_epsilon()
    hard_reason, _ = last_hard_soft_verify(trajectory)

    if force_explore:
        rev = pick_explore_revision(a, goal, kw, failed_attempt, eps, neg, pool_check_round=pool_check_round)
        if cognitive_conflict_hint:
            rev["cognitive_conflict_hint"] = cognitive_conflict_hint
        return _finalize_s4_revision(
            rev, goal, kw, cognitive_conflict_hint=cognitive_conflict_hint, force_explore=force_explore
        )

    headline_traffic_retry = (
        a == "generate_headline"
        and failed_attempt == 1
        and "likes" in hard_reason
        and "pass_min" in hard_reason
    )
    explore = random.random() < eps
    if headline_traffic_retry:
        explore = False

    if explore:
        rev = pick_explore_revision(a, goal, kw, failed_attempt, eps, neg, pool_check_round=pool_check_round)
        if cognitive_conflict_hint:
            rev["cognitive_conflict_hint"] = cognitive_conflict_hint
        return _finalize_s4_revision(
            rev, goal, kw, cognitive_conflict_hint=cognitive_conflict_hint, force_explore=force_explore
        )

    if a == "analyze_trends":
        p["window_min"] = int(p.get("window_min", 15)) + random.choice([5, 10, 15])
        p["symbol"] = random.choice(["HS_FLOW", "TOPIC_B", "MAIN_BOARD", "NIGHT_SESSION"])
    elif a == "generate_headline":
        p["boost"] = True
        p["tone"] = random.choice(["sharp", "steady", "minimal", "debug"])
        p["topic"] = str(p.get("topic") or goal[:50] or "流量复盘").strip()
    elif a == "generate_hook_lines":
        p["tone"] = random.choice(["sharp", "steady", "minimal", "debug"])
        p["hook_angle"] = random.choice(["反转", "对比", "数字", "限时", ""])
        p["topic"] = str(p.get("topic") or goal[:55] or "流量复盘").strip()
    elif a == "predict_traffic":
        last_headline = ""
        for step in reversed(trajectory):
            if not isinstance(step, dict):
                continue
            if step.get("phase") == "act":
                env = step.get("envelope") or {}
                oc = env.get("openclaw") if isinstance(env.get("openclaw"), dict) else None
                if oc and isinstance(oc.get("result"), dict):
                    res = oc["result"]
                    h = res.get("headline")
                    if isinstance(h, str) and h:
                        last_headline = h
                        break
        text = str(p.get("headline") or last_headline or goal[:120] or "")
        if kw not in text:
            text = f"{kw}！{text}" if text else kw
        p["headline"] = text
    elif a == "publish_and_monitor":
        p["headline"] = str(p.get("headline") or goal[:100] or "mock").strip()
        p["wait_sec"] = float(p.get("wait_sec") or 2.0) + 1.0
        p["channel"] = random.choice(["mock_dy", "mock_ks", "mock_xhs"])
    else:
        rev = pick_explore_revision(a, goal, kw, failed_attempt, eps, neg, pool_check_round=pool_check_round)
        if cognitive_conflict_hint:
            rev["cognitive_conflict_hint"] = cognitive_conflict_hint
        return _finalize_s4_revision(
            rev, goal, kw, cognitive_conflict_hint=cognitive_conflict_hint, force_explore=force_explore
        )

    stale_retry = False
    for _ in range(10):
        blocked, tag = neg_pool_mod.negative_pool_resolve_action_params(str(a), p, neg, pool_check_round)
        if not blocked:
            stale_retry = tag == "stale_retry"
            break
        if a == "generate_headline":
            p["tone"] = random.choice(["sharp", "steady", "minimal", "debug", "absurdist"])
            p["boost"] = not bool(p.get("boost"))
        elif a == "publish_and_monitor":
            p["wait_sec"] = float(p.get("wait_sec", 2.0)) + 0.5
        else:
            break

    out_exploit: dict[str, Any] = {
        "action": a,
        "params": p,
        "variant_id": f"v1.{failed_attempt + 1}",
        "correct_mode": "exploit",
        "epsilon": eps,
    }
    if stale_retry:
        out_exploit["stale_retry"] = True
    if cognitive_conflict_hint:
        out_exploit["cognitive_conflict_hint"] = cognitive_conflict_hint
    return _finalize_s4_revision(
        out_exploit, goal, kw, cognitive_conflict_hint=cognitive_conflict_hint, force_explore=force_explore
    )
