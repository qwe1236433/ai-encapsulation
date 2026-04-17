"""
scripts/diagnose_note.py
========================
CLI：对一篇笔记（用户粘贴的 title+body）产出诊断报告。

用法：
  python scripts/diagnose_note.py --title "..." --body "..."
  python scripts/diagnose_note.py --title-file t.txt --body-file b.txt
  python scripts/diagnose_note.py --json-in case.json  # {title, body, [sop_tag], [emotion_tag]}
  python scripts/diagnose_note.py ... --format markdown|json  (默认 markdown)
  python scripts/diagnose_note.py ... --out report.md

特性：
  - 直接调 openclaw.xhs_diagnose.diagnose（单一事实源）
  - 默认输出 Markdown；--format json 输出原始结构化结果
  - 不做任何链接解析；标题/正文由用户手动提供
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "openclaw"))

# Windows 控制台强制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from openclaw.xhs_diagnose import diagnose  # noqa: E402
from openclaw.xhs_diagnose_renderer import render_markdown  # noqa: E402


def _read_text_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="小红书笔记诊断 CLI（L3 写作级提示）")
    g_in = ap.add_mutually_exclusive_group()
    g_in.add_argument("--json-in", help="从 JSON 文件读取 {title, body, sop_tag?, emotion_tag?}")
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--title-file")
    ap.add_argument("--body-file")
    ap.add_argument("--sop-tag", default="", help="可选：tutorial | review | story | list")
    ap.add_argument("--emotion-tag", default="", help="可选：positive | negative | mixed")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument(
        "--view",
        choices=["blogger", "dev"],
        default="blogger",
        help="blogger=博主友好人话版（默认）；dev=开发者视图，保留 CI/系数/AUC 等细节",
    )
    ap.add_argument("--out", help="输出文件路径；缺省则打印到 stdout")
    args = ap.parse_args()

    if args.json_in:
        data = json.loads(Path(args.json_in).read_text(encoding="utf-8-sig"))
        title = str(data.get("title") or "")
        body = str(data.get("body") or "")
        sop_tag = str(data.get("sop_tag") or "")
        emotion_tag = str(data.get("emotion_tag") or "")
    else:
        title = args.title or (_read_text_file(args.title_file) if args.title_file else "")
        body = args.body or (_read_text_file(args.body_file) if args.body_file else "")
        sop_tag = args.sop_tag
        emotion_tag = args.emotion_tag

    if not title and not body:
        print("错误：必须通过 --title/--body 或 --title-file/--body-file 或 --json-in 提供内容", file=sys.stderr)
        return 2

    result = diagnose(title, body, sop_tag=sop_tag, emotion_tag=emotion_tag)

    if args.format == "json":
        payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    else:
        payload = render_markdown(result, audience=args.view)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        print(f"[完成] 诊断报告 → {out_path}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
