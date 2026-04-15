"""
从工厂用 samples.json（JSON 数组）导出研究用特征表 CSV（feature_schema v0）。

用法（仓库根目录）:

  python scripts/export_features_v0.py --samples openclaw/data/xhs-feed/samples.json --out research/features_v0.csv

可选：按点赞阈值生成操作化二分类标签 y_rule（非平台「爆文」真值）:

  python scripts/export_features_v0.py --samples ... --out research/features_v0.csv --viral-threshold 1000

或从数据契约 JSON 读取阈值（推荐与实验报告一致；示例见 research/labels_spec.example.json）:

  python scripts/export_features_v0.py --samples ... --out research/features_v0.csv --labels-spec research/labels_spec.json

若同时传入 --viral-threshold 与 --labels-spec，以命令行 --viral-threshold 为准。

批次元数据（可选，写入 CSV 列，train_baseline 仍只读数值特征列）:

  --batch-id 显式批次号；否则读环境变量 EXPORT_FEATURES_BATCH_ID；再否则读 --feed-digest JSON 内的 batch_id（若有）。
  --feed-digest 指向 export_to_xhs_feed 产出的 xhs_feed_digest_v1；列 feed_digest_sha256 取自文件内 sha256（不计算、不猜测）。
  --verify-samples-digest 与 --feed-digest 联用：对 --samples 文件计算 sha256，必须与 digest 内一致，否则退出（防错配）。

定义见 research/schema_notes.md 与 research/EXPERIMENT_REPORT.md。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_viral_threshold_from_spec(path: Path) -> int | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
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


def _load_feed_digest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取 digest: {path} ({e})") from e
    if not isinstance(raw, dict):
        raise ValueError(f"digest 须为 JSON 对象: {path}")
    if raw.get("schema") != "xhs_feed_digest_v1":
        raise ValueError(f"digest.schema 须为 xhs_feed_digest_v1: {path}")
    if "sha256" not in raw or not isinstance(raw.get("sha256"), str):
        raise ValueError(f"digest 缺少字符串字段 sha256: {path}")
    return raw


def _resolve_batch_id(cli: str, digest: dict[str, Any] | None) -> str:
    s = (cli or "").strip()
    if s:
        return s
    s = (os.environ.get("EXPORT_FEATURES_BATCH_ID") or "").strip()
    if s:
        return s
    if digest:
        b = digest.get("batch_id")
        if b is not None and str(b).strip():
            return str(b).strip()
    return ""


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
    ap.add_argument(
        "--batch-id",
        type=str,
        default="",
        help="可选；写入 CSV batch_id 列。优先于环境变量 EXPORT_FEATURES_BATCH_ID 与 digest 内 batch_id",
    )
    ap.add_argument(
        "--feed-digest",
        type=str,
        default="",
        help="可选；xhs_feed_digest_v1 JSON；用于 feed_digest_sha256 列，并可在未传 batch-id 时提供 batch_id",
    )
    ap.add_argument(
        "--verify-samples-digest",
        action="store_true",
        help="若已设 --feed-digest：校验 samples 文件 sha256 与 digest 一致（推荐正式实验开启）",
    )
    args = ap.parse_args()
    if args.verify_samples_digest and not (args.feed_digest or "").strip():
        print("错误：--verify-samples-digest 必须同时提供 --feed-digest", flush=True)
        return 2

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

    digest_obj: dict[str, Any] | None = None
    if (args.feed_digest or "").strip():
        dig_path = Path(args.feed_digest).expanduser().resolve()
        try:
            digest_obj = _load_feed_digest(dig_path)
        except ValueError as e:
            print(str(e), flush=True)
            return 2
        print(f"使用 feed digest: {dig_path}", flush=True)
        op = digest_obj.get("output_path")
        if isinstance(op, str) and op.strip():
            try:
                dig_out = Path(op).expanduser().resolve()
                if dig_out != inp:
                    print(
                        f"警告：--samples 与 digest.output_path 不一致\n samples={inp}\n  digest={dig_out}",
                        flush=True,
                    )
            except OSError:
                pass
        if args.verify_samples_digest:
            actual = _sha256_file(inp)
            expected = str(digest_obj.get("sha256") or "")
            if actual != expected:
                print(
                    f"校验失败：samples sha256 与 digest 不一致（请确认未换错文件或 digest 未过期）\n"
                    f"  actual={actual}\n  expected={expected}",
                    flush=True,
                )
                return 2
            print("verify-samples-digest: sha256 OK", flush=True)

    batch_id_val = _resolve_batch_id(args.batch_id, digest_obj)
    feed_sha = (digest_obj.get("sha256") if digest_obj else "") or ""

    fieldnames = [
        "row_index",
        "title_len",
        "body_len",
        "like_proxy",
        "log1p_like",
        "sop_tag",
        "emotion_tag",
        "y_rule",
        "batch_id",
        "feed_digest_sha256",
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
                    "batch_id": batch_id_val,
                    "feed_digest_sha256": feed_sha,
                }
            )

    print(f"Wrote {len(rows)} rows -> {outp}", flush=True)
    if viral_t is None:
        print("未设置阈值（无 --viral-threshold /有效 --labels-spec）：y_rule 列为空（见 schema_notes.md）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
