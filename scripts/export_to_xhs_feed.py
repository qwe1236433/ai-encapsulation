"""
把 MediaCrawler / 自建导出的 JSON 合并并写成 OpenClaw 小红书工厂可读 feed。

用法（在仓库根目录，例如 d:\\ai封装）:

  python scripts/export_to_xhs_feed.py --in D:\\path\\to\\raw.json --out openclaw/data/xhs-feed/samples.json

按话题写分文件（文件名与 xhs_factory 内话题 slug 一致）:

  python scripts/export_to_xhs_feed.py --topic "减脂餐" --in D:\\a.json D:\\b.json --out-dir openclaw/data/xhs-feed

去重（最小实现；默认 none，与旧行为一致）:

  --dedupe none 不去重（默认）
  --dedupe key      优先用原始行里的稳定 id；若无则退回正文指纹
  --dedupe content  仅用归一化 title_hint + body_hint 指纹

审计侧车（可选）:

  --digest-out path/to/samples.digest.json
  --batch-id 20260415-run1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 爬虫/导出里常见 id 字段（按顺序取第一个非空）；无则 key 模式会退回 content 指纹。
_ID_KEYS = (
    "note_id",
    "noteId",
    "noteid",
    "id",
    "item_id",
    "itemId",
    "aweme_id",
    "object_id",
    "feed_id",
)



def _topic_file_slug(topic: str) -> str:
    """与 openclaw/xhs_factory._topic_file_slug 保持一致。"""
    return hashlib.sha256((topic or "").encode("utf-8")).hexdigest()[:16]


def _normalize_external_sample(raw: dict[str, Any]) -> dict[str, Any] | None:
    """与 openclaw/xhs_factory._normalize_external_sample 保持一致（无第三方依赖）。"""
    if not isinstance(raw, dict):
        return None
    title = (
        raw.get("title_hint")
        or raw.get("title")
        or raw.get("note_title")
        or raw.get("desc")
        or raw.get("description")
    )
    body = (
        raw.get("body_hint")
        or raw.get("content")
        or raw.get("note_text")
        or raw.get("desc")
        or raw.get("description")
    )
    title_s = str(title or "").strip()[:500]
    body_s = str(body or "").strip()[:2000]
    if not title_s and not body_s:
        return None
    if not title_s:
        title_s = body_s[:120]
    if not body_s:
        body_s = title_s
    likes = raw.get("like_proxy") or raw.get("liked_count") or raw.get("likes") or raw.get("like_count")
    try:
        like_proxy = int(likes) if likes is not None else 100
    except (TypeError, ValueError):
        like_proxy = 100
    sop = str(raw.get("sop_tag") or raw.get("viral_sop") or "对照式").strip()[:32] or "对照式"
    emo = str(raw.get("emotion_tag") or raw.get("target_emotion") or "共鸣").strip()[:32] or "共鸣"
    return {
        "title_hint": title_s,
        "body_hint": body_s,
        "like_proxy": max(1, like_proxy),
        "sop_tag": sop,
        "emotion_tag": emo,
    }


def _unwrap_records(obj: object) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("data", "items", "notes", "list", "records", "result"):
            v = obj.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [obj]
    return []


def _load_path(p: Path) -> list[dict]:
    if p.is_dir():
        rows: list[dict] = []
        files = sorted({*p.glob("*.json"), *p.glob("*.jsonl")})
        for f in files:
            rows.extend(_load_path(f))
        return rows
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return _unwrap_records(json.loads(text))
        except json.JSONDecodeError:
            return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            out.extend(_unwrap_records(row))
        except json.JSONDecodeError:
            continue
    return out


def _stable_id_from_raw(raw: dict[str, Any]) -> str | None:
    for k in _ID_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _content_fingerprint(norm: dict[str, Any]) -> str:
    t = str(norm.get("title_hint") or "")
    b = str(norm.get("body_hint") or "")
    h = hashlib.sha256(f"{t}\n{b}".encode("utf-8")).hexdigest()
    return h


def _merge_key(dedupe: str, raw: dict[str, Any], norm: dict[str, Any]) -> str:
    if dedupe == "content":
        return "c:" + _content_fingerprint(norm)
    # key
    sid = _stable_id_from_raw(raw)
    if sid:
        return "i:" + sid
    return "c:" + _content_fingerprint(norm)


def _build_feed(
    raw_rows: list[dict],
    dedupe: str,
) -> tuple[list[dict], dict[str, int]]:
    stats = {
        "raw_rows": len(raw_rows),
        "empty_drop": 0,
        "dedup_drop": 0,
        "out": 0,
    }
    out: list[dict] = []
    if dedupe == "none":
        for row in raw_rows:
            n = _normalize_external_sample(row)
            if n:
                out.append(n)
            else:
                stats["empty_drop"] += 1
        stats["out"] = len(out)
        return out, stats

    seen: set[str] = set()
    for row in raw_rows:
        n = _normalize_external_sample(row)
        if not n:
            stats["empty_drop"] += 1
            continue
        k = _merge_key(dedupe, row, n)
        if k in seen:
            stats["dedup_drop"] += 1
            continue
        seen.add(k)
        out.append(n)
    stats["out"] = len(out)
    return out, stats


def _emit_digest(
    digest_path: Path,
    output_path: Path,
    st: dict[str, int],
    dedupe: str,
    batch_id: str | None = None,
) -> None:
    """审计用侧车文件：输出文件 sha256 + merge_stats（不替代 Git LFS/对象存储）。"""
    raw = output_path.read_bytes()
    payload: dict[str, Any] = {
        "schema": "xhs_feed_digest_v1",
        "output_path": str(output_path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "merge_stats": {
            "raw_rows": st["raw_rows"],
            "empty_drop": st["empty_drop"],
            "dedup_drop": st["dedup_drop"],
            "out": st["out"],
        },
        "dedupe": dedupe,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if batch_id:
        payload["batch_id"] = batch_id
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"digest: wrote {digest_path}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="合并导出 JSON → xhs_factory feed")
    ap.add_argument(
        "--in",
        dest="inputs",
        nargs="+",
        required=True,
        help="文件或目录（目录内 *.json / *.jsonl）",
    )
    ap.add_argument("--out", type=str, default="", help="输出单个 JSON 数组文件")
    ap.add_argument("--out-dir", type=str, default="", help="输出目录（与 --topic 合用）")
    ap.add_argument("--topic", type=str, default="", help="话题字符串，用于生成 {slug}.json")
    ap.add_argument(
        "--dedupe",
        choices=("none", "key", "content"),
        default="none",
        help="合并时去重策略（默认 none，不改变历史行为）",
    )
    ap.add_argument(
        "--digest-out",
        type=str,
        default="",
        help="可选：写入审计 JSON（sha256、merge_stats、dedupe）；与本次写出的 feed 文件对应",
    )
    ap.add_argument(
        "--batch-id",
        type=str,
        default="",
        help="可选：写入 digest 的 batch_id（便于与爬虫批次/计划任务对齐；不设则 digest 不含该字段）",
    )
    args = ap.parse_args()
    batch_id_s = (args.batch_id or "").strip() or None

    raw: list[dict] = []
    for s in args.inputs:
        raw.extend(_load_path(Path(s).expanduser().resolve()))

    normed, st = _build_feed(raw, args.dedupe)
    print(
        "merge_stats: "
        f"raw_rows={st['raw_rows']} empty_drop={st['empty_drop']} "
        f"dedup_drop={st['dedup_drop']} out={st['out']} dedupe={args.dedupe}",
        file=sys.stderr,
    )

    if args.out_dir and args.topic:
        out_dir = Path(args.out_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = _topic_file_slug(args.topic.strip())
        out_file = out_dir / f"{slug}.json"
        out_file.write_text(json.dumps(normed, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(normed)} samples -> {out_file}")
        print(f"话题 slug（与容器内 xhs_factory 一致）: {slug}")
        if (args.digest_out or "").strip():
            _emit_digest(
                Path(args.digest_out).expanduser().resolve(),
                out_file,
                st,
                args.dedupe,
                batch_id_s,
            )
        return 0

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(normed, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(normed)} samples -> {out_path}")
        if (args.digest_out or "").strip():
            _emit_digest(
                Path(args.digest_out).expanduser().resolve(),
                out_path,
                st,
                args.dedupe,
                batch_id_s,
            )
        return 0

    print("请指定 --out 或同时指定 --out-dir 与 --topic", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
