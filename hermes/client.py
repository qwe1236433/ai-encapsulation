"""对 OpenClaw 战术核心的 HTTP 调用（无业务分支）。"""

from __future__ import annotations

import json
import os
import uuid
import urllib.error
import urllib.request
from typing import Any

import settings


def fetch_json(url: str) -> tuple[Any | None, str | None]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=settings.LINK_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw), None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        return None, f"HTTP {e.code}: {e.reason} {err_body}".strip()
    except Exception as e:
        return None, str(e)


def post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[Any | None, str | None]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw), None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            err_body = ""
        return None, f"HTTP {e.code}: {e.reason} {err_body}".strip()
    except Exception as e:
        return None, str(e)


def dispatch(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
    timeout_sec: float = settings.PROCESS_TIMEOUT_SEC,
) -> dict[str, Any]:
    base = (os.environ.get("OPENCLAW_URL") or "").strip().rstrip("/")
    if not base:
        return {
            "task_id": task_id or "",
            "status": "error",
            "action": action,
            "hermes_error": "OPENCLAW_URL is empty",
            "openclaw": None,
        }

    tid = task_id or str(uuid.uuid4())
    payload = {"task_id": tid, "action": action, "params": params or {}}
    url = f"{base}/process"
    data, err = post_json(url, payload, timeout=timeout_sec)
    if err is not None:
        return {
            "task_id": tid,
            "status": "error",
            "action": action,
            "hermes_error": err,
            "openclaw": data,
        }
    if not isinstance(data, dict):
        return {
            "task_id": tid,
            "status": "error",
            "action": action,
            "hermes_error": "invalid JSON from OpenClaw",
            "openclaw": data,
        }
    return {
        "task_id": tid,
        "status": data.get("status", "error"),
        "action": action,
        "hermes_error": None,
        "openclaw": data,
    }
