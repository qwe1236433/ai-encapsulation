"""
根据 evaluate_baseline_weights 产出 + 你编写的规则表，把「要改的 MC 配置」合并进 mediacrawler_base_config.json。

- 规则文件不存在或 rules 为空：退出 0，不写文件。
- 会读取现有 research/runtime/mediacrawler_base_config.json（若有），再叠规则匹配产生的 set，写回同一路径；
  未在规则里出现的键保留为你手工配置的值。

用法:
  python scripts/merge_mediacrawler_patch_from_eval.py
  python scripts/merge_mediacrawler_patch_from_eval.py --eval research/artifacts/eval_auto_baseline_v1.json

数分里的「模型」默认是特征上的逻辑回归基线，不是 LLM；本脚本读的是该 eval JSON 的 warnings / n_samples / holdout AUC 等。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _rule_matches(rule: dict[str, Any], eval_payload: dict[str, Any]) -> bool:
    warns = set(eval_payload.get("warnings") or [])
    n = int(eval_payload.get("n_samples") or 0)
    auc = eval_payload.get("artifact_holdout_roc_auc")
    auc_f: float | None
    if isinstance(auc, (int, float)):
        auc_f = float(auc)
    else:
        auc_f = None

    any_w = rule.get("if_any_warning")
    if isinstance(any_w, list) and any_w:
        if not (set(any_w) & warns):
            return False
    all_w = rule.get("if_all_warnings")
    if isinstance(all_w, list) and all_w:
        if not set(all_w).issubset(warns):
            return False

    lt = rule.get("if_n_samples_lt")
    if lt is not None and not (n < int(lt)):
        return False
    gte = rule.get("if_n_samples_gte")
    if gte is not None and not (n >= int(gte)):
        return False

    if rule.get("if_holdout_auc_gte") is not None:
        if auc_f is None:
            return False
        if not (auc_f >= float(rule["if_holdout_auc_gte"])):
            return False
    if rule.get("if_holdout_auc_lt") is not None:
        if auc_f is None:
            return False
        if not (auc_f < float(rule["if_holdout_auc_lt"])):
            return False

    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge MC patch from eval + rules")
    ap.add_argument("--repo-root", type=str, default="")
    ap.add_argument(
        "--eval",
        type=str,
        default="",
        help="eval_auto_baseline_v1.json",
    )
    ap.add_argument(
        "--rules",
        type=str,
        default="",
        help="mediacrawler_eval_patch_rules.json",
    )
    ap.add_argument(
        "--patch",
        type=str,
        default="",
        help="mediacrawler_base_config.json in/out",
    )
    args = ap.parse_args()

    here = Path(__file__).resolve().parent.parent
    root = Path(args.repo_root).expanduser().resolve() if args.repo_root else here
    rules_path = Path(args.rules or (root / "research" / "runtime" / "mediacrawler_eval_patch_rules.json"))
    patch_path = Path(args.patch or (root / "research" / "runtime" / "mediacrawler_base_config.json"))
    eval_path = Path(
        args.eval or (root / "research" / "artifacts" / "eval_auto_baseline_v1.json")
    ).expanduser().resolve()

    if not rules_path.is_file():
        print(f"no rules file: {rules_path} (skip)", flush=True)
        return 0

    try:
        rules_obj = json.loads(rules_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"invalid rules JSON: {e}", flush=True)
        return 2

    raw_rules = rules_obj.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        print("rules.rules empty (skip)", flush=True)
        return 0

    if not eval_path.is_file():
        print(f"no eval json: {eval_path} (skip)", flush=True)
        return 0

    try:
        ev = json.loads(eval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"invalid eval JSON: {e}", flush=True)
        return 2

    base: dict[str, Any] = {}
    if patch_path.is_file():
        try:
            loaded = json.loads(patch_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                base = loaded
        except (json.JSONDecodeError, OSError):
            print(f"WARN: could not parse existing patch, starting empty: {patch_path}", flush=True)

    overlay: dict[str, Any] = {}
    matched = 0
    for i, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            continue
        if not _rule_matches(rule, ev):
            continue
        st = rule.get("set")
        if not isinstance(st, dict):
            continue
        overlay = _deep_merge(overlay, st)
        matched += 1
        name = rule.get("name", f"rule[{i}]")
        print(f"matched {name} -> merge set keys {list(st.keys())}", flush=True)

    if not overlay:
        print("no rule matched or empty set (patch file unchanged)", flush=True)
        return 0

    merged = _deep_merge(base, overlay)
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {patch_path} ({matched} rule(s) applied)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
