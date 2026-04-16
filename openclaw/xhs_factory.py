"""
小红书「流量内容工厂」：挖掘 → 二创 → 预测。

- 挖掘：默认对样本做 **log1p(点赞)** 加权的标签聚合 + 频次众数对照 + 标签熵 + Top 证据列表（`quantitative`），
  大模型仅可选补充 `formulas`，不再默认覆盖主标签。
- 预测：默认 **linear_clamp_v1** 可复现线性分 + `score_breakdown`；可选读 **`XHS_FACTORY_BASELINE_JSON`**（`train_baseline_v0.py` 产出，`feature_schema_v0` 或 **`feature_schema_v1`**）做 **baseline_lr**（logistic，无 sklearn），与线性分 **blend / replace**；`/process` 可传 **`like_proxy_hint` / `like_proxy`**；v1 还可传 **`comment_proxy_hint`** 等（见 `.env.example`）。`XHS_FACTORY_PREDICT_USE_LLM=1` 时 MiniMax 仍可在最后覆盖 `predicted_score`。
- 样本接入：改 `_fetch` 或环境变量 `XHS_FACTORY_*`。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from openclaw.feed_like_parse import like_proxy_with_default

import minimax_client
import prompt_store

# --- 可替换：真实话题下样本抓取 ---------------------------------------------


def _factory_use_minimax() -> bool:
    if not minimax_client.minimax_configured():
        return False
    v = (os.environ.get("OPENCLAW_MINIMAX_FACTORY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _topic_file_slug(topic: str) -> str:
    return hashlib.sha256((topic or "").encode("utf-8")).hexdigest()[:16]


# --- Feed v1 扩展（与 scripts/export_to_xhs_feed.py 保持同步）---
_COMMENT_KEYS = (
    "comment_proxy",
    "comment_count",
    "comments_count",
    "comment_cnt",
    "note_comment_count",
    "sub_comment_count",
)
_COLLECT_KEYS = (
    "collect_proxy",
    "collected_count",
    "collection_count",
    "favorite_count",
    "bookmark_count",
    "collect_count",
)
_SHARE_KEYS = ("share_proxy", "share_count", "shared_count", "forward_count")
_TIME_STR_KEYS = (
    "published_at",
    "publish_time",
    "create_time",
    "time",
    "note_publish_time",
    "last_update_time",
)
_TIME_NUM_KEYS = ("timestamp", "create_timestamp", "publish_timestamp")


def _ts_to_iso_utc(ts: float) -> str | None:
    if ts > 1e12:
        ts = ts / 1000.0
    if ts < 0 or ts > 4102444800:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _coerce_published_at_from_string(s: str) -> str | None:
    try:
        s_iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s_iso)
    except ValueError:
        try:
            return _ts_to_iso_utc(float(s))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _parse_published_at_iso(raw: dict[str, Any]) -> str | None:
    for k in _TIME_STR_KEYS:
        v = raw.get(k)
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out = _ts_to_iso_utc(float(v))
            if out:
                return out
            continue
        if isinstance(v, str):
            out = _coerce_published_at_from_string(v.strip())
            if out:
                return out
    for k in _TIME_NUM_KEYS:
        v = raw.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            out = _ts_to_iso_utc(float(v))
        except (TypeError, ValueError):
            continue
        if out:
            return out
    return None


def _first_nonneg_int(raw: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = raw.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        return n
    return None


def _optional_feed_v1_fields(raw: dict[str, Any]) -> dict[str, Any]:
    ext: dict[str, Any] = {}
    pa = _parse_published_at_iso(raw)
    if pa is not None:
        ext["published_at"] = pa
    c = _first_nonneg_int(raw, *_COMMENT_KEYS)
    if c is not None:
        ext["comment_proxy"] = c
    col = _first_nonneg_int(raw, *_COLLECT_KEYS)
    if col is not None:
        ext["collect_proxy"] = col
    sh = _first_nonneg_int(raw, *_SHARE_KEYS)
    if sh is not None:
        ext["share_proxy"] = sh
    return ext


_baseline_lr_cache: dict[str, Any] | None = None
_baseline_lr_cache_key: tuple[str, float] | None = None


def _baseline_assumed_like_proxy() -> int:
    raw = (os.environ.get("XHS_FACTORY_BASELINE_ASSUMED_LIKE") or "100").strip()
    try:
        v = int(raw)
    except ValueError:
        v = 100
    return max(1, v)


def _recreated_text_to_title_body_lengths(text: str) -> tuple[int, int]:
    """与 export_features_v0 对 title_hint/body_hint 长度统计方式对齐的启发式：首行作标题，其余作正文。"""
    t = (text or "").strip()
    if not t:
        return 0, 0
    if "\n" in t:
        first, _, rest = t.partition("\n")
        title_s = first.strip()[:500]
        body_s = rest.strip()[:2000]
    else:
        title_s = ""
        body_s = t.strip()[:2000]
    if not title_s and not body_s:
        return 0, 0
    if not title_s:
        title_s = body_s[:120]
    if not body_s:
        body_s = title_s
    return len(title_s), len(body_s)


_BASELINE_V0_FEATS: tuple[str, ...] = ("title_len", "body_len", "log1p_like")
_BASELINE_V1_FEATS: tuple[str, ...] = (
    "title_len",
    "body_len",
    "log1p_like",
    "log1p_comment",
    "log1p_collect",
    "log1p_share",
    "age_days",
)


def _utc_dt_from_published_iso(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
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


def _baseline_nonneg_from_env_hint(env_key: str, hint_key: str, hints: dict[str, Any] | None) -> int:
    if hints:
        v = hints.get(hint_key)
        if v is not None and str(v).strip() != "":
            try:
                return max(0, int(float(v)))
            except (TypeError, ValueError):
                pass
    raw = (os.environ.get(env_key) or "0").strip()
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def _baseline_age_days_from_hint(hints: dict[str, Any] | None) -> float:
    pa: datetime | None = None
    if hints and hints.get("published_at"):
        pa = _utc_dt_from_published_iso(str(hints["published_at"]))
    if pa is None:
        raw = (os.environ.get("XHS_FACTORY_BASELINE_ASSUMED_AGE_DAYS") or "0").strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0
    ref = datetime.now(timezone.utc)
    return max(0.0, (ref - pa).total_seconds() / 86400.0)


def _load_baseline_lr_payload() -> dict[str, Any] | None:
    """读取 train_baseline_v0产出；支持 feature_schema_v0 / feature_schema_v1。"""
    global _baseline_lr_cache, _baseline_lr_cache_key
    path = (os.environ.get("XHS_FACTORY_BASELINE_JSON") or "").strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _baseline_lr_cache is not None and _baseline_lr_cache_key == (path, mtime):
        return _baseline_lr_cache
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if schema not in ("feature_schema_v0", "feature_schema_v1"):
        return None
    feats = data.get("feature_names")
    coef = data.get("coefficients")
    icept = data.get("intercept")
    if not isinstance(feats, list) or not isinstance(coef, dict) or not isinstance(icept, (int, float)):
        return None
    if schema == "feature_schema_v0":
        if tuple(feats) != _BASELINE_V0_FEATS:
            return None
    elif tuple(feats) != _BASELINE_V1_FEATS:
        return None
    for name in feats:
        if name not in coef:
            return None
    _baseline_lr_cache = data
    _baseline_lr_cache_key = (path, mtime)
    return data


def _baseline_lr_logistic_p(
    payload: dict[str, Any],
    text: str,
    like_proxy_hint: int | None = None,
    v1_hints: dict[str, Any] | None = None,
) -> tuple[float, dict[str, Any]]:
    feats: list[str] = list(payload["feature_names"])
    coefs: dict[str, Any] = payload["coefficients"]
    icept = float(payload["intercept"])
    schema = str(payload.get("schema") or "")
    title_len, body_len = _recreated_text_to_title_body_lengths(text)
    if like_proxy_hint is not None and int(like_proxy_hint) >= 1:
        assumed = int(like_proxy_hint)
        like_src = "request_hint"
    else:
        assumed = _baseline_assumed_like_proxy()
        like_src = "env_default"
    log1p_like = round(math.log1p(assumed), 6)
    x_map: dict[str, float] = {
        "title_len": float(title_len),
        "body_len": float(body_len),
        "log1p_like": float(log1p_like),
    }
    if schema == "feature_schema_v1":
        h = v1_hints or {}
        cmt = _baseline_nonneg_from_env_hint("XHS_FACTORY_BASELINE_ASSUMED_COMMENT", "comment_proxy", h)
        col = _baseline_nonneg_from_env_hint("XHS_FACTORY_BASELINE_ASSUMED_COLLECT", "collect_proxy", h)
        shr = _baseline_nonneg_from_env_hint("XHS_FACTORY_BASELINE_ASSUMED_SHARE", "share_proxy", h)
        age = _baseline_age_days_from_hint(h)
        x_map["log1p_comment"] = float(round(math.log1p(cmt), 6))
        x_map["log1p_collect"] = float(round(math.log1p(col), 6))
        x_map["log1p_share"] = float(round(math.log1p(shr), 6))
        x_map["age_days"] = float(round(age, 6))
    z = icept
    detail: dict[str, Any] = {
        "model": "baseline_lr_v1" if schema == "feature_schema_v1" else "baseline_lr_v0",
        "schema": schema,
        "title_len": title_len,
        "body_len": body_len,
        "assumed_like_proxy": assumed,
        "like_proxy_source": like_src,
        "log1p_like": log1p_like,
        "z_linear": None,
        "p_logistic_raw": None,
    }
    if schema == "feature_schema_v1":
        detail["v1_assumptions"] = {
            "comment_proxy": _baseline_nonneg_from_env_hint(
                "XHS_FACTORY_BASELINE_ASSUMED_COMMENT", "comment_proxy", v1_hints
            ),
            "collect_proxy": _baseline_nonneg_from_env_hint(
                "XHS_FACTORY_BASELINE_ASSUMED_COLLECT", "collect_proxy", v1_hints
            ),
            "share_proxy": _baseline_nonneg_from_env_hint(
                "XHS_FACTORY_BASELINE_ASSUMED_SHARE", "share_proxy", v1_hints
            ),
            "age_days": round(_baseline_age_days_from_hint(v1_hints), 6),
            "published_at_hint": (v1_hints or {}).get("published_at"),
        }
    for name in feats:
        if name not in x_map:
            return 0.5, {**detail, "error": f"missing_feature:{name}"}
        z += float(coefs[name]) * x_map[name]
    detail["z_linear"] = round(z, 6)
    zc = max(-30.0, min(30.0, z))
    p = 1.0 / (1.0 + math.exp(-zc))
    detail["p_logistic_raw"] = round(p, 6)
    return p, detail


def _baseline_mode_and_weight() -> tuple[str, float]:
    mode = (os.environ.get("XHS_FACTORY_BASELINE_MODE") or "blend").strip().lower()
    if mode not in ("blend", "replace"):
        mode = "blend"
    raw_w = (os.environ.get("XHS_FACTORY_BASELINE_WEIGHT") or "0.35").strip()
    try:
        w = float(raw_w)
    except ValueError:
        w = 0.35
    w = max(0.0, min(1.0, w))
    return mode, w


def _normalize_external_sample(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把 MediaCrawler / 自建导出等字段映射为工厂内部结构。"""
    if not isinstance(raw, dict):
        return None
    title = (
        raw.get("title_hint")
        or raw.get("title")
        or raw.get("note_title")
        or raw.get("desc")
        or raw.get("description")
    )
    body = (
        raw.get("body_hint")
        or raw.get("content")
        or raw.get("note_text")
        or raw.get("desc")
        or raw.get("description")
    )
    title_s = str(title or "").strip()[:500]
    body_s = str(body or "").strip()[:2000]
    if not title_s and not body_s:
        return None
    if not title_s:
        title_s = body_s[:120]
    if not body_s:
        body_s = title_s
    likes = raw.get("like_proxy") or raw.get("liked_count") or raw.get("likes") or raw.get("like_count")
    like_proxy = like_proxy_with_default(likes, default=100)
    sop = str(raw.get("sop_tag") or raw.get("viral_sop") or "对照式").strip()[:32] or "对照式"
    emo = str(raw.get("emotion_tag") or raw.get("target_emotion") or "共鸣").strip()[:32] or "共鸣"
    out: dict[str, Any] = {
        "title_hint": title_s,
        "body_hint": body_s,
        "like_proxy": max(1, like_proxy),
        "sop_tag": sop,
        "emotion_tag": emo,
    }
    out.update(_optional_feed_v1_fields(raw))
    return out


def _load_json_array_from_path(path: str) -> list[dict[str, Any]]:
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw_text = f.read()
    except OSError:
        return []
    items: list[Any] = []
    raw_text_stripped = raw_text.strip()
    if not raw_text_stripped:
        return []
    if raw_text_stripped.startswith("["):
        try:
            data = json.loads(raw_text_stripped)
            items = data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    else:
        for line in raw_text_stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    items.append(row)
            except json.JSONDecodeError:
                continue
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        norm = _normalize_external_sample(row)
        if norm:
            out.append(norm)
    return out


def _fetch_mock(topic: str, n: int) -> list[dict[str, Any]]:
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


def _fetch_from_http(base_url: str, topic: str, n: int) -> list[dict[str, Any]] | None:
    base_url = (base_url or "").strip()
    if not base_url:
        return None
    q = urllib.parse.urlencode({"topic": topic, "limit": str(n)})
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}{q}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, list):
        return None
    out: list[dict[str, Any]] = []
    for row in data:
        if isinstance(row, dict):
            norm = _normalize_external_sample(row)
            if norm:
                out.append(norm)
    return out[:n] if out else None


def _fetch(topic: str, sample_size: int) -> list[dict[str, Any]]:
    """
    样本来源优先级：HTTP（XHS_FACTORY_FEED_URL）→ 目录分文件/汇总    （XHS_FACTORY_FEED_DIR）→ 单文件（XHS_FACTORY_SAMPLES_PATH）→ 模拟。
    外部 JSON 可为数组或 JSONL；字段见 _normalize_external_sample。
    """
    n = max(3, min(48, int(sample_size)))
    t = (topic or "").strip()

    feed_url = (os.environ.get("XHS_FACTORY_FEED_URL") or "").strip()
    if feed_url:
        got = _fetch_from_http(feed_url, t, n)
        if got:
            return got

    feed_dir = (os.environ.get("XHS_FACTORY_FEED_DIR") or "").strip()
    if feed_dir and os.path.isdir(feed_dir):
        slug = _topic_file_slug(t)
        for name in (f"{slug}.json", "samples.json", "feed.json"):
            p = os.path.join(feed_dir, name)
            rows = _load_json_array_from_path(p)
            if rows:
                return rows[:n]

    samples_path = (os.environ.get("XHS_FACTORY_SAMPLES_PATH") or "").strip()
    if samples_path:
        rows = _load_json_array_from_path(samples_path)
        if rows:
            return rows[:n]

    return _fetch_mock(t, n)


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
        out = {
            "viral_sop": str(gene_sop.get("viral_sop") or "对照式")[:32],
            "core_hook": str(gene_sop.get("core_hook") or "")[:200],
            "target_emotion": str(gene_sop.get("target_emotion") or "共鸣")[:32],
        }
        formulas = gene_sop.get("formulas")
        if isinstance(formulas, list) and formulas:
            out["formulas"] = formulas[:5]
        return out
    if isinstance(gene_sop, str) and gene_sop.strip():
        try:
            obj = json.loads(gene_sop)
            if isinstance(obj, dict):
                return _norm_gene(obj)
        except json.JSONDecodeError:
            pass
        return {"viral_sop": gene_sop.strip()[:32], "core_hook": "", "target_emotion": "共鸣"}
    return {"viral_sop": "对照式", "core_hook": "", "target_emotion": "共鸣"}


def _extract_use_llm() -> bool:
    """是否在挖掘阶段调用大模型（默认开，仅补充 formulas；主标签默认已由统计给出）。"""
    if not _factory_use_minimax():
        return False
    v = (os.environ.get("XHS_FACTORY_EXTRACT_USE_LLM") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _extract_llm_overrides_gene() -> bool:
    """为1 时允许 MiniMax 覆盖 viral_sop / target_emotion / core_hook（旧行为）。"""
    v = (os.environ.get("XHS_FACTORY_EXTRACT_LLM_OVERRIDES_GENE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _predict_use_llm() -> bool:
    """预测分是否走大模型；默认关闭，仅用确定性公式（见 predict_viral_score）。"""
    if not minimax_client.minimax_configured():
        return False
    v = (os.environ.get("XHS_FACTORY_PREDICT_USE_LLM") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _xhs_gene_agg_mode() -> str:
    """viral_sop / target_emotion 主结论：weighted=点赞 log1p 加权；count=纯条数众数。"""
    m = (os.environ.get("XHS_FACTORY_GENE_AGG_MODE") or "weighted").strip().lower()
    return "count" if m == "count" else "weighted"


def _quantitative_sample_analysis(samples: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    """
    可复现的样本统计（不调用大模型）。
    - 每条样本对标签权重贡献：w = log(1 + max(like_proxy, 0))。
    - 加权主标签 = argmax_i sum_{样本 j标签为 i} w_j；占比 = 该标签加权和 / 全体加权和。
    - 标签离散度（自然底 e）：由标签出现频次得 p_i，H = -sum_i p_i log p_i，越高表示套路越分散。
    """
    sop_w: dict[str, float] = {}
    emo_w: dict[str, float] = {}
    sop_c: dict[str, int] = {}
    emo_c: dict[str, int] = {}
    likes_list: list[int] = []
    title_lens: list[int] = []

    for s in samples:
        sop = str(s.get("sop_tag") or "对照式").strip() or "对照式"
        emo = str(s.get("emotion_tag") or "共鸣").strip() or "共鸣"
        try:
            lk = max(0, int(s.get("like_proxy") or 0))
        except (TypeError, ValueError):
            lk = 0
        wt = math.log1p(lk)
        sop_w[sop] = sop_w.get(sop, 0.0) + wt
        emo_w[emo] = emo_w.get(emo, 0.0) + wt
        sop_c[sop] = sop_c.get(sop, 0) + 1
        emo_c[emo] = emo_c.get(emo, 0) + 1
        likes_list.append(lk)
        title_lens.append(len(str(s.get("title_hint") or "").strip()))

    def _entropy_from_counts(counts: dict[str, int]) -> float:
        tot = sum(counts.values())
        if tot <= 0:
            return 0.0
        h = 0.0
        for c in counts.values():
            if c <= 0:
                continue
            p = c / tot
            h -= p * math.log(p + 1e-12)
        return round(h, 4)

    total_w_sop = sum(sop_w.values()) or 1.0
    total_w_emo = sum(emo_w.values()) or 1.0
    viral_w = max(sop_w, key=lambda k: sop_w[k]) if sop_w else "对照式"
    emo_win_w = max(emo_w, key=lambda k: emo_w[k]) if emo_w else "共鸣"
    viral_c = max(sop_c, key=lambda k: sop_c[k]) if sop_c else "对照式"
    emo_win_c = max(emo_c, key=lambda k: emo_c[k]) if emo_c else "共鸣"
    share_sop = round(float(sop_w.get(viral_w, 0.0)) / total_w_sop, 4)
    share_emo = round(float(emo_w.get(emo_win_w, 0.0)) / total_w_emo, 4)

    indexed = list(enumerate(samples))
    indexed.sort(key=lambda x: int(x[1].get("like_proxy") or 0), reverse=True)
    top_evidence: list[dict[str, Any]] = []
    for _, s in indexed[:5]:
        top_evidence.append(
            {
                "like_proxy": int(s.get("like_proxy") or 0),
                "title_hint": str(s.get("title_hint") or "")[:160],
                "sop_tag": str(s.get("sop_tag") or ""),
                "emotion_tag": str(s.get("emotion_tag") or ""),
            }
        )

    sorted_likes = sorted(likes_list)
    mid = len(sorted_likes) // 2
    median_like = float(sorted_likes[mid]) if sorted_likes else 0.0
    mean_like = sum(likes_list) / max(len(likes_list), 1)
    t_short = (topic or "").strip()[:20] or "主题"

    formula_reference = [
        "w_j = log(1 + max(like_proxy_j, 0))；每条样本只贡献给其 sop_tag / emotion_tag各一次。",
        "标签 i 的加权和 W_i = sum_j w_j * 1{tag_j=i}；加权主标签 = argmax_i W_i；占比 = W_i* / sum_k W_k。",
        "频次主标签 = argmax_i count_i（与加权结果对照，防止样本条数多但赞少时绑架结论）。",
        "熵 H = -sum_i p_i log p_i（p_i 为标签频次占比），无量纲，越高表示标签越分散。",
    ]

    return {
        "aggregation_kernel": "log1p_like_proxy",
        "formula_reference": formula_reference,
        "gene_agg_mode_applied": _xhs_gene_agg_mode(),
        "viral_sop_weighted": viral_w,
        "viral_sop_count_mode": viral_c,
        "target_emotion_weighted": emo_win_w,
        "target_emotion_count_mode": emo_win_c,
        "weighted_share_viral_sop": share_sop,
        "weighted_share_target_emotion": share_emo,
        "entropy_sop_tags": _entropy_from_counts(sop_c),
        "entropy_emotion_tags": _entropy_from_counts(emo_c),
        "engagement_summary": {
            "mean_like_proxy": round(mean_like, 2),
            "median_like_proxy": median_like,
            "sum_like_proxy": int(sum(likes_list)),
        },
        "title_length_mean": round(sum(title_lens) / max(len(title_lens), 1), 2),
        "top_samples_by_like": top_evidence,
        "topic_slice_for_hook": t_short,
    }


def _deterministic_predict_score(
    text: str,
    gene_sop: Any,
    case_library: list[dict[str, Any]],
) -> tuple[float, float, str, dict[str, Any]]:
    """
    可解释、可复现的预测分（不调用大模型）。
    在 [0.05, 0.95] 内对线性分截断；特征为长度、中文占比、案例库 SOP 命中、参考库均分、过短惩罚。
    """
    g = _norm_gene(gene_sop)
    sop = g["viral_sop"]
    lib = case_library if isinstance(case_library, list) and case_library else _default_case_library()
    ref_scores = [float(x.get("avg_score", 0.6)) for x in lib if isinstance(x, dict)]
    ref_mean = sum(ref_scores) / max(len(ref_scores), 1)
    matches = sum(1 for x in lib if isinstance(x, dict) and str(x.get("viral_sop")) == sop)
    L = len(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    cjk_r = cjk / max(L, 1)
    L_n = min(1.0, L / 560.0)
    m_n = matches / max(len(lib), 1)
    short_pen = 1.0 if L < 48 else 0.0
    miss_pen = 1.0 if matches == 0 else 0.0
    raw = (
        0.14
        + 0.26 * L_n
        + 0.22 * cjk_r
        + 0.12 * m_n
        + 0.22 * ref_mean
        - 0.12 * short_pen
        - 0.06 * miss_pen
    )
    no_cjk = not re.search(r"[\u4e00-\u9fff]", text)
    if no_cjk:
        raw *= 0.88
    predicted = max(0.05, min(0.95, raw))
    conf_raw = 0.38 + 0.28 * L_n + 0.18 * m_n + 0.08 * max(0.0, 1.0 - abs(0.65 - ref_mean))
    confidence = max(0.35, min(0.9, conf_raw))
    if no_cjk:
        risk = "no_cjk_body"
    elif L < 48:
        risk = "length_short"
    elif matches == 0:
        risk = "sop_mismatch_low"
    else:
        risk = "low_risk_heuristic"
    breakdown = {
        "model": "linear_clamp_v1",
        "formula": (
            "clip(0.14 + 0.26*L_norm + 0.22*cjk_ratio + 0.12*m_norm + 0.22*ref_mean "
            "- 0.12*I(len<48) - 0.06*I(sop_no_lib_match), 0.05, 0.95); multiply by 0.88 if no CJK. "
            "L_norm=min(1,len/560); m_norm=matches/len(case_library)."
        ),
        "L": L,
        "L_norm": round(L_n, 4),
        "cjk_ratio": round(cjk_r, 4),
        "case_sop_match_count": matches,
        "case_sop_match_norm": round(m_n, 4),
        "reference_mean": round(ref_mean, 4),
        "raw_before_clip": round(raw, 4),
        "penalties": {"short_text": bool(short_pen), "sop_miss": bool(miss_pen), "no_cjk": bool(no_cjk)},
    }
    return round(predicted, 4), round(confidence, 4), risk, breakdown


def extract_viral_patterns(topic: str, sample_size: int = 12) -> dict[str, Any]:
    """【挖掘】从话题下样本做可量化聚合，再可选调用大模型补充 formulas。"""
    t = (topic or "").strip()[:120]
    n = max(3, min(48, int(sample_size)))
    samples = _fetch(t, n)
    quant = _quantitative_sample_analysis(samples, t)
    mode = _xhs_gene_agg_mode()
    if mode == "count":
        viral_sop = quant["viral_sop_count_mode"]
        target_emotion = quant["target_emotion_count_mode"]
    else:
        viral_sop = quant["viral_sop_weighted"]
        target_emotion = quant["target_emotion_weighted"]
    like_sum = int(quant["engagement_summary"]["sum_like_proxy"])
    share_s = quant["weighted_share_viral_sop"]
    share_e = quant["weighted_share_target_emotion"]
    t_short = quant["topic_slice_for_hook"]
    core_hook = (
        f"用「{viral_sop}」承接「{target_emotion}」：样本中加权和占比约 {share_s:.0%}/{share_e:.0%}（{mode}），"
        f"前 3 秒用与「{t_short}」强相关的反差信息留人。"
    )[:200]
    out: dict[str, Any] = {
        "topic": t,
        "sample_size_requested": n,
        "sample_size_effective": len(samples),
        "viral_sop": viral_sop,
        "core_hook": core_hook,
        "target_emotion": target_emotion,
        "aggregate_like_proxy": like_sum,
        "quantitative": quant,
        "notes": "stats-first extract_viral_patterns (log1p-weighted tags + entropy; optional LLM for formulas only)",
    }
    if _extract_use_llm():
        slim = []
        for s in samples[:16]:
            row = {k: s.get(k) for k in ("title_hint", "body_hint", "like_proxy", "sop_tag", "emotion_tag") if k in s}
            bh = row.get("body_hint")
            if isinstance(bh, str) and len(bh) > 200:
                row["body_hint"] = bh[:200] + "…"
            slim.append(row)
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
            formulas = parsed.get("formulas")
            if isinstance(formulas, list) and formulas:
                out["formulas"] = formulas[:3]
            if _extract_llm_overrides_gene():
                f0 = formulas[0] if isinstance(formulas, list) and formulas and isinstance(formulas[0], dict) else {}
                if isinstance(f0, dict):
                    hl = str(f0.get("hook_logic") or "").strip()
                    ss = str(f0.get("structure_sop") or "").strip()
                    et = str(f0.get("emotional_trigger") or "").strip()
                    pid = str(f0.get("pattern_id") or "").strip()
                    if len(str(parsed.get("viral_sop") or "").strip()) < 2:
                        vs_fb = (ss[:32] or pid[:32] or "对照式").strip()
                        out["viral_sop"] = (vs_fb if len(vs_fb) >= 2 else "对照式")[:32]
                    if len(str(parsed.get("core_hook") or "").strip()) < 4:
                        merged = "；".join(x for x in (hl, ss) if x).strip()
                        if len(merged) >= 4:
                            out["core_hook"] = merged[:200]
                    if len(str(parsed.get("target_emotion") or "").strip()) < 2:
                        out["target_emotion"] = (et[:32] if et else "共鸣")[:32]
                vs = str(parsed.get("viral_sop") or out.get("viral_sop") or "").strip()
                ch = str(parsed.get("core_hook") or out.get("core_hook") or "").strip()
                te = str(parsed.get("target_emotion") or out.get("target_emotion") or "").strip()
                if len(vs) >= 2:
                    out["viral_sop"] = vs[:32]
                if len(ch) >= 4:
                    out["core_hook"] = ch[:200]
                if len(te) >= 2:
                    out["target_emotion"] = te[:32]
                if len(str(out.get("core_hook") or "").strip()) < 4:
                    out["core_hook"] = (
                        f"用「{out.get('viral_sop') or '对照式'}」承接「{out.get('target_emotion') or '共鸣'}」，"
                        f"前3秒抛出与「{t_short}」强相关的反差信息"
                    )[:200]
                if len(str(out.get("viral_sop") or "").strip()) < 2:
                    out["viral_sop"] = "对照式"
                if len(str(out.get("target_emotion") or "").strip()) < 2:
                    out["target_emotion"] = "共鸣"
            out["notes"] = out["notes"] + " | llm: formulas" + (" + gene_override" if _extract_llm_overrides_gene() else "")
        else:
            out["notes"] = (out["notes"] or "") + " | llm: no parseable json"
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
        user_tpl = str(pack.get("user_template") or "").strip()
        gene_json = json.dumps(g, ensure_ascii=False)
        if user_tpl:
            try:
                user_p = prompt_store.substitute_user_template(
                    user_tpl,
                    original=orig[:omax],
                    style=st,
                    gene_json=gene_json,
                )
            except (KeyError, ValueError):
                user_p = json.dumps(
                    {"original": orig[:omax], "gene_sop": g, "style": st},
                    ensure_ascii=False,
                )
        else:
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
    like_proxy_hint: int | None = None,
    baseline_v1_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """【预测】默认可复现的线性启发式；仅当 XHS_FACTORY_PREDICT_USE_LLM=1 时才用 MiniMax 覆盖分数。
    like_proxy_hint：可选；>=1 时用于 baseline 的 log1p_like；否则用 XHS_FACTORY_BASELINE_ASSUMED_LIKE。
    baseline_v1_hints：feature_schema_v1 时可选 comment_proxy / collect_proxy / share_proxy / published_at（ISO），缺省读 XHS_FACTORY_BASELINE_ASSUMED_* 环境变量。
    """
    text = (recreated_text or "").strip()
    g = _norm_gene(gene_sop)
    lib = case_library if isinstance(case_library, list) and case_library else _default_case_library()
    ref_scores = [float(x.get("avg_score", 0.6)) for x in lib if isinstance(x, dict)]
    ref_mean = sum(ref_scores) / max(len(ref_scores), 1)
    predicted, confidence, risk, breakdown = _deterministic_predict_score(text, gene_sop, lib)
    notes = "deterministic predict_viral_score (linear_clamp_v1; see score_breakdown)"
    bl_payload = _load_baseline_lr_payload()
    if bl_payload is not None:
        p_raw, bl_detail = _baseline_lr_logistic_p(
            bl_payload, text, like_proxy_hint, v1_hints=baseline_v1_hints
        )
        if bl_detail.get("error"):
            notes = notes + f" | baseline_lr: {bl_detail.get('error')}"
        else:
            p_lr = max(0.05, min(0.95, float(p_raw)))
            mode, w = _baseline_mode_and_weight()
            breakdown = {**breakdown, "baseline_lr": bl_detail}
            linear_score = float(predicted)
            if mode == "replace":
                predicted = round(p_lr, 4)
                notes = notes + " | baseline_lr replace"
            else:
                predicted = round(w * p_lr + (1.0 - w) * linear_score, 4)
                notes = notes + f" | baseline_lr blend(w={w})"
            predicted = max(0.05, min(0.95, float(predicted)))
    out: dict[str, Any] = {
        "predicted_score": predicted,
        "confidence": confidence,
        "risk_factor": risk,
        "reference_mean": round(ref_mean, 4),
        "case_library_size": len(lib),
        "score_breakdown": breakdown,
        "notes": notes,
    }
    if not _predict_use_llm():
        return out
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
    user_tpl = str(pack.get("user_template") or "").strip()
    gene_json = json.dumps(g, ensure_ascii=False)
    case_blob = json.dumps(lib[:lib_n], ensure_ascii=False)[:12000]
    if user_tpl:
        try:
            user_p = prompt_store.substitute_user_template(
                user_tpl,
                recreated_blob=text[:tmax],
                gene_json=gene_json,
                case_blob=case_blob,
            )
        except (KeyError, ValueError):
            user_p = json.dumps(
                {
                    "recreated_text": text[:tmax],
                    "gene_sop": g,
                    "case_library": lib[:lib_n],
                },
                ensure_ascii=False,
            )
    else:
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
            if ps > 1.0:
                ps = min(1.0, ps / 100.0)
            ar = str(parsed.get("audit_reason") or "").strip()
            rf0 = str(parsed.get("risk_factor") or risk).strip()
            rf = f"{ar} | {rf0}" if ar else rf0
            rf = rf[:240]
            if 0.0 <= ps <= 1.0 and 0.0 <= cf <= 1.0:
                out["predicted_score"] = round(ps, 4)
                out["confidence"] = round(cf, 4)
                out["risk_factor"] = rf
                out["notes"] = "minimax predict_viral_score (overrides score; deterministic kept in score_breakdown)"
                return out
            out["notes"] = notes + " | minimax: scores out of range"
        except (TypeError, ValueError):
            out["notes"] = notes + " | minimax: invalid numeric fields"
    else:
        out["notes"] = notes + " | minimax: no parseable json"
    return out
