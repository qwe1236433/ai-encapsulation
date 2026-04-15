"""
校验 features_v0.csv 中的 y_rule 是否与 labels_spec 的点赞阈值定义一致。

定义（与 export_features_v0.py 一致）：y_rule == 1 当且仅当 like_proxy >= viral_like_threshold。

用法（仓库根目录）:

  python scripts/verify_features_labels_spec.py --features research/features_v0.csv --labels-spec research/labels_spec.json

退出码：0 全部一致；1 存在不一致或无法校验；2 参数/文件错误。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def _threshold_from_spec(path: Path) -> int:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("labels_spec 根节点须为 JSON 对象")
    for key in ("viral_like_threshold", "viral_threshold"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key} 须为整数") from e
    raise ValueError("未找到 viral_like_threshold 或 viral_threshold")


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 CSV 的 y_rule 与 labels_spec 阈值")
    ap.add_argument("--features", type=str, required=True, help="features_v0.csv 路径")
    ap.add_argument("--labels-spec", type=str, required=True, help="labels_spec.json 路径")
    args = ap.parse_args()

    feat_path = Path(args.features).expanduser().resolve()
    spec_path = Path(args.labels_spec).expanduser().resolve()
    if not feat_path.is_file():
        print(f"找不到 CSV: {feat_path}", flush=True)
        return 2
    if not spec_path.is_file():
        print(f"找不到 labels_spec: {spec_path}", flush=True)
        return 2
    try:
        t = _threshold_from_spec(spec_path)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"读取 labels_spec 失败: {e}", flush=True)
        return 2

    mismatches: list[tuple[int, int, int, int]] = []
    skipped_empty = 0
    bad_y = 0
    checked = 0

    with feat_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "y_rule" not in reader.fieldnames or "like_proxy" not in reader.fieldnames:
            print("CSV 须含列 y_rule, like_proxy", flush=True)
            return 2
        for row_num, row in enumerate(reader, start=2):
            idx = row_num
            try:
                ri = row.get("row_index", "")
                if str(ri).strip() != "":
                    idx = int(ri)
            except ValueError:
                idx = row_num
            y_raw = str(row.get("y_rule", "")).strip()
            if y_raw == "":
                skipped_empty += 1
                continue
            try:
                y = int(float(y_raw))
            except ValueError:
                bad_y += 1
                continue
            if y not in (0, 1):
                bad_y += 1
                continue
            try:
                lk = int(float(row.get("like_proxy", 0)))
            except ValueError:
                bad_y += 1
                continue
            lk = max(0, lk)
            expected = 1 if lk >= t else 0
            checked += 1
            if y != expected:
                mismatches.append((idx, lk, y, expected))

    print(
        f"labels_spec: {spec_path} viral_like_threshold={t} | "
        f"checked_rows={checked} skipped_empty_y_rule={skipped_empty} bad_cells={bad_y}",
        flush=True,
    )
    if mismatches:
        print(f"MISMATCH count={len(mismatches)} (row_index, like_proxy, y_rule, expected_y_rule):", flush=True)
        for item in mismatches[:50]:
            print(f"  {item}", flush=True)
        if len(mismatches) > 50:
            print(f"  ... and {len(mismatches) - 50} more", flush=True)
        return 1
    if checked == 0:
        print("无可用行：请确认 y_rule 已用 export_features_v0 与同一 labels_spec 导出。", flush=True)
        return 1
    print("OK: all y_rule values match labels_spec threshold rule.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
