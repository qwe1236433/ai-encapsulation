"""
从 YAML 加载小红书生产链提示词与模板（方案1：外置、Git 管理）。

环境变量 OPENCLAW_PROMPTS_DIR 可指向自定义目录（与默认 openclaw/prompts/xhs 一样，目录内直接放各 .yaml）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_XHS_DIR = Path(__file__).resolve().parent / "prompts" / "xhs"


def xhs_prompts_dir() -> Path:
    raw = (os.environ.get("OPENCLAW_PROMPTS_DIR") or "").strip()
    if raw:
        return Path(raw)
    return _XHS_DIR


def load_xhs_prompt(name: str) -> dict[str, Any]:
    """
    加载 prompts/xhs/{name}.yaml，返回解析后的 dict。
    name 不含 .yaml 后缀，例如 extract_viral_patterns。
    """
    path = xhs_prompts_dir() / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"XHS prompt file missing: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"XHS prompt root must be a mapping: {path}")
    return data


def substitute_user_template(template: str, **kwargs: str) -> str:
    """user侧模板使用 $name占位符；用正则替换，避免 string.Template 对 $ 敏感及未知键抛错。"""
    if not template:
        return ""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in kwargs:
            return str(kwargs[key])
        return m.group(0)

    return re.sub(r"\$(\w+)", repl, template)
