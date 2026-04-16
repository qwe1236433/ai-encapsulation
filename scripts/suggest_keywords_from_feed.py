"""
从归一 Feed（samples.json）里根据高互动笔记抽取候选搜索词，供 MediaCrawler 命令行 --keywords 使用。

无第三方依赖。策略：
  - 从 body_hint 提取小红书式话题：#xxx[话题]#
  - 从 title_hint 提取2～4 字中文片段（滑窗），按 log(1+like_proxy) 加权计分
  - 过滤极短词与少量停用字  - 可与 --seed-keywords 合并去重（种子词优先）

用法（仓库根）:

  python scripts/suggest_keywords_from_feed.py --samples openclaw/data/xhs-feed/samples.json

  MediaCrawler 示例:
  python main.py --platform xhs --lt qrcode --type search --keywords "$(Get-Content research/keyword_candidates_line.txt -Raw)"
  （Windows 下可将首行写入 keyword_candidates_line.txt 仅含逗号分隔词串）

  主题轮换（减轻单一搜索词同质化）:
  --rotation-pool research/keyword_rotation_pool.txt --rotation-state research/runtime/keyword_rotation_state.json
 每行一个主题，每次调用顺序取下一行置于种子词最前（# 行为注释）。

输出:
  - --out-txt  默认 research/keyword_candidates.txt（多行：说明 + 空行 + 一行逗号分隔词）
  - --out-json 默认 research/keyword_candidates.json（审计：得分、来源条数）
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TOPIC_TAG_RE = re.compile(r"#([^#\n]{1,40}?)\[话题\]#")
# 连续 CJK 统一表意文字（含扩展 A 常见区），用于 title 滑窗
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

_STOP = frozenset(
    """
 的 了 和 是 在 也 有 与 或 就 都 很 还 吗 呢 吧 啊 呀 哦 嗯 又 能 会 要 可以 这个 这样 那样 什么 怎么 多少 哪个 如何 为什么 是不是 有没有    一个 一些 没有 不是 但是 因为 所以 如果 虽然 而且 还是 或者 以及 以及 我们 你们 他们 自己 大家 真的 非常 特别 比较 最 更 太 多 少 好 不 别    """.split()
)


def _load_samples(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _weight(like_proxy: int) -> float:
    return math.log1p(max(1, int(like_proxy)))


def _clean_title_for_ngrams(title: str) -> str:
    s = title.strip()
    # 去掉常见 emoji / 符号块（保守：仅剥非 CJK/Latin/digit）
    s = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9\s]", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def _ngrams_from_text(text: str, wmin: int, wmax: int) -> list[str]:
    out: list[str] = []
    for m in _CJK_RE.finditer(text):
        chunk = m.group(0)
        if len(chunk) < wmin:
            continue
        L = len(chunk)
        for n in range(wmin, min(wmax, L) + 1):
            for i in range(0, L - n + 1):
                out.append(chunk[i : i + n])
    return out


def _topics_from_body(body: str) -> list[str]:
    if not body:
        return []
    seen: list[str] = []
    for m in _TOPIC_TAG_RE.finditer(body):
        t = m.group(1).strip()
        if 1 < len(t) <= 20 and t not in seen:
            seen.append(t)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description="samples.json → keyword candidates for MediaCrawler")
    ap.add_argument("--samples", type=str, default="openclaw/data/xhs-feed/samples.json")
    ap.add_argument("--out-txt", type=str, default="research/keyword_candidates.txt")
    ap.add_argument("--out-json", type=str, default="research/keyword_candidates.json")
    ap.add_argument(
        "--top-keywords",
        type=int,
        default=18,
        help="输出词表上限（去重后）；0 表示不截断，输出所有得分词（仅 key_ok 且来自当前 scored集合）",
    )
    ap.add_argument("--top-notes", type=int, default=100, help="参与统计的最高赞条数（按 like_proxy 排序）")
    ap.add_argument("--ngram-min", type=int, default=2)
    ap.add_argument("--ngram-max", type=int, default=4)
    ap.add_argument("--min-like", type=int, default=1, help="仅 like_proxy >= 此值的笔记")
    ap.add_argument(
        "--seed-keywords",
        type=str,
        default="",
        help="英文逗号分隔；总是放在输出词表最前（去重）",
    )
    ap.add_argument(
        "--cli-line-out",
        type=str,
        default="research/keyword_candidates_for_cli.txt",
        help="仅写入一行英文逗号分隔词，便于拼 MediaCrawler --keywords",
    )
    ap.add_argument(
        "--rotation-pool",
        type=str,
        default="",
        help="可选；文本文件每行一个主题/种子词（# 开头为注释）；每次调用取下一行轮询，置于 seed 最前，减轻单一搜索词同质化",
    )
    ap.add_argument(
        "--rotation-state",
        type=str,
        default="research/runtime/keyword_rotation_state.json",
        help="与 --rotation-pool 配合；记录下一轮索引",
    )
    args = ap.parse_args()

    inp = Path(args.samples).expanduser().resolve()
    if not inp.is_file():
        print(f"找不到 samples: {inp}", flush=True)
        return 2

    rows = _load_samples(inp)
    if not rows:
        print("samples 为空", flush=True)
        return 2

    scored: defaultdict[str, float] = defaultdict(float)
    topic_hits: dict[str, int] = Counter()
    ngram_hits: dict[str, int] = Counter()

    def key_ok(s: str) -> bool:
        s = s.strip()
        if len(s) < 2 or len(s) > 16:
            return False
        if s in _STOP:
            return False
        if all(c in _STOP for c in s.split()):
            return False
        return True

    ranked = sorted(
        rows,
        key=lambda r: int(r.get("like_proxy") or 1),
        reverse=True,
    )[: max(1, int(args.top_notes))]

    used = 0
    for r in ranked:
        like = int(r.get("like_proxy") or 1)
        if like < int(args.min_like):
            continue
        w = _weight(like)
        title = str(r.get("title_hint") or "")
        body = str(r.get("body_hint") or "")
        used += 1

        for t in _topics_from_body(body):
            if not key_ok(t):
                continue
            scored[t] += w * 1.5
            topic_hits[t] += 1

        clean = _clean_title_for_ngrams(title)
        for g in _ngrams_from_text(clean, int(args.ngram_min), int(args.ngram_max)):
            if not key_ok(g):
                continue
            scored[g] += w * 0.35
            ngram_hits[g] += 1

    ordered = sorted(scored.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))

    rotation_first: list[str] = []
    pool_path = (args.rotation_pool or "").strip()
    if pool_path:
        pp = Path(pool_path).expanduser().resolve()
        st_path = Path(args.rotation_state or "research/runtime/keyword_rotation_state.json").expanduser().resolve()
        if pp.is_file():
            lines = []
            for line in pp.read_text(encoding="utf-8").splitlines():
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                lines.append(t)
            if lines:
                st_path.parent.mkdir(parents=True, exist_ok=True)
                idx = 0
                if st_path.is_file():
                    try:
                        st = json.loads(st_path.read_text(encoding="utf-8"))
                        if isinstance(st, dict) and isinstance(st.get("index"), int):
                            idx = int(st["index"])
                    except (OSError, json.JSONDecodeError):
                        idx = 0
                pick = lines[idx % len(lines)]
                rotation_first.append(pick)
                st_path.write_text(
                    json.dumps({"index": idx + 1, "pool_path": str(pp)}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"rotation: pool line [{idx % len(lines)}] -> seed prefix {pick!r}", flush=True)

    seeds = rotation_first + [x.strip() for x in (args.seed_keywords or "").split(",") if x.strip()]
    out_terms: list[str] = []
    for s in seeds:
        if s not in out_terms:
            out_terms.append(s)
    top_kw = int(args.top_keywords)
    for term, _ in ordered:
        if term not in out_terms:
            out_terms.append(term)
        if top_kw > 0 and len(out_terms) >= top_kw:
            break

    outp_txt = Path(args.out_txt).expanduser().resolve()
    outp_json = Path(args.out_json).expanduser().resolve()
    outp_txt.parent.mkdir(parents=True, exist_ok=True)
    outp_json.parent.mkdir(parents=True, exist_ok=True)

    line_csv = ",".join(out_terms)
    header = (
        f"# MediaCrawler --keywords （英文逗号分隔，整行复制）\n"
        f"# 生成 UTC: {datetime.now(timezone.utc).isoformat()}\n"
        f"# 来源: {inp}\n"
        f"# 参与笔记数(截断后): {used}\n"
    )
    outp_txt.write_text(header + "\n" + line_csv + "\n", encoding="utf-8")

    cli_path = Path(args.cli_line_out).expanduser().resolve()
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text(line_csv + "\n", encoding="utf-8")

    payload = {
        "schema": "keyword_candidates_v1",
        "top_keywords_limit": top_kw if top_kw > 0 else None,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_samples": str(inp),
        "notes_used": used,
        "keywords": out_terms,
        "keywords_csv": line_csv,
        "scores_top": [
            {"term": t, "score": round(float(scored[t]), 4)} for t in out_terms if float(scored[t]) > 0
        ],
        "topic_hits": dict(topic_hits.most_common(50)),
        "ngram_hits": dict(ngram_hits.most_common(50)),
    }
    outp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote {len(out_terms)} keywords -> {outp_txt}", flush=True)
    print(f"CLI one-line -> {cli_path}", flush=True)
    print(f"Audit JSON -> {outp_json}", flush=True)
    print(line_csv[:200] + ("..." if len(line_csv) > 200 else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
