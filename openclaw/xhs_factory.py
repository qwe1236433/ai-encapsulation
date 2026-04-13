"""
小红书「流量内容工厂」：挖掘 → 二创 → 预测。

当前为可替换的模拟实现：真实数据接入时只需改 `_fetch`（及下游解析）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

import minimax_client
import prompt_store

# --- 可替换：真实话题下样本抓取 ---------------------------------------------


def _factory_use_minimax() -> bool:
    if not minimax_client.minimax_configured():
        return False
    v = (os.environ.get("OPENCLAW_MINIMAX_FACTORY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _fetch(topic: str, sample_size: int) -> list[dict[str, Any]]:
    """
    占位：未来接 XHS API / 爬虫。返回若干条带 title/body_hint/like_proxy 的样本。
    """
    n = max(3, min(48, int(sample_size)))
    seed = int(hashlib.sha256((topic or "").encode("utf-8")).hexdigest()[:8], 16)
    sop_pool = ["对照式", "递进式", "悬念前置", "清单体", "故事复盘"]
    emotion_pool = ["共鸣", "焦虑缓解", "爽感", "好奇", "身份认同"]
    out: list[dict[str, Any]] = []
    for i in range(n):
        s = (seed + i * 17) % 997
        out.append(
            {
                "title_hint": f"[sim-{i}] {topic[:24] or '话题'} 切片",
                "body_hint": f"结构{(s % 3) + 1}：钩子→展开→总结",
                "like_proxy": 200 + (s % 800),
                "sop_tag": sop_pool[s % len(sop_pool)],
                "emotion_tag": emotion_pool[s % len(emotion_pool)],
            }
        )
    return out


def _default_case_library() -> list[dict[str, Any]]:
    raw = (os.environ.get("XHS_FACTORY_CASE_LIBRARY_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    return [
        {"label": "ref_A", "viral_sop": "对照式", "avg_score": 0.72, "tags": ["干货", "对比"]},
        {"label": "ref_B", "viral_sop": "递进式", "avg_score": 0.68, "tags": ["成长", "步骤"]},
        {"label": "ref_C", "viral_sop": "悬念前置", "avg_score": 0.65, "tags": ["好奇", "反转"]},
    ]


def _norm_gene(gene_sop: Any) -> dict[str, Any]:
    if isinstance(gene_sop, dict):
        return {
            "viral_sop": str(gene_sop.get("viral_sop") or "对照式")[:32],
            "core_hook": str(gene_sop.get("core_hook") or "")[:200],
            "target_emotion": str(gene_sop.get("target_emotion") or "共鸣")[:32],
        }
    if isinstance(gene_sop, str) and gene_sop.strip():
        try:
            obj = json.loads(gene_sop)
            if isinstance(obj, dict):
                return _norm_gene(obj)
        except json.JSONDecodeError:
            pass
        return {"viral_sop": gene_sop.strip()[:32], "core_hook": "", "target_emotion": "共鸣"}
    return {"viral_sop": "对照式", "core_hook": "", "target_emotion": "共鸣"}


def extract_viral_patterns(topic: str, sample_size: int = 12) -> dict[str, Any]:
    """【挖掘】从话题下样本归纳爆款基因（模拟）。"""
    t = (topic or "").strip()[:120]
    n = max(3, min(48, int(sample_size)))
    samples = _fetch(t, n)
    sop_counts: dict[str, int] = {}
    emo_counts: dict[str, int] = {}
    like_sum = 0
    for s in samples:
        sop_counts[s.get("sop_tag") or "对照式"] = sop_counts.get(s.get("sop_tag") or "对照式", 0) + 1
        emo_counts[s.get("emotion_tag") or "共鸣"] = emo_counts.get(s.get("emotion_tag") or "共鸣", 0) + 1
        try:
            like_sum += int(s.get("like_proxy") or 0)
        except (TypeError, ValueError):
            pass
    viral_sop = max(sop_counts, key=lambda k: sop_counts[k])
    target_emotion = max(emo_counts, key=lambda k: emo_counts[k])
    core_hook = f"用「{viral_sop}」承接「{target_emotion}」，前3秒抛出与「{t[:20] or '主题'}」强相关的反差信息"
    out: dict[str, Any] = {
        "topic": t,
        "sample_size_requested": n,
        "sample_size_effective": len(samples),
        "viral_sop": viral_sop,
        "core_hook": core_hook,
        "target_emotion": target_emotion,
        "aggregate_like_proxy": like_sum,
        "notes": "mock extract_viral_patterns — replace _fetch for real feeds",
    }
    if _factory_use_minimax():
        slim = [
            {k: s.get(k) for k in ("title_hint", "like_proxy", "sop_tag", "emotion_tag") if k in s}
            for s in samples[:16]
        ]
        blob = json.dumps(slim, ensure_ascii=False)[:12000]
        pack = prompt_store.load_xhs_prompt("extract_viral_patterns")
        sys_p = str(pack.get("system") or "").strip()
        user_tpl = str(pack.get("user_template") or "").strip()
        user_p = prompt_store.substitute_user_template(
            user_tpl,
            topic=t,
            sample_count=str(len(samples)),
            samples_blob=blob,
        )
        parsed = minimax_client.MiniMaxClient().complete_json(sys_p, user_p)
        if parsed:
            vs = str(parsed.get("viral_sop") or "").strip()
            ch = str(parsed.get("core_hook") or "").strip()
            te = str(parsed.get("target_emotion") or "").strip()
            if len(vs) >= 2:
                out["viral_sop"] = vs[:32]
            if len(ch) >= 4:
                out["core_hook"] = ch[:200]
            if len(te) >= 2:
                out["target_emotion"] = te[:32]
            out["notes"] = "minimax extract_viral_patterns (+ mock sample stats)"
        else:
            out["notes"] = (out["notes"] or "") + " | minimax: no parseable json"
    return out


def recreate_content(original_text: str, gene_sop: Any, style: str = "sharp") -> dict[str, Any]:
    """【二创】按基因 SOP 解构重组原文（模拟）。"""
    orig = (original_text or "").strip()
    if not orig:
        orig = "（空原文占位）"
    g = _norm_gene(gene_sop)
    st = (style or "steady").strip()[:24]
    sop = g["viral_sop"]
    hook = g["core_hook"][:80]
    emo = g["target_emotion"]
    title = f"{sop}·{orig[:28] or '主题'}·{st}表达"
    if len(title) > 58:
        title = title[:56] + "…"
    body = (
        f"【结构:{sop} / 情绪:{emo}】\n"
        f"开头：{hook}\n"
        f"中段：围绕「{orig[:200]}」拆 3 点，每点一句落地动作。\n"
        f"结尾：一句互动提问 + 引导收藏。\n"
        f"（style={st}）"
    )[:2000]
    tags = [sop[:6], emo[:6], st[:6] or "风格", "小红书成长"]
    notes = "mock recreate_content"
    recreated: dict[str, Any] = {"title": title, "body": body, "tags": tags}
    if _factory_use_minimax():
        pack = prompt_store.load_xhs_prompt("recreate_content")
        sys_p = str(pack.get("system") or "").strip()
        lim = pack.get("limits") if isinstance(pack.get("limits"), dict) else {}
        try:
            omax = int(lim.get("original_max_chars", 4000))
        except (TypeError, ValueError):
            omax = 4000
        omax = max(500, min(12000, omax))
        user_p = json.dumps(
            {"original": orig[:omax], "gene_sop": g, "style": st},
            ensure_ascii=False,
        )
        parsed = minimax_client.MiniMaxClient().complete_json(sys_p, user_p)
        if parsed:
            tt = str(parsed.get("title") or "").strip()
            bd = str(parsed.get("body") or "").strip()
            tg = parsed.get("tags")
            if len(tt) >= 2 and len(bd) >= 8:
                tag_list: list[str] = []
                if isinstance(tg, list):
                    tag_list = [str(x).strip()[:24] for x in tg if str(x).strip()]
                if len(tag_list) < 3:
                    tag_list = [sop[:6], emo[:6], st[:6] or "风格", "小红书"]
                recreated = {"title": tt[:80], "body": bd[:2000], "tags": tag_list[:12]}
                notes = "minimax recreate_content"
            else:
                notes = notes + " | minimax: json missing title/body"
        else:
            notes = notes + " | minimax: no parseable json"
    return {
        "original": orig[:5000],
        "gene_sop_echo": g,
        "style_applied": st,
        "recreated": recreated,
        "notes": notes,
    }


def predict_viral_score(
    recreated_text: str,
    gene_sop: Any,
    case_library: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """【预测】对照案例库特征给二创打分（模拟）。"""
    text = (recreated_text or "").strip()
    g = _norm_gene(gene_sop)
    lib = case_library if isinstance(case_library, list) and case_library else _default_case_library()
    ref_scores = [float(x.get("avg_score", 0.6)) for x in lib if isinstance(x, dict)]
    ref_mean = sum(ref_scores) / max(len(ref_scores), 1)
    sop = g["viral_sop"]
    matches = sum(1 for x in lib if isinstance(x, dict) and str(x.get("viral_sop")) == sop)
    base = 0.42 + 0.12 * min(matches, 3) + 0.08 * (1.0 if len(text) > 80 else 0.0)
    h = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF
    jitter = (h - 0.5) * 0.08
    predicted = max(0.05, min(0.95, base + 0.15 * ref_mean + jitter))
    confidence = max(0.35, min(0.92, 0.55 + 0.25 * (len(text) / 500.0)))
    risk = "length_short" if len(text) < 40 else "sop_mismatch_low" if matches == 0 else "mock_low_risk"
    if not re.search(r"[\u4e00-\u9fff]", text):
        risk = "no_cjk_body"
        predicted *= 0.85
    notes = "mock predict_viral_score — wire real model or platform signals"
    if _factory_use_minimax():
        pack = prompt_store.load_xhs_prompt("predict_viral_score")
        sys_p = str(pack.get("system") or "").strip()
        lim = pack.get("limits") if isinstance(pack.get("limits"), dict) else {}
        try:
            tmax = int(lim.get("recreated_text_max_chars", 6000))
        except (TypeError, ValueError):
            tmax = 6000
        try:
            lib_n = int(lim.get("case_library_max_items", 20))
        except (TypeError, ValueError):
            lib_n = 20
        tmax = max(500, min(32000, tmax))
        lib_n = max(1, min(50, lib_n))
        user_p = json.dumps(
            {
                "recreated_text": text[:tmax],
                "gene_sop": g,
                "case_library": lib[:lib_n],
            },
            ensure_ascii=False,
        )
        parsed = minimax_client.MiniMaxClient().complete_json(sys_p, user_p)
        if parsed:
            try:
                ps = float(parsed.get("predicted_score"))
                cf = float(parsed.get("confidence"))
                rf = str(parsed.get("risk_factor") or risk)
                if 0.0 <= ps <= 1.0 and 0.0 <= cf <= 1.0:
                    return {
                        "predicted_score": round(ps, 4),
                        "confidence": round(cf, 4),
                        "risk_factor": rf[:120],
                        "reference_mean": round(ref_mean, 4),
                        "case_library_size": len(lib),
                        "notes": "minimax predict_viral_score",
                    }
                notes = notes + " | minimax: scores out of range"
            except (TypeError, ValueError):
                notes = notes + " | minimax: invalid numeric fields"
        else:
            notes = notes + " | minimax: no parseable json"
    return {
        "predicted_score": round(predicted, 4),
        "confidence": round(confidence, 4),
        "risk_factor": risk,
        "reference_mean": round(ref_mean, 4),
        "case_library_size": len(lib),
        "notes": notes,
    }
