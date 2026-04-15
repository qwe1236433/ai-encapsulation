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

数据质量（可选；默认 none，不改变历史行为）:

  --validate-mode none不校验（默认）
  --validate-mode report 校验后仍写出文件；stderr 输出 validate_stats（exit 0）
  --validate-mode warn   同 report，前缀 WARNING，略多明细
  --validate-mode fail   若有违规则不写 --out / digest，exit 2

  --validate-schema PATH  JSON Schema（默认使用 scripts/schemas/xhs_feed_item_v1.schema.json，若存在且已 pip install jsonschema 则优先用其校验；否则使用内置等价规则）。
可选依赖: pip install -r scripts/requirements-feed-tools.txt
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

_VALIDATE_MODES = frozenset({"none", "report", "warn", "fail"})


def _default_validate_schema_path() -> Path:
    return Path(__file__).resolve().parent / "schemas" / "xhs_feed_item_v1.schema.json"


def _resolve_validate_schema_path(cli: str) -> Path | None:
    s = (cli or "").strip()
    if s:
        p = Path(s).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"--validate-schema 文件不存在: {p}")
        return p
    p = _default_validate_schema_path()
    return p if p.is_file() else None


def _builtin_item_errors(item: Any) -> list[str]:
    """与 scripts/schemas/xhs_feed_item_v1.schema.json 语义对齐（无 jsonschema 时）。"""
    errs: list[str] = []
    if not isinstance(item, dict):
        return ["条目须为 JSON 对象"]
    req = ("title_hint", "body_hint", "like_proxy", "sop_tag", "emotion_tag")
    for k in req:
        if k not in item:
            errs.append(f"缺少必填键 {k!r}")
    if errs:
        return errs
    th, bh = item["title_hint"], item["body_hint"]
    if not isinstance(th, str):
        errs.append("title_hint 须为字符串")
    elif len(th) > 500:
        errs.append(f"title_hint 长度 {len(th)} 超过 500")
    if not isinstance(bh, str):
        errs.append("body_hint 须为字符串")
    elif len(bh) > 2000:
        errs.append(f"body_hint 长度 {len(bh)} 超过 2000")
    if isinstance(th, str) and isinstance(bh, str) and not th.strip() and not bh.strip():
        errs.append("title_hint 与 body_hint 不能均为空")
    lp = item["like_proxy"]
    if isinstance(lp, bool):
        errs.append("like_proxy 不能为布尔值")
    elif not isinstance(lp, int):
        errs.append("like_proxy 须为整数")
    elif lp < 1:
        errs.append("like_proxy 须 >= 1")
    st, et = item["sop_tag"], item["emotion_tag"]
    if not isinstance(st, str):
        errs.append("sop_tag 须为字符串")
    elif len(st) > 32:
        errs.append(f"sop_tag 长度 {len(st)} 超过 32")
    if not isinstance(et, str):
        errs.append("emotion_tag 须为字符串")
    elif len(et) > 32:
        errs.append(f"emotion_tag 长度 {len(et)} 超过 32")
    return errs


def _validate_feed_items(items: list[Any], schema_path: Path | None) -> tuple[list[tuple[int, str]], str]:
    """返回 (violations, engine)；violations 为 (index, message)，可多消息同 index。"""
    schema_obj: dict[str, Any] | None = None
    if schema_path is not None:
        try:
            raw = json.loads(schema_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                schema_obj = raw
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"无法读取 JSON Schema: {schema_path} ({e})") from e

    if schema_obj is not None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            print(
                "validate: 未安装 jsonschema，使用内置规则（与 xhs_feed_item_v1 等价）。"
                " 安装: pip install -r scripts/requirements-feed-tools.txt",
                file=sys.stderr,
            )
            schema_obj = None
    violations: list[tuple[int, str]] = []
    if schema_obj is not None:
        from jsonschema import Draft202012Validator

        v = Draft202012Validator(schema_obj)
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                violations.append((i, "条目须为 JSON 对象"))
                continue
            for e in sorted(v.iter_errors(item), key=lambda x: list(x.path)):
                violations.append((i, e.message))
        return violations, "jsonschema"
    for i, item in enumerate(items):
        for m in _builtin_item_errors(item):
            violations.append((i, m))
    return violations, "builtin"


def _print_validate_report(mode: str, violations: list[tuple[int, str]], n_items: int, engine: str) -> None:
    affected = len({i for i, _ in violations})
    print(
        f"validate_stats: engine={engine} items={n_items} violation_messages={len(violations)} "
        f"affected_items={affected} ok_items={n_items - affected}",
        file=sys.stderr,
    )
    if not violations:
        return
    cap = 15 if mode == "report" else 20
    prefix = "WARNING validate: " if mode == "warn" else "validate: "
    for i, msg in violations[:cap]:
        print(f"{prefix}item[{i}] {msg}", file=sys.stderr)
    if len(violations) > cap:
        print(f"{prefix}... 另有 {len(violations) - cap} 条消息省略", file=sys.stderr)


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
    ap.add_argument(
        "--validate-mode",
        choices=sorted(_VALIDATE_MODES),
        default="none",
        help="数据质量：none 不校验；report/warn 校验后仍写出；fail 有违规则不写 out/digest 并 exit 2",
    )
    ap.add_argument(
        "--validate-schema",
        type=str,
        default="",
        help="JSON Schema 路径；省略则使用 scripts/schemas/xhs_feed_item_v1.schema.json（若存在且已安装 jsonschema）",
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

    v_mode = str(args.validate_mode or "none").strip().lower()
    if v_mode != "none":
        try:
            schema_p = _resolve_validate_schema_path(str(args.validate_schema or ""))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        try:
            violations, engine = _validate_feed_items(normed, schema_p)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if v_mode in ("report", "warn"):
            _print_validate_report(v_mode, violations, len(normed), engine)
        elif v_mode == "fail":
            if violations:
                _print_validate_report("warn", violations, len(normed), engine)
                print("validate: fail 模式存在违规，已中止写出（未写入 --out / --digest-out）", file=sys.stderr)
                return 2
            _print_validate_report("report", [], len(normed), engine)

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
