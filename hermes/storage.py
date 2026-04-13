"""会话与落盘：短期轨迹（SESSIONS）根路径与原子写。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def sessions_dir() -> str:
    return (os.environ.get("HERMES_SESSIONS_DIR") or "/app/sessions").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_task_filename(task_id: str) -> bool:
    if not task_id or len(task_id) > 128:
        return False
    if ".." in task_id or "/" in task_id or "\\" in task_id:
        return False
    return all(c.isalnum() or c in "-_" for c in task_id)


def session_path(task_id: str) -> str:
    return os.path.join(sessions_dir(), f"{task_id}.json")


def atomic_write_session(path: str, data: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
