"""环境配置与流量实验默认值（战略层可调参数）。"""

from __future__ import annotations

import os

LINK_TIMEOUT_SEC = 5.0
PROCESS_TIMEOUT_SEC = 60.0

FLOW_ACTIONS: list[str] = [
    "analyze_trends",
    "competitor_audit",
    "generate_headline",
    "generate_hook_lines",
    "gen_body",
    "tuning_params",
    "simulated_publish",
    "fetch_metrics",
    "publish_and_monitor",
    "predict_traffic",
    "prepare_xhs_post",
    "fetch_xhs_metrics",
]


def verify_goal_alignment() -> bool:
    return (os.environ.get("HERMES_VERIFY_GOAL_ALIGNMENT") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def correct_pivot_prob() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("HERMES_CORRECT_PIVOT_PROB") or "0.35")))
    except ValueError:
        return 0.35


def explore_epsilon() -> float:
    raw = os.environ.get("HERMES_EXPLORE_EPSILON")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0.0, min(1.0, float(raw)))
        except ValueError:
            return 0.25
    return correct_pivot_prob()


def soft_verify_enabled() -> bool:
    return (os.environ.get("HERMES_SOFT_VERIFY_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def traffic_boost_keyword() -> str:
    return (os.environ.get("TRAFFIC_SIM_BOOST_KEYWORD") or "救命").strip() or "救命"


def traffic_likes_if_hit() -> int:
    try:
        return max(0, int(os.environ.get("TRAFFIC_SIM_LIKES_IF_HIT") or "1000"))
    except ValueError:
        return 1000


def traffic_likes_else() -> int:
    try:
        return max(0, int(os.environ.get("TRAFFIC_SIM_LIKES_ELSE") or "10"))
    except ValueError:
        return 10


def traffic_pass_min() -> int:
    try:
        return max(0, int(os.environ.get("TRAFFIC_SIM_PASS_MIN") or "500"))
    except ValueError:
        return 500


def traffic_ctr_pass_min() -> float | None:
    raw = os.environ.get("TRAFFIC_SIM_CTR_PASS_MIN")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return 3.0


def llm_enabled() -> bool:
    return (os.environ.get("HERMES_LLM_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def ollama_host() -> str:
    return (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")


def hermes_model() -> str:
    return (os.environ.get("HERMES_MODEL") or "gemma2:2b").strip() or "gemma2:2b"


def hermes_model_candidates() -> list[str]:
    """主模型优先，其余来自 HERMES_MODEL_FALLBACK（逗号分隔），用于 Ollama 404/缺模型时顺延。"""
    primary = hermes_model()
    raw = os.environ.get("HERMES_MODEL_FALLBACK") or "gemma2:latest,llama3.2:latest"
    out: list[str] = []
    for m in [primary] + [p.strip() for p in raw.split(",") if p.strip()]:
        if m not in out:
            out.append(m)
    return out


def ollama_timeout_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("HERMES_OLLAMA_TIMEOUT_SEC") or "120"))
    except ValueError:
        return 120.0


def llm_verify_mode() -> str:
    """always：启发式通过后必跑 LLM 软审；never：不跑；if_soft：仅当 HERMES_SOFT_VERIFY_ENABLED 时跑。"""
    v = (os.environ.get("HERMES_LLM_VERIFY_MODE") or "always").strip().lower()
    if v in ("never", "off", "false", "0"):
        return "never"
    if v in ("if_soft", "when_soft", "soft_only"):
        return "if_soft"
    return "always"


def case_library_decay_alpha() -> float:
    """时间权重：Weight = 1/(1 + alpha * DeltaHours)；alpha 越大记忆越短。"""
    try:
        return max(0.0, float(os.environ.get("CASE_LIBRARY_DECAY_ALPHA") or "0.04"))
    except ValueError:
        return 0.04


def case_library_max_age_hours() -> float:
    """硬 TTL（小时）；0 表示不截断，仅软衰减。"""
    try:
        return max(0.0, float(os.environ.get("CASE_LIBRARY_MAX_AGE_HOURS") or "0"))
    except ValueError:
        return 0.0


def case_cognitive_conflict_threshold() -> int:
    """同一 market fingerprint 下，引用案例后 Verify 连续失败达到此次数 → 熔断降级。"""
    try:
        return max(1, min(50, int(os.environ.get("CASE_CONFLICT_FAIL_THRESHOLD") or "3")))
    except ValueError:
        return 3


def case_deprecate_to_negative_pool() -> bool:
    return (os.environ.get("CASE_DEPRECATE_TO_NEGATIVE_POOL") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def case_fingerprint_match_mode() -> str:
    """strict=仅哈希全等；fuzzy=风格标签 Jaccard + 阈值（更易触发认知 streak / 检索环境加成）。"""
    v = (os.environ.get("CASE_FINGERPRINT_MATCH_MODE") or "fuzzy").strip().lower()
    if v in ("strict", "exact", "sha", "hash"):
        return "strict"
    return "fuzzy"


def case_fingerprint_jaccard_min() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("CASE_FINGERPRINT_JACCARD_MIN") or "0.7")))
    except ValueError:
        return 0.7


def case_fingerprint_require_keyword_match() -> bool:
    """为 True 时 fuzzy 模式下还要求 boost_keyword 一致才视为同环境。"""
    return (os.environ.get("CASE_FINGERPRINT_REQUIRE_KEYWORD_MATCH") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def conflict_jump_enabled() -> bool:
    return (os.environ.get("HERMES_CONFLICT_JUMP_ENABLED") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def conflict_jump_strength() -> float:
    """跳跃修正强度 0~1，越大参数/策略偏移越狠（约对应「至少多少幅度的跳出」）。"""
    try:
        return max(0.0, min(1.0, float(os.environ.get("HERMES_CONFLICT_JUMP_STRENGTH") or "0.5")))
    except ValueError:
        return 0.5


def negative_sev_catastrophe_round_mult() -> float:
    try:
        return max(0.25, min(20.0, float(os.environ.get("HERMES_NEGATIVE_SEV_CATASTROPHE_ROUND_MULT") or "3.0")))
    except ValueError:
        return 3.0


def negative_sev_mild_round_mult() -> float:
    try:
        return max(0.1, min(1.0, float(os.environ.get("HERMES_NEGATIVE_SEV_MILD_ROUND_MULT") or "0.55")))
    except ValueError:
        return 0.55


def negative_sev_catastrophe_retry_mult() -> float:
    try:
        return max(0.01, min(1.0, float(os.environ.get("HERMES_NEGATIVE_SEV_CATASTROPHE_RETRY_MULT") or "0.25")))
    except ValueError:
        return 0.25


def negative_sev_mild_retry_mult() -> float:
    try:
        return max(1.0, min(5.0, float(os.environ.get("HERMES_NEGATIVE_SEV_MILD_RETRY_MULT") or "2.0")))
    except ValueError:
        return 2.0
