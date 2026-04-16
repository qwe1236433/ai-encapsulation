"""
将本仓库 research/runtime/mediacrawler_base_config.json 中的白名单项写入 MediaCrawler config/*.py。
仅替换已存在的赋值行（及 CRAWLER_TYPE 多行块）；不认识的键跳过并警告；写前备份 base_config / xhs_config。

用法（仓库根）:
  python scripts/apply_mediacrawler_base_config.py
  python scripts/apply_mediacrawler_base_config.py --mc-root D:\\MediaCrawler --patch research/runtime/mediacrawler_base_config.json --dry-run

环境: MEDIACRAWLER_ROOT 未传 --mc-root 时使用；无 patch 文件则退出 0。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 与上游 NanmiCoder/MediaCrawler base_config.py 命名一致（含拼写 ENABLE_GET_MEIDAS）
ALLOWED_BASE: frozenset[str] = frozenset(
    {
        "PLATFORM",
        "XHS_INTERNATIONAL",
        "KEYWORDS",
        "LOGIN_TYPE",
        "COOKIES",
        "CRAWLER_TYPE",
        "ENABLE_IP_PROXY",
        "IP_PROXY_POOL_COUNT",
        "IP_PROXY_PROVIDER_NAME",
        "HEADLESS",
        "SAVE_LOGIN_STATE",
        "ENABLE_CDP_MODE",
        "CDP_DEBUG_PORT",
        "CUSTOM_BROWSER_PATH",
        "CDP_HEADLESS",
        "BROWSER_LAUNCH_TIMEOUT",
        "CDP_CONNECT_EXISTING",
        "AUTO_CLOSE_BROWSER",
        "SAVE_DATA_OPTION",
        "SAVE_DATA_PATH",
        "USER_DATA_DIR",
        "START_PAGE",
        "CRAWLER_MAX_NOTES_COUNT",
        "MAX_CONCURRENCY_NUM",
        "ENABLE_GET_MEIDAS",
        "ENABLE_GET_COMMENTS",
        "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES",
        "ENABLE_GET_SUB_COMMENTS",
        "ENABLE_GET_WORDCLOUD",
        "CRAWLER_MAX_SLEEP_SEC",
        "DISABLE_SSL_VERIFY",
    }
)

ALLOWED_XHS: frozenset[str] = frozenset(
    {
        "SORT_TYPE",
    }
)


def _py_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    raise ValueError(f"unsupported JSON type for MC config: {type(v).__name__}")


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, bak)
    return bak


def _replace_crawler_type_block(content: str, mode: str) -> tuple[str, bool]:
    """CRAWLER_TYPE = ( ... ) 多行块替换为与上游风格接近的单模式元组。"""
    pat = re.compile(r"CRAWLER_TYPE\s*=\s*\([\s\S]*?\)\s*\n", re.MULTILINE)
    m = pat.search(content)
    if not m:
        return content, False
    inner = json.dumps(mode, ensure_ascii=False)
    block = f'CRAWLER_TYPE = (\n {inner} # search | detail | creator\n)\n'
    return content[: m.start()] + block + content[m.end() :], True


def _replace_single_line_key(content: str, key: str, rhs_py: str) -> tuple[str, bool]:
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)([^\n]*)$")
    done = False
    for line in lines:
        if done:
            out.append(line)
            continue
        m = key_re.match(line)
        if not m:
            out.append(line)
            continue
        indent, eq, _rest = m.group(1), m.group(2), m.group(3)
        if "#" in _rest:
            code_tail, comment = _rest.split("#", 1)
            comment = "#" + comment
        else:
            code_tail, comment = _rest, ""
        code_tail = code_tail.rstrip()
        new_line = f"{indent}{key}{eq}{rhs_py}"
        if comment.strip():
            pad = "" if new_line.endswith((" ", "\t")) else "  "
            new_line = f"{new_line}{pad}{comment}"
        if not new_line.endswith("\n"):
            new_line += "\n"
        out.append(new_line)
        done = True
    return "".join(out), done


def _apply_file(
    path: Path,
    allowed: frozenset[str],
    updates: dict[str, Any],
    dry_run: bool,
) -> list[str]:
    msgs: list[str] = []
    if not updates:
        return msgs
    text = path.read_text(encoding="utf-8")
    original = text
    for k, v in updates.items():
        if k not in allowed:
            msgs.append(f"skip disallowed key: {k}")
            continue
        if k == "CRAWLER_TYPE" and path.name == "base_config.py":
            if not isinstance(v, str):
                msgs.append(f"CRAWLER_TYPE must be string, got {type(v).__name__}")
                continue
            text2, ok = _replace_crawler_type_block(text, v)
            if ok:
                text = text2
                msgs.append(f"set CRAWLER_TYPE -> {v!r}")
                continue
            text2, ok2 = _replace_single_line_key(text, k, _py_literal(v))
            if ok2:
                text = text2
                msgs.append(f"set CRAWLER_TYPE (single line) -> {_py_literal(v)!r}")
            else:
                msgs.append("CRAWLER_TYPE: no multiline block or single line found; skipped")
            continue
        try:
            rhs = _py_literal(v)
        except ValueError as e:
            msgs.append(f"{k}: {e}")
            continue
        text, ok = _replace_single_line_key(text, k, rhs)
        if ok:
            msgs.append(f"set {k} -> {rhs}")
        else:
            msgs.append(f"key {k} not found in {path.name}; skipped")
    if text != original and not dry_run:
        _backup(path)
        path.write_text(text, encoding="utf-8", newline="\n")
    elif text != original and dry_run:
        msgs.append("(dry-run: no write)")
    return msgs


def _normalize_patch(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    if "base_config" in raw or "xhs_config" in raw:
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw.get("base_config"), dict):
            out["base_config.py"] = dict(raw["base_config"])
        if isinstance(raw.get("xhs_config"), dict):
            out["xhs_config.py"] = dict(raw["xhs_config"])
        return out
    return {"base_config.py": dict(raw)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply whitelisted keys to MediaCrawler config")
    ap.add_argument("--mc-root", type=str, default="", help="MediaCrawler root (default MEDIACRAWLER_ROOT or D:\\MediaCrawler)")
    ap.add_argument("--patch", type=str, default="", help="JSON patch path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent.parent
    mc = (args.mc_root or os.environ.get("MEDIACRAWLER_ROOT") or r"D:\MediaCrawler").strip()
    mc_path = Path(mc).expanduser().resolve()
    patch_path = Path(
        args.patch or (here / "research" / "runtime" / "mediacrawler_base_config.json")
    ).expanduser().resolve()

    if not patch_path.is_file():
        print(f"no patch file: {patch_path} (skip)", flush=True)
        return 0

    cfg_dir = mc_path / "config"
    if not cfg_dir.is_dir():
        print(f"MediaCrawler config dir missing: {cfg_dir}", flush=True)
        return 2

    try:
        patch_obj = json.loads(patch_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"invalid patch JSON: {e}", flush=True)
        return 2
    if not isinstance(patch_obj, dict):
        print("patch root must be object", flush=True)
        return 2

    sections = _normalize_patch(patch_obj)
    any_change = False
    for fname, allowed in (
        ("base_config.py", ALLOWED_BASE),
        ("xhs_config.py", ALLOWED_XHS),
    ):
        rel_updates = sections.get(fname)
        if not rel_updates:
            continue
        path = cfg_dir / fname
        if not path.is_file():
            print(f"missing {path}; skip section {fname}", flush=True)
            continue
        msgs = _apply_file(path, allowed, rel_updates, args.dry_run)
        for m in msgs:
            print(f"  {fname}: {m}", flush=True)
        if any("set " in x for x in msgs) or any("CRAWLER_TYPE ->" in x for x in msgs):
            any_change = True

    if any_change:
        print(f"OK: applied patch from {patch_path}", flush=True)
    else:
        print("no changes applied", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
