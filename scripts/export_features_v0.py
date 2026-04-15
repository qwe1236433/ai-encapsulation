"""
从工厂用 samples.json（JSON 数组）导出研究用特征表 CSV（feature_schema v0）。

用法（仓库根目录）:

  python scripts/export_features_v0.py --samples openclaw/data/xhs-feed/samples.json --out research/features_v0.csv

可选：按点赞阈值生成操作化二分类标签 y_rule（非平台「爆文」真值）:

  python scripts/export_features_v0.py --samples ... --out research/features_v0.csv --viral-threshold 1000

或从数据契约 JSON 读取阈值（推荐与实验报告一致；示例见 research/labels_spec.example.json）:

  python scripts/export_features_v0.py --samples ... --out research/features_v0.csv --labels-spec research/labels_spec.json

若同时传入 --viral-threshold 与 --labels-spec，以命令行 --viral-threshold 为准。

定义见 research/schema_notes.md 与 research/EXPERIMENT_REPORT.md。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


def _load_viral_threshold_from_spec(path: Path) -> int | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    for key in ("viral_like_threshold", "viral_threshold"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def _rows_from_samples(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def main() -> int:
    ap = argparse.ArgumentParser(description="samples.json → research/features_v0.csv")
    ap.add_argument(
        "--samples",
        type=str,
        default="openclaw/data/xhs-feed/samples.json",
        help="JSON 数组路径",
    )
    ap.add_argument(
        "--out",
        type=str,
        default="research/features_v0.csv",
        help="输出 CSV",
    )
    ap.add_argument(
        "--viral-threshold",
        type=int,
        default=None,
        help="若设置，则 y_rule = 1 when like_proxy >= T else 0（显式传入时优先于 --labels-spec）",
    )
    ap.add_argument(
        "--labels-spec",
        type=str,
        default="",
        help="JSON 路径，读取 viral_like_threshold（或 viral_threshold）；可与 example 对齐复制为 research/labels_spec.json",
    )
    args = ap.parse_args()

    inp = Path(args.samples).expanduser().resolve()
    if not inp.is_file():
        print(f"找不到输入文件: {inp}", flush=True)
        return 2

    rows = _rows_from_samples(inp)
    outp = Path(args.out).expanduser().resolve()
    outp.parent.mkdir(parents=True, exist_ok=True)

    viral_t = args.viral_threshold
    if viral_t is None and (args.labels_spec or "").strip():
        spec_path = Path(args.labels_spec).expanduser().resolve()
        if not spec_path.is_file():
            print(f"找不到 --labels-spec 文件: {spec_path}", flush=True)
            return 2
        viral_t = _load_viral_threshold_from_spec(spec_path)
        if viral_t is None:
            print(
                f"{spec_path} 中未找到有效的 viral_like_threshold / viral_threshold（整数）",
                flush=True,
            )
            return 2
        print(f"使用标签契约: {spec_path} → viral_like_threshold={viral_t}", flush=True)

    fieldnames = [
        "row_index",
        "title_len",
        "body_len",
        "like_proxy",
        "log1p_like",
        "sop_tag",
        "emotion_tag",
        "y_rule",
    ]

    with outp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(rows):
            title = str(r.get("title_hint") or "").strip()
            body = str(r.get("body_hint") or "").strip()
            try:
                lk = int(r.get("like_proxy") or 0)
            except (TypeError, ValueError):
                lk = 0
            lk = max(0, lk)
            sop = str(r.get("sop_tag") or "").strip()
            emo = str(r.get("emotion_tag") or "").strip()
            y_rule = ""
            if viral_t is not None:
                y_rule = 1 if lk >= viral_t else 0
            w.writerow(
                {
                    "row_index": i,
                    "title_len": len(title),
                    "body_len": len(body),
                    "like_proxy": lk,
                    "log1p_like": round(math.log1p(lk), 6),
                    "sop_tag": sop,
                    "emotion_tag": emo,
                    "y_rule": y_rule,
                }
            )

    print(f"Wrote {len(rows)} rows -> {outp}", flush=True)
    if viral_t is None:
        print("未设置阈值（无 --viral-threshold /有效 --labels-spec）：y_rule 列为空（见 schema_notes.md）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
