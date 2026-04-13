"""
MiniMax HTTP 客户端（stdlib only）：供 OpenClaw 算子与 xhs_factory 调用。

默认对接官方 Text Chat V2：`POST .../v1/text/chatcompletion_v2`。
可通过 `MINIMAX_BASE_URL` 覆盖为兼容端点（如 OpenAI-style）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# GET / 等场景的密钥探测结果缓存：(monotonic 过期时间, payload)
_probe_cache: tuple[float, dict[str, Any]] | None = None


def minimax_configured() -> bool:
    return bool((os.environ.get("MINIMAX_API_KEY") or "").strip())


def _probe_cache_ttl_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("MINIMAX_PROBE_CACHE_SEC") or "120"))
    except ValueError:
        return 120.0


def _probe_timeout_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("MINIMAX_PROBE_TIMEOUT_SEC") or "25"))
    except ValueError:
        return 25.0


def minimax_key_probe(*, force: bool = False) -> dict[str, Any]:
    """
    轻量请求 MiniMax，区分「配置了 KEY」与「KEY 被服务端接受」。
    结果带短 TTL 缓存（MINIMAX_PROBE_CACHE_SEC，0=不缓存），避免频繁刷新页面打爆配额。
    """
    global _probe_cache
    empty_key: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        "error": "MINIMAX_API_KEY empty",
        "cached": False,
        "base_status_code": None,
    }
    if not minimax_configured():
        return dict(empty_key)

    ttl = _probe_cache_ttl_sec()
    now = time.monotonic()
    if not force and ttl > 0 and _probe_cache is not None:
        exp, cached = _probe_cache
        if now < exp:
            out = dict(cached)
            out["cached"] = True
            return out

    c = MiniMaxClient()
    try:
        mt = int(os.environ.get("MINIMAX_PROBE_MAX_TOKENS") or "64")
    except ValueError:
        mt = 64
    mt = max(16, min(256, mt))
    payload: dict[str, Any] = {
        "model": c.model,
        "messages": [
            {"role": "system", "content": "Reply with only the single character A."},
            {"role": "user", "content": "Now."},
        ],
        "temperature": 0.01,
        "stream": False,
        "max_completion_tokens": mt,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        c.base_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {c.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    result: dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "error": None,
        "cached": False,
        "base_status_code": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=_probe_timeout_sec()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        result["error"] = f"http_{e.code}: {err_body[:200]}"
        if ttl > 0:
            _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
        return result
    except json.JSONDecodeError as e:
        result["error"] = f"invalid json response: {e}"[:240]
        if ttl > 0:
            _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
        return result
    except Exception as e:
        logger.warning("MiniMax probe request error: %s", e)
        result["error"] = str(e)[:240]
        if ttl > 0:
            _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
        return result

    if not isinstance(data, dict):
        result["error"] = "response is not a JSON object"
        if ttl > 0:
            _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
        return result

    base = data.get("base_resp")
    if isinstance(base, dict):
        code = base.get("status_code")
        result["base_status_code"] = code
        if code not in (None, 0):
            msg = str(base.get("status_msg") or "")
            result["error"] = f"{code}: {msg}"[:240]
            if ttl > 0:
                _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
            return result

    text = _content_from_response(data)
    if not (text and text.strip()):
        result["error"] = "empty completion (check key, model, MINIMAX_GROUP_ID)"
        if ttl > 0:
            _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
        return result

    result["ok"] = True
    result["error"] = None
    if ttl > 0:
        _probe_cache = (now + ttl, {k: v for k, v in result.items() if k != "cached"})
    return result


def _timeout_sec() -> float:
    try:
        return max(15.0, float(os.environ.get("MINIMAX_TIMEOUT_SEC") or "120"))
    except ValueError:
        return 120.0


def _default_base_url() -> str:
    return (os.environ.get("MINIMAX_BASE_URL") or "").strip() or "https://api.minimax.io/v1/text/chatcompletion_v2"


def _model() -> str:
    return (os.environ.get("MINIMAX_MODEL") or "MiniMax-M2.7").strip() or "MiniMax-M2.7"


def _append_group_id(url: str) -> str:
    gid = (os.environ.get("MINIMAX_GROUP_ID") or "").strip()
    if not gid:
        return url
    parsed = urlparse(url)
    q = list(parse_qsl(parsed.query, keep_blank_values=True))
    if not any(k == "GroupId" for k, _ in q):
        q.append(("GroupId", gid))
    return urlunparse(parsed._replace(query=urlencode(q)))


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出中尽量抽出单个 JSON 对象（去 markdown 围栏、截断杂质）。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _content_from_response(data: Any) -> str:
    """解析 MiniMax chatcompletion_v2 或常见 OpenAI-compat 形态。"""
    if not isinstance(data, dict):
        return ""
    base = data.get("base_resp")
    if isinstance(base, dict):
        code = base.get("status_code")
        if code not in (None, 0):
            msg = base.get("status_msg") or ""
            logger.warning("MiniMax base_resp status_code=%s msg=%s", code, msg)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0]
        if isinstance(c0, dict):
            msg = c0.get("message")
            if isinstance(msg, dict) and msg.get("content"):
                return str(msg.get("content") or "").strip()
            delta = c0.get("delta")
            if isinstance(delta, dict) and delta.get("content"):
                return str(delta.get("content") or "").strip()
    if isinstance(data.get("message"), dict):
        return str(data["message"].get("content") or "").strip()
    rep = data.get("reply")
    if isinstance(rep, str) and rep.strip():
        return rep.strip()
    resp = data.get("response")
    if isinstance(resp, dict):
        return str(resp.get("content") or resp.get("text") or "").strip()
    if isinstance(resp, str):
        return resp.strip()
    return ""


class MiniMaxClient:
    def __init__(self) -> None:
        self.api_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
        self.base_url = _append_group_id(_default_base_url())
        self.model = _model()

    def complete_chat(self, system_prompt: str, user_prompt: str, *, temperature: float | None = None) -> str:
        if not self.api_key:
            logger.error("MINIMAX_API_KEY is missing")
            return ""
        temp = 0.2 if temperature is None else max(0.01, min(1.0, float(temperature)))
        try:
            max_tokens = int(os.environ.get("MINIMAX_MAX_COMPLETION_TOKENS") or "2048")
        except ValueError:
            max_tokens = 2048
        max_tokens = max(256, min(8192, max_tokens))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temp,
            "stream": False,
        }
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_timeout_sec()) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                err_body = ""
            logger.exception("MiniMax HTTP %s: %s", e.code, err_body)
            return ""
        except Exception as e:
            logger.exception("MiniMax request error: %s", e)
            return ""
        return _content_from_response(data)

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        text = self.complete_chat(system_prompt, user_prompt, temperature=0.15)
        return extract_json_object(text)
