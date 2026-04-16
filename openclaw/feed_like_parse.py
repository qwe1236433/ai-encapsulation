"""
从爬虫/导出原始字段解析点赞类整数，供 xhs_factory 与 export_to_xhs_feed 共用。

支持：int/float、纯数字字符串、含「万」「w」的简写（如 6.5万）、千分位逗号。
解析失败返回 None，由调用方决定默认值（通常为 100）。
"""

from __future__ import annotations

import math
import re
from typing import Any


def parse_like_proxy_value(v: Any) -> int | None:
    """
    返回 >=1 的整数；无法解析时返回 None（非「缺字段」——缺字段由调用方在取 raw 前判断）。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        if v < 1:
            return None
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        n = int(round(v))
        return n if n >= 1 else None

    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    s = s.replace(",", "").replace("，", "").replace(" ", "")

    # 6.5万、12万
    if "万" in s:
        m = re.match(r"^([\d.]+)\s*万", s)
        if m:
            try:
                n = int(round(float(m.group(1)) * 10000))
                return n if n >= 1 else None
            except ValueError:
                return None

    # 4.1w（部分导出）
    m2 = re.match(r"^([\d.]+)\s*([wW])$", s)
    if m2:
        try:
            n = int(round(float(m2.group(1)) * 10000))
            return n if n >= 1 else None
        except ValueError:
            return None

    try:
        x = float(s)
        n = int(round(x))
        return n if n >= 1 else None
    except ValueError:
        return None


def like_proxy_with_default(v: Any, *, default: int = 100) -> int:
    """与历史行为一致：解析失败或过小则用 default，再 max(1, …)。"""
    p = parse_like_proxy_value(v)
    if p is None:
        p = default
    return max(1, int(p))
