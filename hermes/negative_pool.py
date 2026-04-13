"""Long-term：负样本池 + strike/stale 观测聚合。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

import settings
import storage

_stale_retry_outcomes: deque[int] = deque(maxlen=200)
_STALE_OUTCOME_LOCK = threading.Lock()
_NEG_OBS_AGG_LOCK = threading.Lock()


def stale_outcome_lock() -> threading.Lock:
    return _STALE_OUTCOME_LOCK


def stale_retry_outcomes() -> deque[int]:
    return _stale_retry_outcomes


def params_fingerprint(action: str, params: dict[str, Any]) -> str:
    raw = json.dumps({"action": action, "params": params}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def negative_pool_enabled() -> bool:
    return (os.environ.get("HERMES_NEGATIVE_POOL_ENABLED") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def negative_pool_max() -> int:
    try:
        return max(4, min(512, int(os.environ.get("HERMES_NEGATIVE_POOL_MAX") or "64")))
    except ValueError:
        return 64


def negative_pool_decay_rounds() -> int:
    try:
        return max(1, min(10_000, int(os.environ.get("HERMES_NEGATIVE_POOL_DECAY_ROUNDS") or "12")))
    except ValueError:
        return 12


def negative_soft_retry_prob() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("HERMES_NEGATIVE_SOFT_RETRY_PROB") or "0.01")))
    except ValueError:
        return 0.01


def negative_pool_dynamic_mode() -> bool:
    return (os.environ.get("HERMES_NEGATIVE_POOL_DYNAMIC_MODE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def negative_pool_confidence_threshold() -> float:
    try:
        return max(0.5, min(0.999, float(os.environ.get("HERMES_NEGATIVE_POOL_CONFIDENCE_THRESHOLD") or "0.95")))
    except ValueError:
        return 0.95


def negative_pool_empirical_min_n() -> int:
    try:
        return max(0, min(10_000, int(os.environ.get("HERMES_NEGATIVE_POOL_EMPIRICAL_MIN_N") or "8")))
    except ValueError:
        return 8


def negative_pool_exploration_budget() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("HERMES_NEGATIVE_POOL_EXPLORATION_BUDGET") or "0.05")))
    except ValueError:
        return 0.05


def negative_pool_decay_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("HERMES_NEGATIVE_POOL_DECAY_SEC") or "0"))
    except ValueError:
        return 0.0


def parse_iso_age_sec(iso_s: str) -> float | None:
    try:
        s = (iso_s or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def negative_obs_aggregate_path() -> str:
    return os.path.join(storage.sessions_dir(), "_obs", "negative_pool_aggregate.json")


def classify_failure_severity(hard_reason: str, metrics: dict[str, Any] | None) -> str:
    """轻微失败 → 更快 stale、更高复活权重；灾难性失败 → 更长封杀、更低复活概率。"""
    hr = (hard_reason or "").lower()
    m = metrics or {}
    try:
        ctr = float(m.get("ctr_pct")) if m.get("ctr_pct") is not None else None
    except (TypeError, ValueError):
        ctr = None
    try:
        likes = int(m.get("likes") or 0)
    except (TypeError, ValueError):
        likes = -1
    if ctr is not None and ctr <= 0.2:
        return "catastrophic"
    if likes == 0 and ("likes" in hr or "like" in hr):
        return "catastrophic"
    if "executor_not_success" in hr:
        return "catastrophic"
    if any(x in hr for x in ("border", "barely", "边缘", "勉强")):
        return "mild"
    return "moderate"


def negative_entry_stale(entry: dict[str, Any], check_round: int) -> bool:
    last_r = int(entry.get("last_confirmed_round") or 0)
    round_gap = check_round - last_r
    sev = str(entry.get("failure_severity") or "moderate")
    mult = 1.0
    if sev == "catastrophic":
        mult = settings.negative_sev_catastrophe_round_mult()
    elif sev == "mild":
        mult = settings.negative_sev_mild_round_mult()
    need = max(1, int(negative_pool_decay_rounds() * mult + 0.5))
    rounds_stale = round_gap >= need
    time_stale = False
    ds = negative_pool_decay_sec()
    if ds > 0:
        age = parse_iso_age_sec(str(entry.get("updated_at") or ""))
        if age is not None and age >= ds:
            time_stale = True
    return bool(rounds_stale or time_stale)


def severity_adjusted_retry_prob(entry: dict[str, Any], base_prob: float) -> float:
    sev = str(entry.get("failure_severity") or "moderate")
    if sev == "catastrophic":
        return float(max(0.0, min(1.0, base_prob * settings.negative_sev_catastrophe_retry_mult())))
    if sev == "mild":
        return float(max(0.0, min(1.0, base_prob * settings.negative_sev_mild_retry_mult())))
    return float(max(0.0, min(1.0, base_prob)))


def load_global_strike_survival_totals() -> dict[str, dict[str, int]]:
    path = negative_obs_aggregate_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("strike_survival_totals") or {}
        out: dict[str, dict[str, int]] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            out[str(k)] = {
                "n": int(v.get("n") or 0),
                "later_success": int(v.get("later_success") or 0),
            }
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def firm_block_by_confidence(entry: dict[str, Any]) -> bool:
    if not negative_pool_dynamic_mode():
        return True
    th = negative_pool_confidence_threshold()
    s = int(entry.get("strike_count") or 1)
    buckets = load_global_strike_survival_totals()
    b = buckets.get(str(s), {})
    n = int(b.get("n", 0))
    if n < negative_pool_empirical_min_n():
        return True
    ls = int(b.get("later_success", 0))
    rate = ls / n if n else 0.0
    return rate < (1.0 - th)


def record_stale_retry_outcome(passed: bool) -> None:
    with _STALE_OUTCOME_LOCK:
        _stale_retry_outcomes.append(1 if passed else 0)


def effective_stale_retry_prob() -> float:
    base = negative_soft_retry_prob()
    if not negative_pool_dynamic_mode():
        return base
    budget = negative_pool_exploration_budget()
    with _STALE_OUTCOME_LOCK:
        w = list(_stale_retry_outcomes)[-100:]
    if not w:
        return float(min(base, budget)) if budget > 0 else base
    sr = sum(w) / len(w)
    adj = base + (budget - base) * sr
    return float(max(0.0001, min(budget, adj)))


def negative_pool_resolve_action_params(
    action: str,
    params: dict[str, Any],
    pool: list[dict[str, Any]],
    check_round: int,
) -> tuple[bool, str]:
    if not pool:
        return False, "clean"
    fp = params_fingerprint(str(action), dict(params))
    for e in pool:
        if e.get("fp") != fp:
            continue
        if not negative_entry_stale(e, check_round):
            if firm_block_by_confidence(e):
                return True, "block_firm"
            return False, "clean"
        if random.random() <= severity_adjusted_retry_prob(e, effective_stale_retry_prob()):
            return False, "stale_retry"
        return True, "block_firm"
    return False, "clean"


def negative_pool_blocks_action_params(
    action: str,
    params: dict[str, Any],
    pool: list[dict[str, Any]],
    check_round: int,
) -> bool:
    blocked, _tag = negative_pool_resolve_action_params(action, params, pool, check_round)
    return blocked


def negative_pool_record(
    pool: list[dict[str, Any]],
    current: dict[str, Any],
    hard_reason: str,
    *,
    tavc_round: int,
    metrics: dict[str, Any] | None = None,
) -> tuple[str, int] | None:
    if not negative_pool_enabled():
        return None
    if "hard_fail" not in hard_reason and "likes" not in hard_reason and "ctr" not in hard_reason:
        return None
    act = str(current.get("action") or "")
    prm = dict(current.get("params") or {})
    fp = params_fingerprint(act, prm)
    ts = storage.utc_now_iso()
    sev = classify_failure_severity(hard_reason, metrics)
    for e in pool:
        if e.get("fp") == fp:
            e["last_confirmed_round"] = int(tavc_round)
            e["strike_count"] = int(e.get("strike_count", 1)) + 1
            e["updated_at"] = ts
            e["hard_reason"] = hard_reason[:500]
            e["failure_severity"] = sev
            e["variant_id"] = current.get("variant_id", e.get("variant_id"))
            return (fp, int(e["strike_count"]))
    pool.append(
        {
            "variant_id": current.get("variant_id"),
            "action": act,
            "params": prm,
            "hard_reason": hard_reason[:500],
            "failure_severity": sev,
            "fp": fp,
            "created_at": ts,
            "updated_at": ts,
            "last_confirmed_round": int(tavc_round),
            "strike_count": 1,
        }
    )
    mx = negative_pool_max()
    while len(pool) > mx:
        pool.pop(0)
    return (fp, 1)


def extract_latency_ms(envelope: dict[str, Any]) -> float:
    try:
        oc = envelope.get("openclaw") if isinstance(envelope.get("openclaw"), dict) else None
        if not oc:
            return 0.0
        m = oc.get("metrics") if isinstance(oc.get("metrics"), dict) else {}
        v = m.get("latency_ms")
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def compute_survival_by_strike(
    strike_events: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
) -> dict[str, Any]:
    verifies: list[tuple[int, bool]] = []
    for s in trajectory:
        if not isinstance(s, dict) or s.get("phase") != "verify":
            continue
        verifies.append((int(s.get("attempt") or 0), bool(s.get("pass"))))
    buckets: dict[str, dict[str, int]] = {}
    for ev in strike_events:
        k = str(int(ev["strike_after"]))
        b = buckets.setdefault(k, {"n": 0, "later_success": 0})
        b["n"] += 1
        att = int(ev.get("attempt") or 0)
        if any(p for a, p in verifies if a > att and p):
            b["later_success"] += 1
    out: dict[str, Any] = {}
    for k, b in buckets.items():
        n = int(b["n"])
        ls = int(b["later_success"])
        out[k] = {
            "n": n,
            "later_success": ls,
            "negative_sample_survival_rate": (ls / n) if n else None,
        }
    return out


def merge_negative_pool_global_aggregate(session_summary: dict[str, Any]) -> None:
    path = negative_obs_aggregate_path()
    with _NEG_OBS_AGG_LOCK:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data: dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = {}
        totals = data.setdefault("strike_survival_totals", {})
        surv = session_summary.get("negative_sample_survival_rate_by_strike") or {}
        for k, row in surv.items():
            if not isinstance(row, dict):
                continue
            t = totals.setdefault(str(k), {"n": 0, "later_success": 0})
            t["n"] += int(row.get("n") or 0)
            t["later_success"] += int(row.get("later_success") or 0)
        stale_g = data.setdefault(
            "stale_retry_global",
            {"attempted": 0, "success": 0, "latency_ms_total": 0.0},
        )
        stale_g["attempted"] += int(session_summary.get("stale_retry_attempted") or 0)
        stale_g["success"] += int(session_summary.get("stale_retry_success") or 0)
        stale_g["latency_ms_total"] = float(stale_g.get("latency_ms_total") or 0.0) + float(
            session_summary.get("stale_retry_compute_cost_ms") or 0.0
        )
        data["updated_at"] = storage.utc_now_iso()
        data["tasks_merged"] = int(data.get("tasks_merged") or 0) + 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def finalize_obs_negative_pool(
    trajectory: list[dict[str, Any]],
    strike_events: list[dict[str, Any]],
    obs: dict[str, Any],
) -> dict[str, Any]:
    survival = compute_survival_by_strike(strike_events, trajectory)
    att = int(obs.get("stale_retry_attempted") or 0)
    ok = int(obs.get("stale_retry_success") or 0)
    return {
        "negative_sample_survival_rate_by_strike": survival,
        "stale_retry_attempted": att,
        "stale_retry_success": ok,
        "stale_retry_success_rate": (ok / att) if att else None,
        "stale_retry_compute_cost_ms": float(obs.get("stale_retry_latency_ms_total") or 0.0),
        "verify_trace_count": len(obs.get("verify_traces") or []),
    }
