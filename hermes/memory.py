"""Mid-term / Long-term 记忆：案例库 TTL/环境指纹、检索加权、认知冲突熔断与负样本联动。"""

from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Any

import negative_pool
import settings
import storage

_STYLE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("焦虑", "紧迫", "崩盘", "内卷", "慌", "危机"), "anxiety"),
    (("治愈", "松弛", "躺平", "安心", "疗愈", "慢生活"), "healing"),
    (("搞钱", "副业", "暴富", "逆袭", "翻盘"), "hustle"),
)


def infer_style_tags(goal: str) -> list[str]:
    g = (goal or "").strip()
    if not g:
        return []
    tags: list[str] = []
    for keys, tag in _STYLE_RULES:
        if any(k in g for k in keys):
            tags.append(tag)
    return sorted(set(tags))


def market_fingerprint(goal: str) -> dict[str, Any]:
    """当前任务的环境快照：爆词、目标摘要、风格标签与稳定哈希（供案例对齐与冲突提示）。"""
    g = (goal or "").strip()
    kw = settings.traffic_boost_keyword()
    tags = infer_style_tags(g)
    raw = json.dumps(
        {"g": g[:240], "kw": kw, "tags": tags},
        sort_keys=True,
        ensure_ascii=False,
    )
    fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return {
        "boost_keyword": kw,
        "style_tags": tags,
        "fingerprint": fp,
        "goal_excerpt": g[:160],
    }


def _hours_since_archived_iso(iso_s: str) -> float | None:
    sec = negative_pool.parse_iso_age_sec(iso_s)
    if sec is None:
        return None
    return max(0.0, sec / 3600.0)


def freshness_weight_from_hours(hours: float | None) -> float:
    if hours is None:
        return 1.0
    alpha = settings.case_library_decay_alpha()
    return 1.0 / (1.0 + alpha * max(0.0, hours))


def card_time_weight(card: dict[str, Any]) -> float:
    h = _hours_since_archived_iso(str(card.get("archived_at") or ""))
    return freshness_weight_from_hours(h)


def card_effective_market_context(card: dict[str, Any]) -> dict[str, Any]:
    mc = card.get("market_context")
    if isinstance(mc, dict) and mc.get("fingerprint"):
        return mc
    g = str(card.get("goal") or "")
    return market_fingerprint(g)


def style_tag_jaccard(old_tags: list[str], cur_tags: list[str]) -> float | None:
    sa = {str(x) for x in (old_tags or []) if x}
    sb = {str(x) for x in (cur_tags or []) if x}
    if not sa and not sb:
        return None
    u = sa | sb
    if not u:
        return None
    return len(sa & sb) / len(u)


def market_context_align(old_mc: dict[str, Any], cur_mc: dict[str, Any]) -> tuple[bool, float]:
    """
    是否将案例与当前任务视为「同一类流量环境」（用于 cognitive streak / 检索加权）。
    strict：指纹哈希全等（可选要求爆词一致）；fuzzy：风格标签 Jaccard ≥ 阈值。
    """
    old_fp = str(old_mc.get("fingerprint") or "")
    cur_fp = str(cur_mc.get("fingerprint") or "")
    ot = [str(x) for x in (old_mc.get("style_tags") or []) if x]
    ct = [str(x) for x in (cur_mc.get("style_tags") or []) if x]
    kw_o = str(old_mc.get("boost_keyword") or "")
    kw_c = str(cur_mc.get("boost_keyword") or "")
    req_kw = settings.case_fingerprint_require_keyword_match()
    kw_ok = True
    if req_kw and (kw_o or kw_c):
        kw_ok = kw_o == kw_c

    mode = settings.case_fingerprint_match_mode()
    if mode == "strict":
        same = bool(old_fp and cur_fp and old_fp == cur_fp) and kw_ok
        return same, 1.0 if same else 0.0

    if not kw_ok:
        j = style_tag_jaccard(ot, ct)
        return False, float(j or 0.0)

    thr = settings.case_fingerprint_jaccard_min()
    if old_fp and cur_fp and old_fp == cur_fp:
        j = style_tag_jaccard(ot, ct)
        return True, float(j if j is not None else 1.0)

    j = style_tag_jaccard(ot, ct)
    if j is None:
        return False, 0.0
    return (j >= thr), float(j)


def _ensure_memory_health(card: dict[str, Any]) -> dict[str, Any]:
    mh = card.get("memory_health")
    if not isinstance(mh, dict):
        mh = {}
    streaks = mh.get("fp_fail_streak")
    if not isinstance(streaks, dict):
        streaks = {}
    clean: dict[str, int] = {}
    for k, v in streaks.items():
        try:
            clean[str(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    mh["fp_fail_streak"] = clean
    mh.setdefault("deprecated", False)
    if "deprecated_reason" not in mh:
        mh["deprecated_reason"] = None
    if "deprecated_at" not in mh:
        mh["deprecated_at"] = None
    card["memory_health"] = mh
    return mh


def _keyword_match(goal: str, card: dict[str, Any]) -> bool:
    g = (goal or "").strip()
    if not g:
        return False
    cg = str(card.get("goal") or "").strip()
    if not cg:
        return False
    g_low, cg_low = g.lower(), cg.lower()
    if len(g) >= 2 and (g in cg or cg in g or g_low in cg_low or cg_low in g_low):
        return True
    for i in range(len(g) - 1):
        if g[i : i + 2] in cg:
            return True
    return False


def _tags_label(tags: list[str]) -> str:
    if not tags:
        return "neutral"
    return ",".join(tags)


def _append_deprecation_to_pool(
    pool: list[dict[str, Any]] | None,
    card: dict[str, Any],
    *,
    tavc_round: int,
    current_fp: str,
) -> None:
    if pool is None or not settings.case_deprecate_to_negative_pool():
        return
    if not negative_pool.negative_pool_enabled():
        return
    cf = card.get("candidate_formula") if isinstance(card.get("candidate_formula"), dict) else {}
    act = str(cf.get("action") or "")
    prm = dict(cf.get("params") or {}) if isinstance(cf.get("params"), dict) else {}
    if not act:
        return
    fp = negative_pool.params_fingerprint(act, prm)
    ts = storage.utc_now_iso()
    reason = f"cognitive_drift_deprecated_case;case_fp_mismatch_or_stale;ctx_fp={current_fp[:16]}"
    for e in pool:
        if e.get("fp") == fp:
            e["strike_count"] = int(e.get("strike_count", 1)) + 1
            e["last_confirmed_round"] = int(tavc_round)
            e["updated_at"] = ts
            e["hard_reason"] = reason[:500]
            e["failure_severity"] = "moderate"
            return
    pool.append(
        {
            "variant_id": cf.get("variant_id"),
            "action": act,
            "params": prm,
            "hard_reason": reason[:500],
            "failure_severity": "moderate",
            "fp": fp,
            "created_at": ts,
            "updated_at": ts,
            "last_confirmed_round": int(tavc_round),
            "strike_count": 1,
        }
    )
    mx = negative_pool.negative_pool_max()
    while len(pool) > mx:
        pool.pop(0)


class MemoryManager:
    """SESSIONS 下的 case_library + 负样本池操作的统一入口。"""

    def __init__(self, base: str | None = None) -> None:
        self._base = (base or storage.sessions_dir()).strip()

    def case_library_dir(self) -> str:
        return os.path.join(self._base, "case_library")

    def _card_path(self, task_id: str) -> str:
        return os.path.join(self.case_library_dir(), f"{task_id}.json")

    def load_case_card(self, task_id: str) -> dict[str, Any] | None:
        tid = str(task_id or "").strip()
        if not tid or not storage.safe_task_filename(tid):
            return None
        path = self._card_path(tid)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                row = json.load(f)
            if isinstance(row, dict):
                row["_card_path"] = path
                return row
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def save_case_card(self, card: dict[str, Any]) -> None:
        path = str(card.get("_card_path") or "").strip()
        if not path:
            tid = str(card.get("task_id") or "").strip()
            if tid and storage.safe_task_filename(tid):
                path = self._card_path(tid)
                card["_card_path"] = path
        if not path:
            return
        out = {k: v for k, v in card.items() if k != "_card_path"}
        storage.atomic_write_session(path, out)

    def list_archived_cases(self) -> list[dict[str, Any]]:
        """读取 case_library 下全部案例卡片（不含子目录）。"""
        d = self.case_library_dir()
        if not os.path.isdir(d):
            return []
        out: list[dict[str, Any]] = []
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(d, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    row = json.load(f)
                if isinstance(row, dict):
                    row["_card_path"] = path
                    out.append(row)
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def retrieve_cases_for_goal(self, goal: str, *, max_cases: int = 2) -> list[dict[str, Any]]:
        """
        S1：goal 关键词优先；按 freshness_weight × 关键词加成排序；剔除 deprecated 与硬 TTL 过期卡片。
        """
        g = (goal or "").strip()
        cards = self.list_archived_cases()
        if not cards:
            return []
        max_cases = max(1, min(8, max_cases))
        max_age = settings.case_library_max_age_hours()
        scored: list[tuple[float, dict[str, Any]]] = []
        for c in cards:
            _ensure_memory_health(c)
            if bool(c.get("memory_health", {}).get("deprecated")):
                continue
            archived_at = str(c.get("archived_at") or "")
            hours = _hours_since_archived_iso(archived_at)
            if max_age > 0 and hours is not None and hours > max_age:
                continue
            w_time = freshness_weight_from_hours(hours)
            kw_boost = 2.0 if (g and _keyword_match(g, c)) else 1.0
            cur_mc = market_fingerprint(g)
            aligned, sim = market_context_align(card_effective_market_context(c), cur_mc)
            env_boost = 1.35 if aligned else (1.0 + 0.25 * sim)
            score = w_time * kw_boost * env_boost
            if score <= 0:
                continue
            scored.append((score, c))
        if not scored:
            return []
        scored.sort(key=lambda x: x[0], reverse=True)
        ordered = [c for _, c in scored]
        if not g:
            random.shuffle(ordered)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in ordered:
            key = str(c.get("task_id") or c.get("_card_path") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= max_cases:
                break
        return out

    def archive_session(self, session: dict[str, Any]) -> str | None:
        """成功任务 → 案例卡片（含 market_context / memory_health）。"""
        if not session.get("final_pass"):
            return None
        tid = str(session.get("task_id") or "")
        if not tid:
            return None
        d = self.case_library_dir()
        os.makedirs(d, exist_ok=True)
        goal_s = str(session.get("goal") or "")
        card = {
            "task_id": tid,
            "archived_at": storage.utc_now_iso(),
            "goal": session.get("goal"),
            "candidate_formula": session.get("candidate_formula"),
            "trajectory_len": len(session.get("trajectory") or []),
            "market_context": market_fingerprint(goal_s),
            "memory_health": {
                "fp_fail_streak": {},
                "deprecated": False,
                "deprecated_reason": None,
                "deprecated_at": None,
            },
        }
        path = self._card_path(tid)
        storage.atomic_write_session(path, card)
        return path

    def apply_verify_feedback(
        self,
        retrieved_case_ids: list[str],
        market_ctx: dict[str, Any],
        *,
        passed: bool,
        tavc_round: int,
        pool: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Verify 后更新案例健康度：成功清零当前指纹下的失败 streak；失败递增至阈值则 deprecated并可写入负样本池。
        返回 cognitive_hint / force_explore 供 S4。
        """
        fp = str((market_ctx or {}).get("fingerprint") or "")
        ids = [str(x).strip() for x in (retrieved_case_ids or []) if str(x).strip()]
        if not fp or not ids:
            return {"cognitive_hint": "", "force_explore": False, "deprecated_task_ids": []}

        threshold = settings.case_cognitive_conflict_threshold()
        deprecated: list[str] = []
        mismatch_lines: list[str] = []

        for tid in ids:
            card = self.load_case_card(tid)
            if not card:
                continue
            mh = _ensure_memory_health(card)
            if mh.get("deprecated"):
                continue

            old_mc = card_effective_market_context(card)
            old_tags = list(old_mc.get("style_tags") or [])
            cur_tags = list((market_ctx or {}).get("style_tags") or [])
            aligned, sim = market_context_align(old_mc, market_ctx or {})

            if passed:
                streaks = mh.get("fp_fail_streak") or {}
                if isinstance(streaks, dict) and fp in streaks:
                    streaks.pop(fp, None)
                    mh["fp_fail_streak"] = streaks
                self.save_case_card(card)
                continue

            if not aligned:
                mismatch_lines.append(
                    f"案例 {tid} 与当前环境对齐不足（标签 {_tags_label(old_tags)} vs {_tags_label(cur_tags)}，"
                    f"Jaccard={sim:.2f}，模式={settings.case_fingerprint_match_mode()}）。"
                    "勿再沿用该范式，优先考虑对向风格或换 action。"
                )
                continue

            streaks = mh.setdefault("fp_fail_streak", {})
            if not isinstance(streaks, dict):
                streaks = {}
                mh["fp_fail_streak"] = streaks
            n = int(streaks.get(fp, 0)) + 1
            streaks[fp] = n
            if n >= threshold:
                mh["deprecated"] = True
                mh["deprecated_reason"] = f"cognitive_conflict_fp:{fp[:12]}_fails_{n}"
                mh["deprecated_at"] = storage.utc_now_iso()
                deprecated.append(tid)
                _append_deprecation_to_pool(pool, card, tavc_round=tavc_round, current_fp=fp)

            self.save_case_card(card)

        hint_parts = list(mismatch_lines)
        force = bool(deprecated)
        if deprecated:
            hint_parts.append(
                "警告：在【相同市场指纹】下引用案例后 Verify 已连续失败达到阈值，系统已将案例 "
                f"{', '.join(deprecated)} 降级并写入负样本池（若开启）。请立即抛弃对该案例的微调执念，"
                "改走探索分支（换 action 或相反风格）。"
            )
        return {
            "cognitive_hint": " ".join(hint_parts).strip(),
            "force_explore": force,
            "deprecated_task_ids": deprecated,
        }

    @staticmethod
    def update_negative_pool(
        pool: list[dict[str, Any]],
        current: dict[str, Any],
        hard_reason: str,
        *,
        tavc_round: int,
        metrics: dict[str, Any] | None = None,
    ):
        return negative_pool.negative_pool_record(
            pool, current, hard_reason, tavc_round=tavc_round, metrics=metrics
        )

    @staticmethod
    def get_profile() -> dict[str, Any]:
        """用户认知画像占位：可接 YAML/DB/外部 Profile服务。"""
        return {"source": "stub", "note": "wire profile provider when ready"}


def retrieve_cases_for_goal(goal: str, *, max_cases: int = 2) -> list[dict[str, Any]]:
    """模块级便捷函数，供 brain 引用。"""
    return MemoryManager().retrieve_cases_for_goal(goal, max_cases=max_cases)
