"""
openclaw/xhs_diagnose.py
========================
小红书笔记内容诊断引擎（L3 定位，非预测工具）。

核心原则：
  1. evidence 主体用描述统计（历史真实占比差），模型系数和 Bootstrap CI
     作为附注。绝不把 logistic 系数误译为"预期提升 x%"。
  2. 只输出经过双稳定性检验（Bootstrap CI 不跨 0 + 时间分段同号≥75%）
     的建议；对消融中被识别为"反向有害"的特征给出"减少"建议。
  3. 引擎只输出机器可读的 action_code + 证据数据，不包含任何面向用户的
     示例文案。示例由 renderer 层按 action_code 查模板库。
  4. 在顶层明确标注赛道限定、AUC、组合效应免责声明，防止误用。

数据源（运行时加载）：
  - 模型系数/CI: research/artifacts/baseline_v2_time.json
  - 稳定性等级: research/artifacts/formula_validation_v2.json
  - 描述统计:   research/features_v2.csv（首次调用时懒计算 + 进程内缓存）

依赖：复用 openclaw.xhs_factory._v2_text_features 做特征抽取，避免重复实现。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from openclaw.xhs_factory import _v2_text_features  # 单一事实源

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASELINE = REPO_ROOT / "research" / "artifacts" / "baseline_v2_time.json"
_DEFAULT_VALIDATION = REPO_ROOT / "research" / "artifacts" / "formula_validation_v2.json"
_DEFAULT_FEATURES_CSV = REPO_ROOT / "research" / "features_v2.csv"

_VERTICAL_NOTICE = (
    "本工具基于健身/减脂赛道 ~741 篇笔记数据训练，其他赛道参考价值有限。"
    "模型时间外 hold-out AUC=0.532，仅能作为单条规则级的写作提示，不具备整体爆款预测能力。"
)
_COMBO_DISCLAIMER = (
    "各条建议的效应基于模型线性独立假设；同时修改多条后的综合效果未经 A/B 实验验证。"
)

Severity = Literal["high", "medium", "info"]


# ─────────────────────────────────────────────────────────────────────────────
# 描述统计缓存（进程内懒加载）
# ─────────────────────────────────────────────────────────────────────────────
_stats_cache: dict[str, Any] | None = None
_stats_cache_key: tuple[str, float] | None = None
_stats_lock = Lock()


def _compute_descriptive_stats(features_csv: Path) -> dict[str, Any]:
    """从 features_v2.csv 计算每条诊断规则所需的占比差描述统计。"""
    buckets: dict[str, dict[str, list[int]]] = {
        "q":   {"with_q":    [], "without_q": []},      # title_has_question
        "em":  {"emoji0":    [], "emoji_ge1": []},      # title_emoji_count
        "ht":  {"ht_ge2":    [], "ht_lt2":   []},       # title_hashtag_count
    }
    with open(features_csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                y = int(row["y_rule"])
                has_q = int(float(row["title_has_question"]))
                em = int(float(row["title_emoji_count"]))
                ht = int(float(row["title_hashtag_count"]))
            except (KeyError, ValueError, TypeError):
                continue
            (buckets["q"]["with_q"] if has_q else buckets["q"]["without_q"]).append(y)
            (buckets["em"]["emoji0"] if em == 0 else buckets["em"]["emoji_ge1"]).append(y)
            (buckets["ht"]["ht_ge2"] if ht >= 2 else buckets["ht"]["ht_lt2"]).append(y)

    def pack(arr: list[int]) -> dict[str, float | int]:
        n = len(arr)
        pos = sum(arr)
        rate = (pos / n) if n else 0.0
        return {"n": n, "pos": pos, "rate": round(rate, 4)}

    def diff(a: dict[str, Any], b: dict[str, Any]) -> float:
        return round((a["rate"] - b["rate"]) * 100, 1)  # 百分点

    q_with = pack(buckets["q"]["with_q"])
    q_without = pack(buckets["q"]["without_q"])
    em0 = pack(buckets["em"]["emoji0"])
    em_ge1 = pack(buckets["em"]["emoji_ge1"])
    ht_ge2 = pack(buckets["ht"]["ht_ge2"])
    ht_lt2 = pack(buckets["ht"]["ht_lt2"])

    total_n = q_with["n"] + q_without["n"]
    return {
        "total_n": total_n,
        "title_has_question": {
            "with":    q_with,
            "without": q_without,
            "abs_diff_pp": diff(q_with, q_without),
        },
        "title_emoji_count": {
            "emoji0":    em0,
            "emoji_ge1": em_ge1,
            "abs_diff_pp": diff(em_ge1, em0),
        },
        "title_hashtag_count": {
            "ht_ge2": ht_ge2,
            "ht_lt2": ht_lt2,
            "abs_diff_pp": diff(ht_ge2, ht_lt2),
        },
    }


def _load_descriptive_stats(features_csv: Path) -> dict[str, Any] | None:
    """读取/缓存描述统计；CSV 不存在时返回 None（引擎降级为只给附注）。"""
    global _stats_cache, _stats_cache_key
    if not features_csv.is_file():
        return None
    try:
        mtime = features_csv.stat().st_mtime
    except OSError:
        return None
    key = (str(features_csv), mtime)
    with _stats_lock:
        if _stats_cache is not None and _stats_cache_key == key:
            return _stats_cache
        stats = _compute_descriptive_stats(features_csv)
        _stats_cache = stats
        _stats_cache_key = key
        return stats


# ─────────────────────────────────────────────────────────────────────────────
# 模型元数据加载
# ─────────────────────────────────────────────────────────────────────────────
def _load_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _coef_from_baseline(baseline: dict[str, Any] | None, name: str) -> float | None:
    if not baseline:
        return None
    coefs = baseline.get("coefficients") or {}
    v = coefs.get(name)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bootstrap_entry(validation: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not validation:
        return None
    for item in (validation.get("bootstrap") or {}).get("features", []):
        if item.get("name") == name:
            return item
    return None


def _ts_entry(validation: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not validation:
        return None
    ts = validation.get("time_segment_stability") or {}
    if ts.get("skipped"):
        return None
    for item in ts.get("features", []):
        if item.get("name") == name:
            return item
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 诊断结果数据结构
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Suggestion:
    action_code: str
    severity: Severity
    title: str                    # 诊断项简短名（非用户文案）
    descriptive: dict[str, Any]   # 描述统计主体
    user_state: dict[str, Any] = field(default_factory=dict)
    # ^^^ 用户当前的真实状态（个性化报告的关键），例如：
    # { "current_value": 10, "target_value": 1, "feature": "title_hashtag_count",
    #   "human": "你当前标题有 10 个 hashtag" }
    coefficient: dict[str, Any] | None = None
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_code": self.action_code,
            "severity": self.severity,
            "title": self.title,
            "descriptive": self.descriptive,
            "user_state": self.user_state,
            "coefficient": self.coefficient,
            "caveats": self.caveats,
        }


@dataclass
class DiagnoseResult:
    version: str
    model_ref: str
    vertical_notice: str
    combo_disclaimer: str
    input_features: dict[str, float]
    suggestions: list[Suggestion]
    info_notes: list[dict[str, Any]]
    generated_at_utc: str
    original_input: dict[str, str] = field(default_factory=dict)
    # ^^^ 保留用户原始 title/body（短 body 会 truncate），供 renderer 做个性化示例

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "model_ref": self.model_ref,
            "vertical_notice": self.vertical_notice,
            "combo_disclaimer": self.combo_disclaimer,
            "input_features": self.input_features,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "info_notes": self.info_notes,
            "generated_at_utc": self.generated_at_utc,
            "original_input": self.original_input,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 规则生成器
# ─────────────────────────────────────────────────────────────────────────────
def _mk_coef_block(
    feat: str, baseline: dict[str, Any] | None, validation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    coef = _coef_from_baseline(baseline, feat)
    b = _bootstrap_entry(validation, feat)
    ts = _ts_entry(validation, feat)
    if coef is None and b is None:
        return None
    out: dict[str, Any] = {"feature": feat, "model_coef": coef}
    if b:
        out["bootstrap"] = {
            "mean": round(b["coef_mean"], 4),
            "ci95": [round(b["ci95_lo"], 4), round(b["ci95_hi"], 4)],
            "same_sign_ratio": b["same_sign_ratio"],
            "verdict": b["verdict"],
            "n_iter": (validation or {}).get("bootstrap", {}).get("n_iter"),
        }
    if ts:
        out["time_segment"] = {
            "sign_consistency": ts["sign_consistency"],
            "verdict": ts["verdict"],
        }
    return out


def _count_questions(title: str) -> int:
    """全角/半角问号总数。"""
    return sum(1 for ch in title if ch in ("?", "？"))


def _extract_hashtags(title: str) -> list[str]:
    """抓标题里所有 `#xxx` 的 token（不跨空白、中文标签也允许）。"""
    import re
    return re.findall(r"#[^\s#＃]+", title)


def _rule_title_has_question(
    title: str, body: str,
    feats: dict[str, float], stats: dict[str, Any] | None,
    baseline: dict[str, Any] | None, validation: dict[str, Any] | None,
) -> Suggestion | None:
    if feats.get("title_has_question", 0) != 1:
        return None
    desc: dict[str, Any] = {"type": "historical_ratio_diff"}
    caveats: list[str] = []
    if stats:
        s = stats["title_has_question"]
        desc.update({
            "group_a": {"label": "标题带问号", "n": s["with"]["n"], "rate": s["with"]["rate"]},
            "group_b": {"label": "标题不带问号", "n": s["without"]["n"], "rate": s["without"]["rate"]},
            "abs_diff_pp": s["abs_diff_pp"],
        })
    else:
        desc["unavailable_reason"] = "features_v2.csv not found; fall back to coef-only"
        caveats.append("缺少描述统计；仅提供模型系数附注")
    q_count = _count_questions(title)
    return Suggestion(
        action_code="REMOVE_TITLE_QUESTION_MARK",
        severity="high",
        title="标题删除问号",
        descriptive=desc,
        user_state={
            "feature": "title_has_question",
            "current_value": 1,
            "target_value": 0,
            "question_count": q_count,
            "human": (
                f"你当前标题里有 {q_count} 个问号"
                if q_count != 1 else "你当前标题带 1 个问号"
            ),
        },
        coefficient=_mk_coef_block("title_has_question", baseline, validation),
        caveats=caveats,
    )


def _rule_title_emoji_count(
    title: str, body: str,
    feats: dict[str, float], stats: dict[str, Any] | None,
    baseline: dict[str, Any] | None, validation: dict[str, Any] | None,
) -> Suggestion | None:
    if feats.get("title_emoji_count", 0) != 0:
        return None  # ≥1 不出建议（1→2 跃迁已平）
    desc: dict[str, Any] = {"type": "historical_ratio_diff", "extra": {"recommend_add": 1}}
    caveats = ["数据显示 emoji 0→1 有跃迁（+7.4pp），1→2 基本持平；本建议仅对 0 个 emoji 时触发"]
    if stats:
        s = stats["title_emoji_count"]
        desc.update({
            "group_a": {"label": "标题无 emoji", "n": s["emoji0"]["n"], "rate": s["emoji0"]["rate"]},
            "group_b": {"label": "标题含 ≥1 个 emoji", "n": s["emoji_ge1"]["n"], "rate": s["emoji_ge1"]["rate"]},
            "abs_diff_pp": s["abs_diff_pp"],
        })
    else:
        desc["unavailable_reason"] = "features_v2.csv not found"
        caveats.append("缺少描述统计；仅提供模型系数附注")
    return Suggestion(
        action_code="ADD_ONE_TITLE_EMOJI",
        severity="high",
        title="标题加入 1 个 emoji",
        descriptive=desc,
        user_state={
            "feature": "title_emoji_count",
            "current_value": 0,
            "target_value": 1,
            "human": "你当前标题 0 个 emoji",
        },
        coefficient=_mk_coef_block("title_emoji_count", baseline, validation),
        caveats=caveats,
    )


def _rule_title_hashtag_count(
    title: str, body: str,
    feats: dict[str, float], stats: dict[str, Any] | None,
    baseline: dict[str, Any] | None, validation: dict[str, Any] | None,
) -> Suggestion | None:
    if feats.get("title_hashtag_count", 0) < 2:
        return None
    current = int(feats["title_hashtag_count"])
    hashtags = _extract_hashtags(title)
    # 若 `#` 计数与抓到的 token 数不等（用户写法不规范），仍以 `#` 计数为准
    desc: dict[str, Any] = {
        "type": "historical_ratio_diff",
        "extra": {
            "current_count": current,
            "recommend_keep": 1,
        },
    }
    caveats: list[str] = []
    if stats:
        s = stats["title_hashtag_count"]
        desc.update({
            "group_a": {"label": "标题 ≥2 个 hashtag", "n": s["ht_ge2"]["n"], "rate": s["ht_ge2"]["rate"]},
            "group_b": {"label": "标题 <2 个 hashtag", "n": s["ht_lt2"]["n"], "rate": s["ht_lt2"]["rate"]},
            "abs_diff_pp": s["abs_diff_pp"],
        })
        if s["ht_ge2"]["n"] < 50:
            caveats.append(
                f"描述统计 A 组样本偏小（n={s['ht_ge2']['n']}），结论稳健性有限"
            )
    else:
        desc["unavailable_reason"] = "features_v2.csv not found"
    # 消融证据（如存在）：auc_drop 为负意味着"剔除后反而升高"，需要人读友好地表达
    if validation:
        abl = (validation.get("feature_ablation") or {}).get("ablation") or []
        ab_row = next((r for r in abl if r.get("removed") == "title_hashtag_count"), None)
        if ab_row and ab_row.get("auc_drop") is not None:
            drop = float(ab_row["auc_drop"])
            auc_wo = ab_row.get("auc_without")
            if drop < 0:
                caveats.append(
                    f"消融实验：剔除该特征后 hold-out AUC 由基线上升 {abs(drop):.4f}"
                    f"（→ {auc_wo}），提示该特征对当前模型是反向贡献"
                )
            else:
                caveats.append(
                    f"消融实验：剔除该特征后 hold-out AUC 下降 {drop:.4f}"
                    f"（→ {auc_wo}）"
                )
    return Suggestion(
        action_code="REDUCE_TITLE_HASHTAG_TO_ONE",
        severity="medium",
        title="标题减少 hashtag 至 1 个",
        descriptive=desc,
        user_state={
            "feature": "title_hashtag_count",
            "current_value": current,
            "target_value": 1,
            "hashtags_in_title": hashtags,
            "human": f"你当前标题共 {current} 个 hashtag（{'、'.join(hashtags[:5])}{'…' if len(hashtags) > 5 else ''}）",
        },
        coefficient=_mk_coef_block("title_hashtag_count", baseline, validation),
        caveats=caveats,
    )


def _info_body_len(feats: dict[str, float]) -> dict[str, Any]:
    cur = int(feats.get("body_len", 0))
    return {
        "info_code": "BODY_LEN_WEAK_SIGNAL",
        "title": "正文长度（仅供参考）",
        "current_value": cur,
        "message": (
            f"当前正文 {cur} 字。数据显示正文长度与高赞占比有弱单调正相关"
            "（<100 字 81.0% → >600 字 88.2%），但模型系数量级极小（+0.0021 / 字），"
            "不建议为了凑字数而扩写。"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 对外 API
# ─────────────────────────────────────────────────────────────────────────────
def diagnose(
    title: str,
    body: str,
    *,
    sop_tag: str = "",
    emotion_tag: str = "",
    baseline_path: Path | str | None = None,
    validation_path: Path | str | None = None,
    features_csv: Path | str | None = None,
) -> DiagnoseResult:
    """对一篇笔记（用户自己粘贴的 title + body）输出结构化诊断。

    Parameters
    ----------
    title, body : 笔记标题、正文
    sop_tag, emotion_tag : 可选的赛道/情绪标签（目前不影响诊断输出，保留接口）
    baseline_path : 默认 research/artifacts/baseline_v2_time.json
    validation_path : 默认 research/artifacts/formula_validation_v2.json
    features_csv : 默认 research/features_v2.csv

    Returns
    -------
    DiagnoseResult（.to_dict() 可直接 JSON 序列化）
    """
    bp = Path(baseline_path) if baseline_path else _DEFAULT_BASELINE
    vp = Path(validation_path) if validation_path else _DEFAULT_VALIDATION
    fp = Path(features_csv) if features_csv else _DEFAULT_FEATURES_CSV

    baseline = _load_json_safe(bp)
    validation = _load_json_safe(vp)
    stats = _load_descriptive_stats(fp)

    t = title or ""
    b = body or ""
    feats = _v2_text_features(t, b, sop_tag=sop_tag, emotion_tag=emotion_tag)

    suggestions: list[Suggestion] = []
    for rule in (_rule_title_has_question, _rule_title_emoji_count, _rule_title_hashtag_count):
        s = rule(t, b, feats, stats, baseline, validation)
        if s:
            suggestions.append(s)

    info_notes: list[dict[str, Any]] = [_info_body_len(feats)]

    model_ref = str(bp.relative_to(REPO_ROOT)).replace("\\", "/") if bp.is_file() else str(bp)

    # 保留原文供 renderer 做个性化（body 限长，防止日志/前端过大）
    original_input = {
        "title": t,
        "body": b[:800] + ("…" if len(b) > 800 else ""),
    }

    return DiagnoseResult(
        version="xhs_diagnose_v2_2026-04-17",
        model_ref=model_ref,
        vertical_notice=_VERTICAL_NOTICE,
        combo_disclaimer=_COMBO_DISCLAIMER,
        input_features={k: round(v, 6) for k, v in feats.items()},
        suggestions=suggestions,
        info_notes=info_notes,
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        original_input=original_input,
    )


__all__ = ["diagnose", "DiagnoseResult", "Suggestion"]
