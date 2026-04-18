"""
Hermes Tuner —— 根据数分中心的打分/统计结果，调 MiniMax 提出"微调提案"。

流程：
  collect_analytics_snapshot()  从 features_v2.csv + baseline_v2.json + 当前规则 → 简洁快照
        ↓
  propose_tuning(snapshot)      把快照 + 严格约束 system prompt 丢给 MiniMax，拿回 TuningProposal
        ↓
  auditor.audit_proposal()      审核（另一个模块，本文件不实现）
        ↓
  若 REJECT → 带 reason 回 Hermes 重提，最多 max_rounds 轮

Hermes 的角色（与 auditor 严格区分）：
  - 只"看分"（读数分统计），不打分
  - 只提"微调"（相邻值、单参数、小幅度）
  - 被审核员驳回时看 reason 返工，不可以绕过审核员

本文件不做模型训练、不实现打分，仅做 LLM 调用与结构化 IO。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "hermes") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "hermes"))

from _minimax import call_minimax  # noqa: E402
from auditor import (  # noqa: E402
    DEFAULT_BASELINE_JSON,
    DEFAULT_FEATURES_CSV,
    TuningProposal,
    audit_proposal,
)

MINIMAX_MAX_TOKENS = 2400

CURRENT_RULES: list[dict[str, Any]] = [
    {
        "action_code": "REMOVE_TITLE_QUESTION_MARK",
        "feature": "title_has_question",
        "trigger_predicate": {"feature": "title_has_question", "op": "==", "value": 1},
        "severity": "high",
        "description": "标题带问号 → 建议删掉",
        "is_core": True,
    },
    {
        "action_code": "ADD_ONE_TITLE_EMOJI",
        "feature": "title_emoji_count",
        "trigger_predicate": {"feature": "title_emoji_count", "op": "==", "value": 0},
        "severity": "high",
        "description": "标题 0 个 emoji → 建议加 1 个",
        "is_core": True,
    },
    {
        "action_code": "REDUCE_TITLE_HASHTAG_TO_ONE",
        "feature": "title_hashtag_count",
        "trigger_predicate": {"feature": "title_hashtag_count", "op": ">=", "value": 2},
        "severity": "medium",
        "description": "标题 ≥2 个 hashtag → 建议减少到 1 个",
        "is_core": False,
    },
]

DEFAULT_KEYWORD_POOL: list[str] = [
    "减脂餐", "减肥餐", "健身餐", "高蛋白", "低卡",
    "HIIT", "腹肌训练", "瘦腿", "哑铃训练", "居家健身",
]


@dataclass
class AnalyticsSnapshot:
    """数分中心给 Hermes 的一页纸快照。"""

    sample_count: int
    baseline_auc: float
    baseline_coefficients: dict[str, float]
    feature_stats: dict[str, dict[str, Any]]
    active_rules: list[dict[str, Any]]
    keyword_pool: list[str]
    audit_history: list[dict[str, Any]] = field(default_factory=list)

    def to_llm_text(self) -> str:
        """LLM 友好的自然语言+数字摘要，避免传大段 JSON 让 LLM 迷失。"""
        lines: list[str] = []
        lines.append(f"# 数分快照（样本 n={self.sample_count}，基线 AUC={self.baseline_auc:.4f}）")
        lines.append("")
        lines.append("## 当前 3 条诊断规则")
        for r in self.active_rules:
            core = " 【核心，禁止删除】" if r.get("is_core") else ""
            pred = r["trigger_predicate"]
            lines.append(
                f"- `{r['action_code']}`{core}：当 `{pred['feature']} {pred['op']} {pred['value']}` 时触发 "
                f"| severity={r['severity']} | {r['description']}"
            )
        lines.append("")
        lines.append("## 核心特征的描述统计与基线系数")
        for feat, stat in self.feature_stats.items():
            coef = self.baseline_coefficients.get(feat, 0.0)
            lines.append(f"- `{feat}` (coef={coef:+.3f}):")
            for g_label, g in stat.get("groups", {}).items():
                lines.append(
                    f"    - {g_label}: n={g['n']}, 高赞率={g['rate']*100:.1f}%"
                )
            if "pp_diff" in stat:
                lines.append(f"    - 组间差异 Δpp = {stat['pp_diff']:+.1f}pp")
        lines.append("")
        lines.append("## 爬虫关键词池（当前）")
        lines.append("  " + "、".join(self.keyword_pool[:20]) + (f" …共 {len(self.keyword_pool)} 个" if len(self.keyword_pool) > 20 else ""))
        if self.audit_history:
            lines.append("")
            lines.append("## 上几轮审核历史（你已经被驳回过，这次别重复）")
            for i, h in enumerate(self.audit_history, 1):
                lines.append(
                    f"  {i}. 提案: {h.get('target')}: {h.get('before')} → {h.get('after')}  "
                    f"驳回原因: {h.get('reject_reason')}"
                )
        return "\n".join(lines)


def _emoji0_vs_ge1(df: pd.DataFrame, y_col: str) -> dict[str, Any]:
    mask = df[y_col].notna()
    s = df.loc[mask]
    a = s[s["title_emoji_count"] == 0]
    b = s[s["title_emoji_count"] >= 1]
    return {
        "groups": {
            "emoji==0": {"n": int(len(a)), "rate": float(a[y_col].mean()) if len(a) else 0.0},
            "emoji>=1": {"n": int(len(b)), "rate": float(b[y_col].mean()) if len(b) else 0.0},
        },
        "pp_diff": (float(b[y_col].mean()) - float(a[y_col].mean())) * 100 if len(a) and len(b) else 0.0,
    }


def _question_group(df: pd.DataFrame, y_col: str) -> dict[str, Any]:
    mask = df[y_col].notna()
    s = df.loc[mask]
    a = s[s["title_has_question"] == 1]
    b = s[s["title_has_question"] == 0]
    return {
        "groups": {
            "带问号": {"n": int(len(a)), "rate": float(a[y_col].mean()) if len(a) else 0.0},
            "不带问号": {"n": int(len(b)), "rate": float(b[y_col].mean()) if len(b) else 0.0},
        },
        "pp_diff": (float(a[y_col].mean()) - float(b[y_col].mean())) * 100 if len(a) and len(b) else 0.0,
    }


def _hashtag_group(df: pd.DataFrame, y_col: str) -> dict[str, Any]:
    mask = df[y_col].notna()
    s = df.loc[mask]
    a = s[s["title_hashtag_count"] >= 2]
    b = s[s["title_hashtag_count"] < 2]
    return {
        "groups": {
            "hashtag>=2": {"n": int(len(a)), "rate": float(a[y_col].mean()) if len(a) else 0.0},
            "hashtag<2": {"n": int(len(b)), "rate": float(b[y_col].mean()) if len(b) else 0.0},
        },
        "pp_diff": (float(a[y_col].mean()) - float(b[y_col].mean())) * 100 if len(a) and len(b) else 0.0,
    }


def collect_analytics_snapshot(
    features_csv: Path = DEFAULT_FEATURES_CSV,
    baseline_json: Path = DEFAULT_BASELINE_JSON,
    keyword_pool: list[str] | None = None,
    audit_history: list[dict[str, Any]] | None = None,
    y_col: str = "y_rule",
) -> AnalyticsSnapshot:
    df = pd.read_csv(features_csv)
    baseline = json.loads(baseline_json.read_text(encoding="utf-8"))
    baseline_auc = (
        baseline.get("cross_validation", {}).get("roc_auc_mean")
        or baseline.get("holdout_roc_auc")
        or 0.5
    )
    stats = {
        "title_emoji_count": _emoji0_vs_ge1(df, y_col),
        "title_has_question": _question_group(df, y_col),
        "title_hashtag_count": _hashtag_group(df, y_col),
    }
    return AnalyticsSnapshot(
        sample_count=int(len(df)),
        baseline_auc=float(baseline_auc),
        baseline_coefficients=dict(baseline.get("coefficients", {})),
        feature_stats=stats,
        active_rules=[dict(r) for r in CURRENT_RULES],
        keyword_pool=list(keyword_pool) if keyword_pool else list(DEFAULT_KEYWORD_POOL),
        audit_history=list(audit_history) if audit_history else [],
    )




SYSTEM_PROMPT = """你是 Hermes —— 小红书诊断项目的"微调官"。

# 思考纪律（最重要）
- 不要长篇推理。你的思考 ≤150字，然后立刻输出 JSON。
- 不要列举所有可能再排除，直接锁定一个最有胜算的方向。
- 最终输出必须是**一个 JSON 对象**，不是 markdown，不要解释。

# 你的唯一职责
根据数分中心给你的快照，提出**恰好 1 条**微调提案（不是重构、不是新规则）。

# 硬约束（违反必被审核员一票否决）
1. 只能修改以下 3 类对象之一：
   - `threshold`：现有规则的触发阈值（例：==0 → <=1；>=2 → >=3）
   - `keyword_pool`：爬虫关键词池（增/减 1-2 个）
   - `prompt`：诊断报告人话文案（单条 action 的一段文字）
2. 每次只改一个参数，改动幅度只能是"相邻值"（不准阈值从 2 跳到 5）。
3. 禁止删除核心规则：`REMOVE_TITLE_QUESTION_MARK` 和 `ADD_ONE_TITLE_EMOJI`。
4. 阈值类提案必须给出 `trigger_predicate`，让审核员能跑统计检验。

# 审核员会对你做 4 项硬检查，你要心里有数
- γ 核心规则保护（删核心 → 直接驳回）
- β 新触发样本 ≥ 30 条
- α 触发指示列的 bootstrap 系数 95% CI 不跨 0，p_bootstrap < 0.05
- δ 加入该指示列后整体 AUC 退化 ≤ 0.02

# 输出格式（严格 JSON，不要有任何解释文字、不要 Markdown 代码块）
{
  "kind": "threshold" | "keyword_pool" | "prompt",
  "target": "简短描述改哪里，例如 'ADD_ONE_TITLE_EMOJI.title_emoji_count'",
  "before": <原值>,
  "after": <新值>,
  "rationale": "中文，≤80字，说明你看到快照里哪个数字支持这个改动",
  "trigger_predicate": {"feature": "...", "op": "<|<=|>|>=|==|!=", "value": <数字>}
}

# 关键提醒
- 如果上一轮因为 α 不显著被驳回，说明证据不够 —— 换一个方向改，或者放弃当前这类。
- 你不能越权做大改动。越权 → 返工。
- 只输出那一个 JSON 对象，不要加 "好的我来提案" 之类的开场白。
"""


def _strip_json_fence(s: str) -> str:
    s = s.strip()
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        return m.group(0)
    return s


_KW_SEPARATORS_RE = re.compile(r"[、,，;；\s]+")


def _normalize_keyword_field(val: Any) -> list[str] | Any:
    """LLM 偶尔把 keyword_pool 的 before/after 返回成 "kw1、kw2、kw3" 字符串；
    把这种情况规范成 list[str]。已经是 list 则去重保序后原样返回。"""
    if isinstance(val, list):
        seen: set[str] = set()
        out: list[str] = []
        for x in val:
            s = str(x).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    if isinstance(val, str):
        parts = [p.strip() for p in _KW_SEPARATORS_RE.split(val) if p.strip()]
        seen2: set[str] = set()
        out2: list[str] = []
        for p in parts:
            if p not in seen2:
                seen2.add(p)
                out2.append(p)
        return out2
    return val


def parse_proposal(raw: str) -> TuningProposal | None:
    """把 LLM 原始输出转成 TuningProposal；失败返回 None。"""
    try:
        s = _strip_json_fence(raw)
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    kind = obj.get("kind")
    if kind not in ("threshold", "keyword_pool", "prompt"):
        return None
    before = obj.get("before")
    after = obj.get("after")
    if kind == "keyword_pool":
        before = _normalize_keyword_field(before)
        after = _normalize_keyword_field(after)
    return TuningProposal(
        kind=kind,
        target=str(obj.get("target", "")),
        before=before,
        after=after,
        rationale=str(obj.get("rationale", "")),
        trigger_predicate=obj.get("trigger_predicate"),
        deleted_action_codes=list(obj.get("deleted_action_codes") or []),
    )


KIND_CONSTRAINT_SUFFIX = {
    "threshold": "\n\n# 本轮强制约束\n本次只能提 `threshold` 类提案（调现有规则的阈值），不能提 keyword_pool 或 prompt。",
    "keyword_pool": "\n\n# 本轮强制约束\n本次只能提 `keyword_pool` 类提案（增删爬虫关键词），不能提 threshold 或 prompt。",
    "prompt": "\n\n# 本轮强制约束\n本次只能提 `prompt` 类提案（改诊断报告文案），不能提 threshold 或 keyword_pool。",
}


def propose_tuning(
    snapshot: AnalyticsSnapshot,
    force_kind: Literal["threshold", "keyword_pool", "prompt"] | None = None,
) -> tuple[TuningProposal | None, dict[str, Any]]:
    """调一次 MiniMax 产出一个提案。返回 (proposal_or_None, debug_dict)。

    force_kind：如指定，在 system prompt 末尾加一条强制约束，让 LLM 只提该类。
    """
    user_text = snapshot.to_llm_text()
    system = SYSTEM_PROMPT + (KIND_CONSTRAINT_SUFFIX.get(force_kind, "") if force_kind else "")
    ok, content, cost = call_minimax(system, user_text, max_tokens=MINIMAX_MAX_TOKENS)
    debug = {"llm_ok": ok, "llm_cost_sec": round(cost, 2), "raw": content[:600]}
    if not ok:
        return None, debug
    proposal = parse_proposal(content)
    debug["parsed"] = proposal is not None
    if proposal and force_kind and proposal.kind != force_kind:
        debug["kind_mismatch"] = f"expected={force_kind} got={proposal.kind}"
        return None, debug
    return proposal, debug


def propose_and_audit_loop(
    snapshot: AnalyticsSnapshot,
    max_rounds: int = 3,
    force_kind: Literal["threshold", "keyword_pool", "prompt"] | None = None,
) -> list[dict[str, Any]]:
    """链路：提议 → 审核 → 若 REJECT 喂回 reason 重提，最多 max_rounds 轮。

    返回每轮的 {round, proposal, audit, debug} 列表。
    """
    rounds: list[dict[str, Any]] = []
    working_history = list(snapshot.audit_history)
    for r in range(1, max_rounds + 1):
        snapshot_r = AnalyticsSnapshot(
            sample_count=snapshot.sample_count,
            baseline_auc=snapshot.baseline_auc,
            baseline_coefficients=snapshot.baseline_coefficients,
            feature_stats=snapshot.feature_stats,
            active_rules=snapshot.active_rules,
            keyword_pool=snapshot.keyword_pool,
            audit_history=list(working_history),
        )
        proposal, debug = propose_tuning(snapshot_r, force_kind=force_kind)
        row: dict[str, Any] = {"round": r, "debug": debug}
        if proposal is None:
            row["proposal"] = None
            row["audit"] = None
            rounds.append(row)
            break
        report = audit_proposal(proposal)
        row["proposal"] = {
            "kind": proposal.kind,
            "target": proposal.target,
            "before": proposal.before,
            "after": proposal.after,
            "rationale": proposal.rationale,
            "trigger_predicate": proposal.trigger_predicate,
        }
        row["audit"] = {
            "passed": report.passed,
            "verdict": report.verdict,
            "gates": [
                {"name": g.name, "applicable": g.applicable, "passed": g.passed, "reason": g.reason}
                for g in report.gates
            ],
        }
        rounds.append(row)
        if report.passed:
            break
        failed_reasons = [g.reason for g in report.gates if g.applicable and not g.passed]
        working_history.append(
            {
                "target": proposal.target,
                "before": proposal.before,
                "after": proposal.after,
                "reject_reason": " | ".join(failed_reasons) or "unknown",
            }
        )
    return rounds


def _print_rounds(rounds: list[dict[str, Any]]) -> None:
    for row in rounds:
        print(f"--- Round {row['round']} ---")
        d = row["debug"]
        print(f"  LLM  | ok={d['llm_ok']} cost={d['llm_cost_sec']}s parsed={d.get('parsed')}"
              + (f" MISMATCH:{d['kind_mismatch']}" if d.get("kind_mismatch") else ""))
        if row.get("proposal"):
            p = row["proposal"]
            print(f"  提案 | kind={p['kind']} target={p['target']}  {p['before']} → {p['after']}")
            print(f"        rationale: {p['rationale']}")
            if p.get("trigger_predicate"):
                tp = p["trigger_predicate"]
                print(f"        trigger: {tp['feature']} {tp['op']} {tp['value']}")
        else:
            print(f"  提案 | (None) raw_head: {d.get('raw','')[:200]}")
            continue
        a = row["audit"]
        print(f"  审核 | {a['verdict']} passed={a['passed']}")
        for g in a["gates"]:
            tag = "APPL" if g["applicable"] else "skip"
            mark = "PASS" if g["passed"] else "FAIL"
            print(f"        [{tag}] [{mark}] {g['name']}: {g['reason']}")
        if a["passed"]:
            print("  ==> 链路结束：提案通过")


def _demo() -> None:
    snapshot = collect_analytics_snapshot()
    print(f"=== 数分快照（样本 n={snapshot.sample_count}, 基线 AUC={snapshot.baseline_auc:.4f}）===\n")
    print(snapshot.to_llm_text())

    print("\n\n=== 场景 A：LLM 自由发挥（最多 3 轮）===\n")
    rounds_a = propose_and_audit_loop(snapshot, max_rounds=3)
    _print_rounds(rounds_a)

    print("\n\n=== 场景 B：强制 threshold，完整跑 α/β/γ/δ 四道硬门禁（最多 3 轮）===\n")
    rounds_b = propose_and_audit_loop(snapshot, max_rounds=3, force_kind="threshold")
    _print_rounds(rounds_b)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _demo()
