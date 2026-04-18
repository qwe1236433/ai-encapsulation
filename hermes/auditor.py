"""
审核员（Auditor）—— 守门人，对 Hermes 提出的"微调提案"做严格审核。

双层结构：
  1. 硬门禁（纯 Python 数学/逻辑，无 LLM）：任一不过 → fail。
     α 统计显著性：新规则触发组的系数 p < 0.05 且 95% CI 不跨 0
     β 样本量底线：新规则触发的样本数 ≥ 30
     γ 核心规则保护：不得删除两条强规则的 action_code
     δ AUC 不退化：新系数的 holdout AUC ≥ baseline - 0.02
  2. 软门禁（调 MiniMax）：
     ε 语义自洽性：threshold 改动后，执行对应动作是否仍然合乎规则语义
       —— 堵"统计成立但业务矛盾"的提案（如：规则是"减少到 1"，新阈值却让 hashtag==1 也触发）
       LLM 不可用时 skip，不一票否决整条链路

审核对象 TuningProposal 分三类：
  - threshold: 诊断规则阈值微调（如 ADD_ONE_TITLE_EMOJI 从 <2 改 <1） → 全部硬门禁
  - keyword_pool: 爬虫关键词池改动 → 只过 γ
  - prompt: 诊断报告人话文案改动 → 只过 γ + 软门禁（软门禁这里留桩）

设计原则：
  - 一票否决：任一硬门禁不过 → 整条提案 fail
  - 可追溯：每条门禁返回具体数字证据，记入 audit_report
  - 不动模型训练流程：只读 baseline_v2.json 和 features_v2.csv，不改写
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

_HERMES_DIR = Path(__file__).resolve().parent
if str(_HERMES_DIR) not in sys.path:
    sys.path.insert(0, str(_HERMES_DIR))
from _minimax import call_minimax  # noqa: E402

warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
from sklearn.exceptions import ConvergenceWarning  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.model_selection import StratifiedKFold  # noqa: E402

warnings.filterwarnings("ignore", category=ConvergenceWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FEATURES_CSV = REPO_ROOT / "research" / "features_v2.csv"
DEFAULT_BASELINE_JSON = REPO_ROOT / "research" / "artifacts" / "baseline_v2.json"

CORE_ACTION_CODES = frozenset(
    {
        "REMOVE_TITLE_QUESTION_MARK",
        "ADD_ONE_TITLE_EMOJI",
    }
)

AUC_TOLERANCE = 0.02
MIN_SAMPLE_TRIGGER = 30
SIGNIFICANCE_ALPHA = 0.05
BOOTSTRAP_ROUNDS = 200
RNG_SEED = 42


@dataclass
class TuningProposal:
    """Hermes 给审核员的微调提案。"""

    kind: Literal["threshold", "keyword_pool", "prompt"]
    target: str
    before: Any
    after: Any
    rationale: str = ""
    trigger_predicate: dict[str, Any] | None = None
    coefficients_override: dict[str, float] | None = None
    deleted_action_codes: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    name: str
    applicable: bool
    passed: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    proposal: TuningProposal
    passed: bool
    verdict: str
    gates: list[GateResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["proposal"]["kind"] = self.proposal.kind
        return d


def _load_features(features_csv: Path = DEFAULT_FEATURES_CSV) -> pd.DataFrame:
    if not features_csv.is_file():
        raise FileNotFoundError(f"features csv missing: {features_csv}")
    return pd.read_csv(features_csv)


def _load_baseline(baseline_json: Path = DEFAULT_BASELINE_JSON) -> dict[str, Any]:
    if not baseline_json.is_file():
        raise FileNotFoundError(f"baseline json missing: {baseline_json}")
    return json.loads(baseline_json.read_text(encoding="utf-8"))


def _eval_predicate(df: pd.DataFrame, predicate: dict[str, Any]) -> pd.Series:
    """把 proposal.trigger_predicate 求值成布尔 Series。格式：{feature: str, op: "<"|"<="|">"|">="|"=="|"!=", value: float}"""
    feat = predicate["feature"]
    op = predicate["op"]
    val = predicate["value"]
    if feat not in df.columns:
        raise KeyError(f"feature '{feat}' not in features csv columns")
    s = df[feat]
    if op == "<":
        return s < val
    if op == "<=":
        return s <= val
    if op == ">":
        return s > val
    if op == ">=":
        return s >= val
    if op == "==":
        return s == val
    if op == "!=":
        return s != val
    raise ValueError(f"unknown op: {op}")


def gate_beta_sample_size(proposal: TuningProposal, df: pd.DataFrame) -> GateResult:
    """β：新规则触发的样本数 ≥ 30。"""
    name = "β_sample_size"
    if proposal.kind != "threshold" or proposal.trigger_predicate is None:
        return GateResult(name=name, applicable=False, passed=True, reason="not_applicable_to_non_threshold")
    triggered = _eval_predicate(df, proposal.trigger_predicate)
    n_triggered = int(triggered.sum())
    n_total = int(len(df))
    ok = n_triggered >= MIN_SAMPLE_TRIGGER
    return GateResult(
        name=name,
        applicable=True,
        passed=ok,
        reason=f"triggered={n_triggered}/{n_total} threshold={MIN_SAMPLE_TRIGGER}",
        evidence={"n_triggered": n_triggered, "n_total": n_total, "threshold": MIN_SAMPLE_TRIGGER},
    )


def gate_gamma_core_rules(proposal: TuningProposal) -> GateResult:
    """γ：不得删除两条强规则（REMOVE_TITLE_QUESTION_MARK / ADD_ONE_TITLE_EMOJI）。"""
    name = "γ_core_rules"
    deleted = set(proposal.deleted_action_codes or [])
    forbidden_hit = deleted & CORE_ACTION_CODES
    ok = not forbidden_hit
    return GateResult(
        name=name,
        applicable=True,
        passed=ok,
        reason="ok" if ok else f"attempted_to_delete_core={sorted(forbidden_hit)}",
        evidence={"deleted": sorted(deleted), "protected": sorted(CORE_ACTION_CODES)},
    )


def gate_alpha_significance(
    proposal: TuningProposal,
    df: pd.DataFrame,
    y_col: str = "y_rule",
) -> GateResult:
    """α：对"是否触发新规则"跑 bootstrap logistic regression，要 95% CI 不跨 0 且 p<0.05。"""
    name = "α_significance"
    if proposal.kind != "threshold" or proposal.trigger_predicate is None:
        return GateResult(name=name, applicable=False, passed=True, reason="not_applicable_to_non_threshold")
    if y_col not in df.columns:
        return GateResult(name=name, applicable=True, passed=False, reason=f"target '{y_col}' missing")
    mask = df[y_col].notna()
    sub = df.loc[mask].copy()
    sub["_trig"] = _eval_predicate(sub, proposal.trigger_predicate).astype(int)
    y = sub[y_col].astype(int).to_numpy()
    x = sub["_trig"].to_numpy().reshape(-1, 1)
    if len(np.unique(x)) < 2 or len(np.unique(y)) < 2:
        return GateResult(name=name, applicable=True, passed=False, reason="degenerate_split")

    rng = np.random.default_rng(RNG_SEED)
    coefs: list[float] = []
    n = len(sub)
    for _ in range(BOOTSTRAP_ROUNDS):
        idx = rng.integers(0, n, size=n)
        xb, yb = x[idx], y[idx]
        if len(np.unique(xb)) < 2 or len(np.unique(yb)) < 2:
            continue
        try:
            lr = LogisticRegression(C=1e8, solver="lbfgs", max_iter=200)
            lr.fit(xb, yb)
            coefs.append(float(lr.coef_[0][0]))
        except Exception:
            continue
    if len(coefs) < 40:
        return GateResult(name=name, applicable=True, passed=False, reason=f"bootstrap_too_few_success={len(coefs)}")
    coefs_arr = np.array(coefs)
    ci_low = float(np.percentile(coefs_arr, 100 * SIGNIFICANCE_ALPHA / 2))
    ci_high = float(np.percentile(coefs_arr, 100 * (1 - SIGNIFICANCE_ALPHA / 2)))
    mean_coef = float(np.mean(coefs_arr))
    spans_zero = ci_low <= 0 <= ci_high
    frac_sign = float(np.mean(np.sign(coefs_arr) == np.sign(mean_coef)))
    p_bootstrap = 2 * min(frac_sign, 1 - frac_sign)
    ok = (not spans_zero) and (p_bootstrap < SIGNIFICANCE_ALPHA)
    return GateResult(
        name=name,
        applicable=True,
        passed=ok,
        reason=f"mean_coef={mean_coef:+.3f} CI95=[{ci_low:+.3f},{ci_high:+.3f}] p_boot={p_bootstrap:.3f}",
        evidence={
            "mean_coef": mean_coef,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "ci_spans_zero": spans_zero,
            "p_bootstrap": p_bootstrap,
            "n_bootstrap": len(coefs),
        },
    )


def gate_delta_auc(
    proposal: TuningProposal,
    df: pd.DataFrame,
    baseline: dict[str, Any],
    y_col: str = "y_rule",
) -> GateResult:
    """δ：用 5-fold CV 的 AUC mean 对比 baseline，新 AUC ≥ baseline - tolerance。

    对 threshold 类提案：我们用"加入触发指示列后重新 fit"来模拟新系数影响。
    对其他类：不适用。
    """
    name = "δ_auc_no_regression"
    if proposal.kind != "threshold" or proposal.trigger_predicate is None:
        return GateResult(name=name, applicable=False, passed=True, reason="not_applicable_to_non_threshold")
    baseline_auc = (
        baseline.get("cross_validation", {}).get("roc_auc_mean")
        or baseline.get("holdout_roc_auc")
        or 0.5
    )
    feature_names = [c for c in baseline.get("feature_names", []) if c in df.columns]
    if not feature_names:
        return GateResult(name=name, applicable=True, passed=False, reason="no_baseline_features_in_df")
    sub = df.dropna(subset=[y_col]).copy()
    x_base = sub[feature_names].fillna(0).to_numpy()
    trig = _eval_predicate(sub, proposal.trigger_predicate).astype(int).to_numpy().reshape(-1, 1)
    x_new = np.hstack([x_base, trig])
    y = sub[y_col].astype(int).to_numpy()
    try:
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RNG_SEED)
        aucs: list[float] = []
        for tr, te in skf.split(x_new, y):
            lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
            lr.fit(x_new[tr], y[tr])
            p = lr.predict_proba(x_new[te])[:, 1]
            aucs.append(roc_auc_score(y[te], p))
        new_auc = float(np.mean(aucs))
    except Exception as e:
        return GateResult(name=name, applicable=True, passed=False, reason=f"fit_failed:{type(e).__name__}:{e}")
    drop = baseline_auc - new_auc
    ok = drop <= AUC_TOLERANCE
    return GateResult(
        name=name,
        applicable=True,
        passed=ok,
        reason=f"baseline_auc={baseline_auc:.4f} new_auc={new_auc:.4f} drop={drop:+.4f} tol={AUC_TOLERANCE}",
        evidence={"baseline_auc": baseline_auc, "new_auc": new_auc, "drop": drop, "tolerance": AUC_TOLERANCE},
    )


EPSILON_SYSTEM_PROMPT = """你是【审核员 ε】—— 小红书诊断项目"阈值微调提案"的语义自洽性审查员。

# 思考纪律
- 不要长篇推理，直接判断。你的思考 ≤80字，立刻给 JSON。
- 最终输出必须是单个 JSON 对象，不要 markdown、不要解释。

# 你要判断什么
给定一条 threshold 微调提案（改了某规则的触发阈值），判断：
  "在新阈值触发时，执行该规则的动作，业务上是否仍然合乎逻辑？"

# 典型不自洽（consistent=false）
- 规则是「减少到 N」，但新阈值让本来已经 ≤ N 的样本也触发 → 没东西可减
- 规则是「加到 N」，但新阈值让本来已经 ≥ N 的样本也触发 → 已经到了
- 新阈值把方向相反的样本也囊括（如：本来"带问号删掉"，新阈值也覆盖"不带问号"的）

# 典型自洽（consistent=true）
- 阈值扩大但动作方向一致（如：emoji==0 → emoji<=1，两种情况都还能"加 emoji"）
- 阈值收窄到更严格的子集

# 输出格式（严格 JSON，无其他文字）
{"consistent": true | false, "reason": "一句话中文，≤40字"}
"""


def gate_epsilon_semantic_consistency(proposal: TuningProposal) -> GateResult:
    """ε：对 threshold 提案，调 MiniMax 判断动作-阈值语义是否自洽。

    软门禁特性：LLM 不可用 / 解析失败 → applicable=True, passed=True（skip 不否决）。
    只有 LLM 明确返回 consistent=false → passed=False 才真的驳回。
    """
    name = "ε_semantic_consistency"
    if proposal.kind != "threshold":
        return GateResult(name=name, applicable=False, passed=True, reason="skip_non_threshold")

    user_payload = (
        f"target: {proposal.target}\n"
        f"before (原触发条件): {json.dumps(proposal.before, ensure_ascii=False)}\n"
        f"after  (新触发条件): {json.dumps(proposal.after, ensure_ascii=False)}\n"
        f"rationale (提案方自述): {proposal.rationale}\n"
    )
    ok, content, cost = call_minimax(EPSILON_SYSTEM_PROMPT, user_payload, max_tokens=1200)
    if not ok:
        return GateResult(
            name=name,
            applicable=True,
            passed=True,
            reason=f"llm_unavailable_skipped: {content[:80]}",
            evidence={"llm_ok": False, "cost_sec": round(cost, 2)},
        )
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return GateResult(
            name=name,
            applicable=True,
            passed=True,
            reason="llm_parse_failed_skipped",
            evidence={"llm_ok": True, "cost_sec": round(cost, 2), "raw_head": content[:200]},
        )
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return GateResult(
            name=name,
            applicable=True,
            passed=True,
            reason="llm_json_decode_failed_skipped",
            evidence={"llm_ok": True, "cost_sec": round(cost, 2), "raw_head": content[:200]},
        )
    consistent = bool(obj.get("consistent"))
    llm_reason = str(obj.get("reason", ""))
    return GateResult(
        name=name,
        applicable=True,
        passed=consistent,
        reason=f"llm_verdict={consistent}: {llm_reason}",
        evidence={
            "consistent": consistent,
            "llm_reason": llm_reason,
            "llm_cost_sec": round(cost, 2),
        },
    )


def audit_proposal(
    proposal: TuningProposal,
    features_csv: Path = DEFAULT_FEATURES_CSV,
    baseline_json: Path = DEFAULT_BASELINE_JSON,
    enable_soft_gates: bool = True,
) -> AuditReport:
    df = _load_features(features_csv)
    baseline = _load_baseline(baseline_json)
    gates = [
        gate_gamma_core_rules(proposal),
        gate_beta_sample_size(proposal, df),
        gate_alpha_significance(proposal, df),
        gate_delta_auc(proposal, df, baseline),
    ]
    if enable_soft_gates:
        gates.append(gate_epsilon_semantic_consistency(proposal))
    passed = all(g.passed for g in gates if g.applicable)
    verdict = "PASS_ALL_GATES" if passed else "REJECT_BY_GATE"
    return AuditReport(proposal=proposal, passed=passed, verdict=verdict, gates=gates)


def _demo() -> None:
    """真实示例：Hermes 提议把 ADD_ONE_TITLE_EMOJI 的触发阈值从"== 0"放宽到"<= 1"。"""
    proposal = TuningProposal(
        kind="threshold",
        target="ADD_ONE_TITLE_EMOJI.title_emoji_count",
        before={"op": "==", "value": 0},
        after={"op": "<=", "value": 1},
        rationale="Hermes：近期批次 emoji==1 组高赞占比比 emoji==0 略高(pp~1.5)，考虑把建议覆盖到 emoji<=1。",
        trigger_predicate={"feature": "title_emoji_count", "op": "<=", "value": 1},
        deleted_action_codes=[],
    )
    report = audit_proposal(proposal)
    print("=== Auditor Demo ===")
    print(f"proposal.kind     = {report.proposal.kind}")
    print(f"proposal.target   = {report.proposal.target}")
    print(f"proposal.before   = {report.proposal.before}")
    print(f"proposal.after    = {report.proposal.after}")
    print(f"verdict           = {report.verdict}")
    print(f"passed            = {report.passed}")
    print("-- gates --")
    for g in report.gates:
        tag = "APPL" if g.applicable else "skip"
        mark = "PASS" if g.passed else "FAIL"
        print(f"  [{tag}] [{mark}] {g.name}: {g.reason}")


if __name__ == "__main__":
    _demo()
