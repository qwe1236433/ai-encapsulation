"""真测 MiniMax 中文能力（脚本文件，避免 PowerShell stdin 编码损坏）。"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read_key() -> str:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("MINIMAX_API_KEY="):
            return line.strip().split("=", 1)[1]
    return ""


def call_minimax(messages: list[dict], model: str = "MiniMax-M2.7", max_tokens: int = 400) -> tuple[float, str]:
    key = read_key()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.minimaxi.com/v1/text/chatcompletion_v2",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
        },
    )
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=60).read()
        obj = json.loads(r.decode("utf-8"))
        cost = time.time() - t0
        content = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
        return cost, content
    except urllib.error.HTTPError as e:
        cost = time.time() - t0
        return cost, f"[HTTP_{e.code}] {e.read().decode('utf-8', 'replace')[:300]}"


if __name__ == "__main__":
    cost, resp = call_minimax(
        [
            {"role": "system", "content": "你是小红书内容运营顾问，用简体中文回答，简洁具体，不讲空话。"},
            {"role": "user", "content": "用一句话（不超过50字）解释：小红书标题结尾带问号为什么更难上热门？"},
        ]
    )
    print(f"=== MiniMax-M2.7 真实中文响应 cost={cost:.1f}s ===")
    print(resp)
