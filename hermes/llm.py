"""LLMManager：Ollama /api/chat 与 /api/generate 封装，供 S1 规划与 S3 软评审。"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

import settings

logger = logging.getLogger(__name__)


class LLMManager:
    """对 Ollama HTTP API 的薄封装；模型名与 Host 由环境变量驱动。"""

    def __init__(self) -> None:
        self.base = settings.ollama_host()
        self.timeout = settings.ollama_timeout_sec()

    def chat(self, user: str, system: str | None = None) -> str:
        last: Exception | None = None
        for model in settings.hermes_model_candidates():
            try:
                return self._complete_one_model(model, user, system)
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")[:300]
                last = e
                if e.code in (400, 404):
                    logger.warning("ollama model=%s HTTP %s: %s", model, e.code, err)
                    continue
                logger.warning("ollama model=%s HTTP %s: %s", model, e.code, err)
                raise
            except Exception as e:
                last = e
                logger.warning("ollama model=%s error: %s", model, e)
                continue
        if last:
            raise last
        return ""

    def _complete_one_model(self, model: str, user: str, system: str | None) -> str:
        try:
            return self._api_chat(model, user, system)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            logger.info("ollama /api/chat 404 for model=%s, trying /api/generate", model)
            return self._api_generate(model, user, system)

    def _api_chat(self, model: str, user: str, system: str | None) -> str:
        url = f"{self.base}/api/chat"
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = json.dumps(
            {"model": model, "messages": messages, "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            msg = data.get("message") if isinstance(data, dict) else None
            if isinstance(msg, dict):
                return str(msg.get("content") or "").strip()
            return ""

    def _api_generate(self, model: str, user: str, system: str | None) -> str:
        url = f"{self.base}/api/generate"
        prompt = user if not system else f"{system}\n\n{user}"
        body = json.dumps(
            {"model": model, "prompt": prompt, "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict) and "response" in data:
                return str(data.get("response") or "").strip()
            return ""

    def plan_tactical_intent(self, goal: str, case_context: str) -> dict[str, Any]:
        """根据 goal 与案例摘要输出结构化战术意图；解析失败则由调用方降级。"""
        actions_literal = ", ".join(f'"{a}"' for a in settings.FLOW_ACTIONS)
        system = (
            "You reply with ONLY a single JSON object, no markdown fences. "
            f"Keys: action (string, must be one of: {actions_literal}), "
            "params (object, MUST include all required fields for that action), variant_id (string).\n"
            "Required: predict_traffic needs params.headline (non-empty string). "
            "generate_headline needs topic, tone, boost. analyze_trends needs symbol, window_min (number).\n"
            "Example: {\"action\":\"generate_headline\",\"params\":{\"topic\":\"复盘\",\"tone\":\"sharp\",\"boost\":false},\"variant_id\":\"v1.llm\"}"
        )
        user = f"User goal:\n{goal}\n\nRelevant past successes (JSON lines, may be empty):\n{case_context or '(none)'}\n\nChoose the first OpenClaw action and params."
        text = self.chat(user, system)
        obj = _extract_json_object(text)
        if not obj:
            raise ValueError(f"LLM did not return parseable JSON: {text[:200]!r}")
        normalized = _normalize_plan(obj)
        if not normalized:
            raise ValueError(f"LLM plan failed validation: {obj!r}")
        return normalized


def _extract_json_object(s: str) -> dict[str, Any] | None:
    s = (s or "").strip()
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


def _normalize_plan(obj: dict[str, Any]) -> dict[str, Any] | None:
    action = str(obj.get("action") or "").strip()
    if action not in settings.FLOW_ACTIONS:
        return None
    params = obj.get("params")
    if not isinstance(params, dict):
        params = {}
    vid = str(obj.get("variant_id") or "v1.llm").strip() or "v1.llm"
    return {"action": action, "params": params, "variant_id": vid}
