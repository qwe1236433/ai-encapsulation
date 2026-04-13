"""
从 YAML 加载小红书生产链提示词与模板（方案1：外置、Git 管理）。

环境变量 OPENCLAW_PROMPTS_DIR 可指向自定义目录（与默认 openclaw/prompts/xhs 一样，目录内直接放各 .yaml）。
"""

from __future__ import annotations

import os
from pathlib import Path
from string import Template
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
    """user侧模板使用 $topic 形式，避免 JSON 样本里的 { 与 str.format 冲突。"""
    return Template(template).substitute(**kwargs)
