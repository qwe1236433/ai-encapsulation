import hashlib
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from fastapi import FastAPI
from pydantic import BaseModel, Field

import minimax_client
import prompt_store
import xhs_factory

app = FastAPI(title="OpenClaw", version="0.1.0")

_LINK_TIMEOUT_SEC = 5.0


def _boost_keyword() -> str:
    return (os.environ.get("TRAFFIC_SIM_BOOST_KEYWORD") or "救命").strip() or "救命"


def _likes_if_hit() -> int:
    try:
        return int(os.environ.get("TRAFFIC_SIM_LIKES_IF_HIT") or "1000")
    except ValueError:
        return 1000


def _likes_else() -> int:
    try:
        return int(os.environ.get("TRAFFIC_SIM_LIKES_ELSE") or "10")
    except ValueError:
        return 10


def _pass_min_hermes_hint() -> int:
    try:
        return int(os.environ.get("TRAFFIC_SIM_PASS_MIN") or "500")
    except ValueError:
        return 500


def simulate_traffic_for_text(text: str) -> dict[str, Any]:
    """
    流量模拟器：标题/文案里出现 boost词（默认「救命」）→ 高赞，否则低赞。
    Hermes S3 应与此处规则一致（环境变量同名）。
    """
    kw = _boost_keyword()
    hit = kw in (text or "")
    likes = _likes_if_hit() if hit else _likes_else()
    return {
        "predicted_likes": likes,
        "boost_keyword": kw,
        "boost_keyword_hit": hit,
        "rule": f"contains '{kw}' -> {_likes_if_hit()} else {_likes_else()}",
    }


def mock_engagement_bundle(text: str) -> dict[str, Any]:
    """点赞 +确定性 CTR/曝光（便于 Hermes 复算核对）；不写死业务公式，仅 mock。"""
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


class ProcessRequest(BaseModel):
    task_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class ProcessResponse(BaseModel):
    task_id: str
    status: str
    action: str
    result: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def _fetch_json(url: str) -> tuple[Any | None, str | None]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_LINK_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


def _gpu_snapshot() -> dict[str, Any]:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return {
                "available": False,
                "backend": "nvidia-smi",
                "detail": (r.stderr or "").strip() or "nvidia-smi nonzero exit",
            }
        line = (r.stdout or "").strip().splitlines()
        if not line:
            return {"available": False, "backend": "nvidia-smi", "detail": "empty output"}
        parts = [p.strip() for p in line[0].split(",")]
        if len(parts) >= 3:
            return {
                "available": True,
                "backend": "nvidia-smi",
                "device": parts[0],
                "memory_used_mib": float(parts[1]),
                "memory_total_mib": float(parts[2]),
            }
        return {"available": True, "backend": "nvidia-smi", "raw": line[0]}
    except FileNotFoundError:
        return {"available": False, "backend": "none", "detail": "nvidia-smi not found"}
    except Exception as e:
        return {"available": False, "backend": "error", "detail": str(e)}


def _simulate_noise_enabled() -> bool:
    v = (os.environ.get("OPENCLAW_SIMULATE_NOISE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _simulate_noise_rate() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("OPENCLAW_SIMULATE_FAILURE_RATE", "0.25"))))
    except ValueError:
        return 0.25


def _env_flag(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _run_analyze_trends(params: dict[str, Any]) -> dict[str, Any]:
    """Mock：多序列/话题趋势摘要。"""
    symbol = str(params.get("symbol", "FLOW_MAIN"))
    window = max(1, min(120, int(params.get("window_min", 15))))
    seed = sum(ord(c) for c in symbol) % 97 / 97.0
    direction = random.choice(["up", "down", "flat"])
    series = [round(seed + i * 0.008 + random.uniform(-0.02, 0.02), 4) for i in range(min(window, 16))]
    out: dict[str, Any] = {
        "symbol": symbol,
        "window_min": window,
        "trend": direction,
        "vol_proxy": round(random.uniform(0.2, 1.0), 3),
        "confidence": round(random.uniform(0.55, 0.95), 3),
        "series_tail": series,
        "notes": "mock analyze_trends (replace with real feeds)",
    }
    if _simulate_noise_enabled() and random.random() < _simulate_noise_rate():
        out["trend"] = "invalid"
        out["data_quality"] = "bad"
        out["notes"] = out["notes"] + " | simulated feed glitch"
    return out


def _run_generate_headline(params: dict[str, Any]) -> dict[str, Any]:
    """
    原子算子：只产出一条标题 +流量模拟预览。
    配置 MINIMAX_API_KEY 且 OPENCLAW_MINIMAX_HEADLINE 开启时走 MiniMax；否则 Mock。
    """
    topic = str(params.get("topic", "流量复盘")).strip()[:80]
    tone = str(params.get("tone", "sharp")).strip()[:20]
    boost = bool(params.get("boost", False)) or (_boost_keyword() in topic)
    kw = _boost_keyword()
    notes = "mock generate_headline"
    headline = ""
    if minimax_client.minimax_configured() and _env_flag("OPENCLAW_MINIMAX_HEADLINE", True):
        sys_p = (
            "你是顶级小红书流量操盘手。只输出一个 JSON 对象，禁止 markdown 代码块。"
            ' 键：headline（string，不超过36字）、reason（string，一句说明钩子逻辑）。'
        )
        user_p = f"话题：{topic}。语气风格：{tone}。"
        if boost:
            user_p += f" 标题里必须自然出现爆词「{kw}」（可用合理变体，勿生硬堆砌）。"
        else:
            user_p += f" 不要使用爆词「{kw}」及其明显变体。"
        user_p += " 标题要让人想点开、带情绪但不过分标题党。"
        parsed = minimax_client.MiniMaxClient().complete_json(sys_p, user_p)
        if parsed and str(parsed.get("headline") or "").strip():
            headline = str(parsed["headline"]).strip()[:80]
            notes = "minimax generate_headline"
    if not headline:
        if boost:
            headline = f"{kw}！{topic} · {tone}钩子"
        else:
            headline = f"{topic} · {tone}速览"
    eng = mock_engagement_bundle(headline)
    return {
        "topic": topic,
        "tone": tone,
        "boost_applied": boost,
        "headline": headline,
        "engagement_preview": eng,
        "notes": notes,
    }


def _run_generate_hook_lines(params: dict[str, Any]) -> dict[str, Any]:
    """原子算子：只产出若干条钩子话术行（不负责标题/封面/正文）。"""
    topic = str(params.get("topic", "流量复盘")).strip()[:80]
    tone = str(params.get("tone", "sharp")).strip()[:20]
    hook_angle = str(params.get("hook_angle", "")).strip()[:40]
    lines = [
        f"钩子A：{hook_angle or '情绪反差'}+{tone}",
        "钩子B：数字锚点 + 限时感",
        "钩子C：评论区留扣",
    ]
    return {
        "topic": topic,
        "tone": tone,
        "hook_angle": hook_angle,
        "lines": lines,
        "notes": "mock generate_hook_lines",
    }


def _run_predict_traffic(params: dict[str, Any]) -> dict[str, Any]:
    """对给定 headline/text 跑同一套流量模拟器。"""
    text = str(params.get("headline") or params.get("text") or params.get("copy") or "").strip()
    if not text:
        return {
            "predicted_likes": _likes_else(),
            "boost_keyword": _boost_keyword(),
            "boost_keyword_hit": False,
            "ctr_pct": 0.5,
            "impressions": 120,
            "rule": "empty text -> low likes",
            "notes": "mock predict_traffic: empty input",
        }
    b = mock_engagement_bundle(text)
    b["notes"] = "mock predict_traffic"
    return b


def _run_competitor_audit(params: dict[str, Any]) -> dict[str, Any]:
    """感知：mock 同行爆款标题/封面线索（参数驱动 niche/limit）。"""
    niche = str(params.get("niche", params.get("topic", "通用"))).strip()[:80]
    limit = max(1, min(12, int(params.get("limit", 5))))
    examples: list[dict[str, Any]] = []
    for i in range(limit):
        examples.append(
            {
                "title": f"[mock竞品{i + 1}] {niche} 切片",
                "cover_hint": random.choice(["大字报", "人脸特写", "对比图", "清单体"]),
                "est_ctr_pct": round(random.uniform(1.2, 8.0), 2),
            }
        )
    return {"niche": niche, "examples": examples, "notes": "mock competitor_audit"}


def _run_gen_body(params: dict[str, Any]) -> dict[str, Any]:
    """生产：正文。MiniMax 开启时由模型二创；否则 Mock。"""
    headline = str(params.get("headline", "")).strip()[:200]
    tone = str(params.get("tone", "steady")).strip()[:20]
    hints = params.get("style_hints") if isinstance(params.get("style_hints"), dict) else {}
    pace = str(hints.get("pace", "中速"))[:12]
    lead = str(hints.get("lead", "共情开场"))[:20]
    notes = "mock gen_body"
    body = ""
    if minimax_client.minimax_configured() and _env_flag("OPENCLAW_MINIMAX_BODY", True):
        sys_p = (
            "你是小红书正文写手。只输出一个 JSON 对象，禁止 markdown。"
            ' 键：body（string，180–480字为宜，分段用\\n）、cta（string，一句结尾互动）。'
        )
        user_p = json.dumps(
            {"headline": headline, "tone": tone, "pace": pace, "lead": lead, "style_hints": hints},
            ensure_ascii=False,
        )
        user_p += " 要求：情绪价值高、口语自然、可执行要点清晰，符合小红书阅读习惯。"
        parsed = minimax_client.MiniMaxClient().complete_json(sys_p, user_p)
        if parsed:
            main = str(parsed.get("body") or "").strip()
            cta = str(parsed.get("cta") or "").strip()
            body = (main + ("\n\n" + cta if cta else "")).strip()[:2000]
            if len(body) >= 8:
                notes = "minimax gen_body"
    if len(body) < 8:
        body = (
            f"（{tone}/{pace}/{lead}）承接「{headline[:48] or '主题'}」：先点出痛点，再给一条可执行小结，末句引导互动。"
        )[:800]
        notes = "mock gen_body"
    return {"tone": tone, "style_hints": hints, "body": body, "notes": notes}


def _run_tuning_params(params: dict[str, Any]) -> dict[str, Any]:
    """生产辅助：合并/裁剪风格参数（无状态）。"""
    base = params.get("base") if isinstance(params.get("base"), dict) else {}
    delta = params.get("delta") if isinstance(params.get("delta"), dict) else {}
    out: dict[str, Any] = {**{str(k): v for k, v in base.items()}, **{str(k): v for k, v in delta.items()}}
    for k in ("energy", "risk", "formality"):
        if k in out:
            try:
                out[k] = max(0.0, min(1.0, float(out[k])))
            except (TypeError, ValueError):
                del out[k]
    return {"tuning": out, "notes": "mock tuning_params"}


def _run_simulated_publish(params: dict[str, Any]) -> dict[str, Any]:
    """分发：mock 发布时刻与 publish_id（无状态，不持久化）。"""
    vid = str(params.get("variant_id", "v0.0"))[:40]
    headline = str(params.get("headline", "")).strip()[:200]
    channel = str(params.get("channel", "mock_short_video"))[:40]
    pid = f"pub_{uuid.uuid4().hex[:12]}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "publish_id": pid,
        "published_at": ts,
        "variant_id": vid,
        "channel": channel,
        "headline_excerpt": headline[:80],
        "notes": "mock simulated_publish",
    }


def _compound_wait_sec(params: dict[str, Any]) -> tuple[float, float]:
    """(实际休眠, 请求值)；实际值受 OPENCLAW_COMPOUND_WAIT_MAX_SEC 限制（mock 防真睡 24h）。"""
    try:
        req = float(params.get("wait_sec") or os.environ.get("OPENCLAW_COMPOUND_WAIT_SEC") or "2")
    except ValueError:
        req = 2.0
    try:
        cap = float(os.environ.get("OPENCLAW_COMPOUND_WAIT_MAX_SEC") or "15")
    except ValueError:
        cap = 15.0
    req = max(0.0, min(req, 86400.0))
    applied = max(0.0, min(req, max(0.0, cap)))
    return applied, req


def _run_publish_and_monitor(params: dict[str, Any]) -> dict[str, Any]:
    """
    复合算子：simulated_publish → 等待（mock，秒级可配）→ fetch_metrics。
    生产可增大 wait_sec / 环境变量；本地默认短休眠。
    """
    headline = str(params.get("headline", "")).strip()[:200]
    body = str(params.get("body", "")).strip()[:800]
    text = (headline + ("\n" + body if body else "")).strip()
    pub_params = {
        "variant_id": params.get("variant_id", "v0.0"),
        "headline": headline,
        "channel": params.get("channel", "mock_short_video"),
    }
    pub = _run_simulated_publish(pub_params)
    applied, requested = _compound_wait_sec(params)
    if applied > 0:
        time.sleep(applied)
    metrics = _run_fetch_metrics(
        {
            "headline": text or headline,
            "publish_id": pub.get("publish_id", ""),
        }
    )
    return {
        "compound": "publish_and_monitor",
        "wait_applied_sec": applied,
        "wait_requested_sec": requested,
        "publish": pub,
        "metrics": metrics,
        "notes": "mock publish_and_monitor compound",
    }


def _run_fetch_metrics(params: dict[str, Any]) -> dict[str, Any]:
    """分发：mock 拉取互动（由 headline/text/body 复算，与实验室同一套度量）。"""
    text = str(
        params.get("headline") or params.get("text") or params.get("body") or params.get("copy") or ""
    ).strip()
    pub = str(params.get("publish_id", "")).strip()
    if not text:
        return {
            "publish_id": pub or None,
            "predicted_likes": _likes_else(),
            "ctr_pct": 0.5,
            "impressions": 120,
            "boost_keyword": _boost_keyword(),
            "boost_keyword_hit": False,
            "notes": "mock fetch_metrics: empty",
        }
    b = mock_engagement_bundle(text)
    b["publish_id"] = pub or None
    b["notes"] = "mock fetch_metrics"
    return b


def _run_flow_echo(params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params, "notes": "debug echo"}


def _openclaw_data_dir() -> str:
    d = (os.environ.get("OPENCLAW_DATA_DIR") or "/app/data").strip()
    os.makedirs(d, exist_ok=True)
    return d


def _xhs_bindings_path() -> str:
    return os.path.join(_openclaw_data_dir(), "xhs_bindings.json")


def _load_xhs_bindings() -> dict[str, Any]:
    path = _xhs_bindings_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            row = json.load(f)
        return row if isinstance(row, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_xhs_bindings(data: dict[str, Any]) -> None:
    path = _xhs_bindings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_engagement_count(raw: Any) -> int:
    """把 API 里常见的 '1.2k' / '3万' / '1,204' 转成 int，避免 Hermes 数值引擎崩掉。"""
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    t = str(raw).strip().lower().replace(",", "").replace(" ", "")
    if not t:
        return 0
    mult = 1
    if t.endswith("w") or t.endswith("万"):
        mult = 10_000
        t = t[:-1]
    elif t.endswith("k"):
        mult = 1000
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return 0


def _run_prepare_xhs_post(params: dict[str, Any]) -> dict[str, Any]:
    """
    生产算子：发布清单（手动发帖）。experiment_variant_id = Hermes 的 variant_id，发布后 real_note_id 与之绑定，无 manifest_id。
    若 params 含 headline/body/hashtags（来自 xhs_factory 流水线），则优先采用。
    模板文案见 prompts/xhs/prepare_xhs_post.yaml。
    """
    pack = prompt_store.load_xhs_prompt("prepare_xhs_post")
    topic = str(params.get("topic", "流量主题")).strip()[:100]
    topic_16 = topic[:16] if topic else "流量"
    tone = str(params.get("tone", "steady")).strip()[:24]
    vid = str(params.get("variant_id", "v1.0")).strip()[:80]
    kw = _boost_keyword()
    ph = str(params.get("headline", "")).strip()
    pb = str(params.get("body", "")).strip()
    # 与 Hermes recreate 校验同向：过短不要用英文 topic 兜底盖住模型输出（曾出现2～3 字标题 + 8～19 字正文触发模板）
    _min_hl = 2
    _min_body_raw = 8
    headline = ph[:80] if len(ph) >= _min_hl else ""
    hs = pack.get("headline_when_short") if isinstance(pack.get("headline_when_short"), dict) else {}
    tpl_a = str(hs.get("template_a") or "{topic}·{tone}视角")
    tpl_b = str(hs.get("template_b") or "{kw}·{topic}")
    fmt_kw = {"topic": topic, "tone": tone, "kw": kw, "topic_40": topic[:40], "topic_16": topic_16}
    if not headline:
        hook_contrast = str(hs.get("hook_contrast") or "").strip()
        hook_number = str(hs.get("hook_number") or "").strip()
        hook_warn = str(hs.get("hook_warn") or "").strip()
        hooks = [h for h in (hook_contrast, hook_number, hook_warn) if h]
        picked = ""
        if hooks:
            idx = int(hashlib.md5(topic.encode("utf-8", errors="replace")).hexdigest(), 16) % len(hooks)
            try:
                picked = hooks[idx].format(**fmt_kw).strip()[:80]
            except (KeyError, ValueError):
                picked = ""
        if len(picked) >= 6:
            headline = picked
        else:
            headline = tpl_a.format(**fmt_kw)[:80]
            if len(headline.strip()) < 6:
                headline = tpl_b.format(**fmt_kw)[:80]
    body = pb[:2000] if len(pb) >= _min_body_raw else ""
    if not body:
        body_tpl = str(pack.get("body_when_short") or "").strip()
        if body_tpl:
            try:
                body = body_tpl.format(**fmt_kw)[:2000]
            except (KeyError, ValueError):
                body = (
                    f"最近老刷到「{topic_16}」相关的内容，我也踩过坑。\n"
                    f"把我自己的判断标准摊开来（无广，纯经历）。你卡在哪一步？{kw} 相关我看到就回～"
                )[:2000]
        else:
            body = (
                f"最近老刷到「{topic_16}」相关的内容，我也踩过坑。\n"
                f"把我自己的判断标准摊开来（无广，纯经历）。你卡在哪一步？{kw} 相关我看到就回～"
            )[:2000]
    raw_tags = params.get("hashtags")
    if isinstance(raw_tags, list) and len(raw_tags) >= 3:
        hashtags = [str(x).strip()[:24] for x in raw_tags[:12] if str(x).strip()]
    else:
        hf = pack.get("hashtags_fallback") if isinstance(pack.get("hashtags_fallback"), dict) else {}
        try:
            ts = int(hf.get("topic_slice", 10))
        except (TypeError, ValueError):
            ts = 10
        try:
            tone_slice = int(hf.get("tone_slice", 8))
        except (TypeError, ValueError):
            tone_slice = 8
        topic_fb = str(hf.get("topic_empty_fallback", "复盘"))
        tone_fb = str(hf.get("tone_empty_fallback", "成长"))
        mid = str(hf.get("static_middle", "干货分享"))
        tshort = topic[:ts] if topic[:ts] else topic_fb
        ttag = tone[:tone_slice] if tone[:tone_slice] else tone_fb
        hashtags = [tshort, mid, ttag]
    ip_tpl = str(pack.get("image_prompt") or "").strip()
    if ip_tpl:
        image_prompt = ip_tpl.format(headline_24=headline[:24], tone=tone, topic=topic, kw=kw)
    else:
        image_prompt = (
            f"小红书竖版封面 1080x1440，主标题「{headline[:24]}」，风格偏{tone}，留白清晰、对比强。"
        )
    fc = params.get("factory_context")
    pc = pack.get("post_checklist") if isinstance(pack.get("post_checklist"), dict) else {}
    boost_where = str(pc.get("boost_where", "标题或正文前 80 字"))
    emoji_note = str(pc.get("emoji_note", "正文或标题旁至少 1 个相关 emoji"))
    cta_note = str(pc.get("cta_note", "结尾引导评论/收藏/关注其一"))
    notes_out = str(
        pack.get("notes")
        or "Post manually on XHS, then call Hermes POST /task/{id}/xhs-sync with real_note_id."
    )
    post_checklist: dict[str, Any] = {
        "must_include_boost_keyword": {"keyword": kw, "where": boost_where},
        "emoji": {"min_count": 1, "note": emoji_note},
        "image": {"aspect_ratio": "1080x1440", "min_short_edge_px": 1080},
        "hashtags": {"min_count": 3, "suggested": hashtags},
        "cta": {"note": cta_note},
    }
    if isinstance(fc, dict) and fc:
        post_checklist["factory_context"] = fc
    return {
        "experiment_variant_id": vid,
        "headline": headline,
        "body": body,
        "hashtags": hashtags,
        "image_prompt": image_prompt,
        "post_checklist": post_checklist,
        "awaiting_manual_publish": True,
        "notes": notes_out,
    }


def _run_extract_viral_patterns(params: dict[str, Any]) -> dict[str, Any]:
    topic = str(params.get("topic", "")).strip()
    try:
        n = int(params.get("sample_size", 12))
    except (TypeError, ValueError):
        n = 12
    return xhs_factory.extract_viral_patterns(topic, n)


def _run_recreate_content(params: dict[str, Any]) -> dict[str, Any]:
    original = str(params.get("original_text", "")).strip()
    gene = params.get("gene_sop", {})
    style = str(params.get("style", "sharp")).strip()
    return xhs_factory.recreate_content(original, gene, style)


def _run_predict_viral_score(params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("recreated_text", "")).strip()
    gene = params.get("gene_sop", {})
    lib = params.get("case_library")
    cl = lib if isinstance(lib, list) else None
    raw_hint = params.get("like_proxy_hint")
    if raw_hint is None:
        raw_hint = params.get("like_proxy")
    like_hint: int | None = None
    if raw_hint is not None:
        try:
            v = int(raw_hint)
            if v >= 1:
                like_hint = v
        except (TypeError, ValueError):
            like_hint = None
    v1_hints: dict[str, Any] = {}
    for param_key, hint_key in (
        ("comment_proxy_hint", "comment_proxy"),
        ("collect_proxy_hint", "collect_proxy"),
        ("share_proxy_hint", "share_proxy"),
        ("published_at_hint", "published_at"),
    ):
        if params.get(param_key) is not None and str(params.get(param_key)).strip() != "":
            v1_hints[hint_key] = params[param_key]
    return xhs_factory.predict_viral_score(
        text,
        gene,
        cl,
        like_proxy_hint=like_hint,
        baseline_v1_hints=v1_hints if v1_hints else None,
    )


def _run_sync_manual_result(params: dict[str, Any]) -> dict[str, Any]:
    """手动发帖完成后：variant_id ↔ real_note_id 单表绑定（少一层 manifest_id）。"""
    vid = str(params.get("variant_id", "")).strip()
    note_id = str(params.get("real_note_id", "")).strip()
    if not vid or not note_id:
        return {"bound": False, "error": "variant_id and real_note_id required"}
    pub_at = str(params.get("published_at", "")).strip()
    if not pub_at:
        pub_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    b = _load_xhs_bindings()
    b[vid] = {
        "real_note_id": note_id,
        "published_at": pub_at,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_xhs_bindings(b)
    return {
        "experiment_variant_id": vid,
        "real_note_id": note_id,
        "published_at": pub_at,
        "bound": True,
        "notes": "Binding stored under OPENCLAW_DATA_DIR/xhs_bindings.json",
    }


def _run_fetch_xhs_metrics(params: dict[str, Any]) -> dict[str, Any]:
    """
    监控算子：拉笔记指标。真实环境可替换为 HTTP/第三方 API；此处 mock 滑动窗口（check_index 越大数据越「长成」）。
    返回标准 likes/ctr/impressions + verify_defer（样本不足则延迟判定，不当作最终失败）。
    """
    note_id = str(params.get("note_id", "")).strip()
    vid = str(params.get("variant_id", "")).strip()
    if not note_id and vid:
        row = _load_xhs_bindings().get(vid)
        if isinstance(row, dict):
            note_id = str(row.get("real_note_id", "")).strip()
    check_index = int(params.get("check_index", 0) or 0)
    raw_like = params.get("raw_like_count")
    if raw_like is not None:
        likes = parse_engagement_count(raw_like)
    else:
        # 模拟冷启动：第 1 档不足样本 → defer；第 2 档过线；第 3 档更高（与 Hermes pass_min 默认 500 对齐）
        curve = [400, 650, 1500]
        likes = curve[min(max(check_index, 0), len(curve) - 1)]
    hit = likes >= _pass_min_hermes_hint()
    ctr = round(2.1 + min(check_index, 3) * 1.4 + random.uniform(0, 0.8), 2) if hit else round(0.4 + random.uniform(0, 0.5), 2)
    imps = max(300, int(likes * (55 + random.uniform(0, 15))))
    collects = max(0, int(likes * 0.08))
    comments = max(0, int(likes * 0.02))
    sample_sufficient = likes >= int(_pass_min_hermes_hint() * 0.85)
    verify_defer = not sample_sufficient
    defer_reason = "insufficient_engagement_for_decision" if verify_defer else ""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "note_id": note_id or None,
        "experiment_variant_id": vid or None,
        "predicted_likes": int(likes),
        "collects": collects,
        "comments": comments,
        "ctr_pct": ctr,
        "impressions": imps,
        "boost_keyword": _boost_keyword(),
        "boost_keyword_hit": hit,
        "sample_sufficient": sample_sufficient,
        "verify_defer": verify_defer,
        "defer_reason": defer_reason,
        "check_timestamp": ts,
        "check_index": check_index,
        "raw_api_shape": {"like_count_display": str(likes), "note": "mock; replace with platform JSON"},
        "notes": "mock fetch_xhs_metrics — wire real API here",
    }


Operator = Callable[[dict[str, Any]], dict[str, Any]]

OPERATORS: dict[str, Operator] = {
    "analyze_trends": _run_analyze_trends,
    "competitor_audit": _run_competitor_audit,
    "generate_headline": _run_generate_headline,
    "generate_hook_lines": _run_generate_hook_lines,
    "gen_body": _run_gen_body,
    "tuning_params": _run_tuning_params,
    "simulated_publish": _run_simulated_publish,
    "fetch_metrics": _run_fetch_metrics,
    "publish_and_monitor": _run_publish_and_monitor,
    "predict_traffic": _run_predict_traffic,
    "prepare_xhs_post": _run_prepare_xhs_post,
    "extract_viral_patterns": _run_extract_viral_patterns,
    "recreate_content": _run_recreate_content,
    "predict_viral_score": _run_predict_viral_score,
    "sync_manual_result": _run_sync_manual_result,
    "fetch_xhs_metrics": _run_fetch_xhs_metrics,
    "flow_echo": _run_flow_echo,
}


@app.get("/health")
def health():
    return {"status": "ok", "role": "executor"}


@app.get("/actions")
def list_actions():
    return {"actions": sorted(OPERATORS.keys())}


@app.post("/process", response_model=ProcessResponse)
def process(req: ProcessRequest) -> ProcessResponse:
    op = OPERATORS.get(req.action)
    if op is None:
        return ProcessResponse(
            task_id=req.task_id,
            status="error",
            action=req.action,
            result=None,
            metrics={"gpu": _gpu_snapshot()},
            error=f"unknown action: {req.action}",
        )

    t0 = time.perf_counter()
    gpu_before = _gpu_snapshot()
    try:
        result = op(req.params)
    except Exception as e:
        return ProcessResponse(
            task_id=req.task_id,
            status="error",
            action=req.action,
            result=None,
            metrics={
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                "gpu": gpu_before,
            },
            error=str(e),
        )

    latency_ms = round((time.perf_counter() - t0) * 1000, 3)
    return ProcessResponse(
        task_id=req.task_id,
        status="success",
        action=req.action,
        result=result,
        metrics={
            "latency_ms": latency_ms,
            "gpu": gpu_before,
        },
        error=None,
    )


@app.get("/test-link")
def test_link():
    base = (os.environ.get("HERMES_URL") or "").strip().rstrip("/")
    if not base:
        return {
            "ok": False,
            "role": "executor",
            "peer": "hermes",
            "target": None,
            "peer_health": None,
            "error": "HERMES_URL is empty",
        }
    target = f"{base}/health"
    data, err = _fetch_json(target)
    peer_ok = (
        err is None
        and isinstance(data, dict)
        and data.get("status") == "ok"
        and data.get("role") == "orchestrator"
    )
    return {
        "ok": peer_ok,
        "role": "executor",
        "peer": "hermes",
        "target": target,
        "peer_health": data,
        "error": err,
    }


@app.get("/")
def root():
    mm = (os.environ.get("MINIMAX_MODEL") or "").strip()
    probe = minimax_client.minimax_key_probe()
    return {
        "service": "openclaw",
        "hermes_url": os.environ.get("HERMES_URL", ""),
        "minimax_ready": minimax_client.minimax_configured(),
        "minimax_key_ok": bool(probe.get("ok")),
        "minimax_probe": {
            "ok": bool(probe.get("ok")),
            "cached": bool(probe.get("cached")),
            "error": probe.get("error"),
            "base_status_code": probe.get("base_status_code"),
            "skipped": bool(probe.get("skipped")),
        },
        "minimax_model": mm or None,
        "traffic_sim": {
            "boost_keyword": _boost_keyword(),
            "likes_if_hit": _likes_if_hit(),
            "likes_else": _likes_else(),
            "pass_min_hermes_hint": _pass_min_hermes_hint(),
            "ctr_pass_min_phint": (os.environ.get("TRAFFIC_SIM_CTR_PASS_MIN") or "3.0"),
        },
        "endpoints": ["/process", "/actions", "/health"],
    }
