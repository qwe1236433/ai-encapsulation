"""
MiniMax 最小调用 helper —— Hermes 包内部共享（tuner + auditor）。

只依赖标准库（urllib），不引入第三方 SDK。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

MINIMAX_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
MINIMAX_MODEL_DEFAULT = "MiniMax-M2.7"
MINIMAX_TIMEOUT_SEC = 60
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_minimax_key() -> str:
    env_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if env_key:
        return env_key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MINIMAX_API_KEY="):
                return line.strip().split("=", 1)[1]
    return ""


def call_minimax(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = MINIMAX_MODEL_DEFAULT,
    max_tokens: int = 2400,
    timeout: int = MINIMAX_TIMEOUT_SEC,
) -> tuple[bool, str, float]:
    """返回 (ok, content_or_error, cost_sec)。

    MiniMax-M2.7 有独立 `reasoning_content` 字段，会吃掉大量 token；若 `content` 空，
    则从 `reasoning_content` 末尾尝试抠出最后一个 JSON 对象作为兜底。
    """
    import re

    key = read_minimax_key()
    if not key:
        return False, "MINIMAX_API_KEY missing", 0.0
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MINIMAX_URL,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
        },
    )
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout).read()
        cost = time.time() - t0
        obj = json.loads(r.decode("utf-8"))
        msg = obj.get("choices", [{}])[0].get("message", {}) or {}
        content = msg.get("content", "") or ""
        if not content:
            reasoning = msg.get("reasoning_content", "") or ""
            m = re.search(r"\{[\s\S]*\}\s*$", reasoning)
            if m:
                content = m.group(0)
        return True, content, cost
    except urllib.error.HTTPError as e:
        cost = time.time() - t0
        return False, f"HTTP_{e.code}: {e.read().decode('utf-8', 'replace')[:300]}", cost
    except Exception as e:
        cost = time.time() - t0
        return False, f"{type(e).__name__}: {str(e)[:300]}", cost
