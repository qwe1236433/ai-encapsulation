"""
research/validate_formula_v2.py
================================
反向验证 v2 模型学到的"公式"是否可行。

四个独立的可信度检验：

  1. **系数 Bootstrap 区间**：N 次有放回采样重训，统计每个特征系数的
     95% 置信区间和符号稳定性。系数 95% CI 跨过 0 表示该特征在统计上
     与标签无关；占比 100% 同号才算"稳定特征"。

  2. **precision@K**：用模型分对时间外测试集排序，取 top-K 看真正
     "高赞"的比例 vs 随机基线 (= 总体阳性率)。lift 越大越说明模型有
     真实排序能力——即使 AUC 不高，只要能把"前 K%"识别出来就有用。

  3. **子样本稳定性**：按时间把数据分 N 段，每段单独训练，看每个
     特征的系数符号在 N 段间的一致比例。生产可用的"公式特征"必须
     跨段一致。

  4. **特征剔除消融**：依次移除单个 top 特征，看 hold-out AUC 跌多少。
     真正贡献的特征剔除后 AUC 应明显下降。

用法（仓库根目录）：

  python research/validate_formula_v2.py \\
      --features research/features_v2.csv \\
      --baseline research/artifacts/baseline_v2_time.json \\
      --out research/artifacts/formula_validation_v2.json \\
      --bootstrap 200 --time-segments 4

输出：
  - 控制台：分项结论 + 综合判决
  - JSON：完整指标，可被 EXPERIMENT_REPORT.md 引用
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows 控制台强制 UTF-8（避免 GBK 编码错误）
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.train_baseline_v0 import (  # noqa: E402
    _utc_from_published_cell,
    build_design_matrix_with_frame,
)


# ── 工具 ─────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _percentile(arr, q):
    import numpy as np
    return float(np.percentile(arr, q))


def _safe_auc(y_true, y_score) -> float:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, y_score))


# ── 1. Bootstrap 系数区间 ────────────────────────────────────────────────────

def bootstrap_coefficients(
    X, y, feats: list[str], n_iter: int = 200, seed: int = 42, max_iter: int = 2000,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    rng = np.random.default_rng(seed)
    n = len(y)
    coef_samples = np.zeros((n_iter, len(feats)))
    intercept_samples = np.zeros(n_iter)

    skipped = 0
    for i in range(n_iter):
        idx = rng.integers(0, n, size=n)
        Xb, yb = X[idx], y[idx]
        if len(np.unique(yb)) < 2:
            skipped += 1
            continue
        m = LogisticRegression(max_iter=max_iter, random_state=seed + i)
        m.fit(Xb, yb)
        coef_samples[i] = m.coef_[0]
        intercept_samples[i] = m.intercept_[0]

    valid = (coef_samples != 0).any(axis=1)
    if skipped:
        _log(f"  bootstrap: 跳过 {skipped}/{n_iter} 次（采样后只有一类）")
    cs = coef_samples[valid]

    out: dict[str, Any] = {"n_iter": int(n_iter), "n_valid": int(valid.sum()), "features": []}
    for j, name in enumerate(feats):
        col = cs[:, j]
        if len(col) == 0:
            continue
        ci_lo = _percentile(col, 2.5)
        ci_hi = _percentile(col, 97.5)
        same_sign = float(np.mean(np.sign(col) == np.sign(np.mean(col))))
        crosses_zero = (ci_lo <= 0 <= ci_hi)
        out["features"].append({
            "name": name,
            "coef_mean": float(np.mean(col)),
            "coef_std": float(np.std(col, ddof=1)),
            "ci95_lo": ci_lo,
            "ci95_hi": ci_hi,
            "same_sign_ratio": round(same_sign, 4),
            "crosses_zero": bool(crosses_zero),
            "verdict": "STABLE" if (not crosses_zero and same_sign >= 0.95) else "UNSTABLE",
        })
    out["features"].sort(key=lambda d: abs(d["coef_mean"]), reverse=True)
    return out


# ── 2. precision@K + lift ────────────────────────────────────────────────────

def precision_at_k(y_true, y_score, k_ratios=(0.05, 0.10, 0.20, 0.30)) -> dict[str, Any]:
    import numpy as np
    n = len(y_true)
    base_rate = float(np.mean(y_true)) if n else 0.0
    order = np.argsort(-np.asarray(y_score))
    out: dict[str, Any] = {"n_test": int(n), "base_rate": round(base_rate, 4), "ks": []}
    for kr in k_ratios:
        k = max(1, int(round(n * kr)))
        topk_y = np.asarray(y_true)[order[:k]]
        prec = float(np.mean(topk_y))
        lift = (prec / base_rate) if base_rate > 0 else float("nan")
        out["ks"].append({
            "k_ratio": kr,
            "k": int(k),
            "precision": round(prec, 4),
            "lift_over_base": round(lift, 3) if not math.isnan(lift) else None,
        })
    return out


# ── 3. 子样本稳定性（时间分段） ─────────────────────────────────────────────

def time_segment_stability(
    df_aligned, X, y, feats: list[str],
    n_segments: int = 4, seed: int = 42, max_iter: int = 2000,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    if "published_at" not in df_aligned.columns:
        return {"skipped": True, "reason": "no_published_at"}

    ts = df_aligned["published_at"].astype(str).map(_utc_from_published_cell)
    valid_mask = ts.notna()
    n_valid = int(valid_mask.sum())
    if n_valid < n_segments * 20:
        return {"skipped": True, "reason": f"insufficient_data: {n_valid} < {n_segments * 20}"}

    order = ts[valid_mask].argsort(kind="mergesort").values
    idx_sorted = np.where(valid_mask)[0][order]

    seg_size = len(idx_sorted) // n_segments
    seg_coefs: list[np.ndarray | None] = []
    seg_meta: list[dict[str, Any]] = []
    for s in range(n_segments):
        a, b = s * seg_size, (s + 1) * seg_size if s < n_segments - 1 else len(idx_sorted)
        seg_idx = idx_sorted[a:b]
        Xs, ys = X[seg_idx], y[seg_idx]
        if len(np.unique(ys)) < 2:
            seg_coefs.append(None)
            seg_meta.append({"segment": s, "n": len(seg_idx), "skipped": True, "reason": "single_class"})
            continue
        m = LogisticRegression(max_iter=max_iter, random_state=seed + s)
        m.fit(Xs, ys)
        seg_coefs.append(m.coef_[0])
        seg_meta.append({
            "segment": s, "n": len(seg_idx),
            "pos_rate": round(float(np.mean(ys)), 4),
            "skipped": False,
        })

    valid_coefs = [c for c in seg_coefs if c is not None]
    if len(valid_coefs) < 2:
        return {
            "skipped": False,
            "n_segments_requested": n_segments,
            "n_segments_valid": len(valid_coefs),
            "warning": "less_than_2_valid_segments",
            "segments": seg_meta,
        }
    coef_matrix = np.vstack(valid_coefs)
    out: dict[str, Any] = {
        "skipped": False,
        "n_segments_requested": n_segments,
        "n_segments_valid": len(valid_coefs),
        "segments": seg_meta,
        "features": [],
    }
    for j, name in enumerate(feats):
        col = coef_matrix[:, j]
        signs = np.sign(col)
        majority = np.sign(np.sum(signs))
        consistency = float(np.mean(signs == majority))
        out["features"].append({
            "name": name,
            "mean_coef": float(np.mean(col)),
            "std_coef": float(np.std(col, ddof=1)) if len(col) > 1 else 0.0,
            "sign_consistency": round(consistency, 4),
            "verdict": "STABLE" if consistency >= 0.75 else "UNSTABLE",
        })
    out["features"].sort(key=lambda d: abs(d["mean_coef"]), reverse=True)
    return out


# ── 4. 特征剔除消融 ─────────────────────────────────────────────────────────

def feature_ablation(
    X, y, feats: list[str], top_n: int = 8, seed: int = 42, max_iter: int = 2000,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.3, random_state=seed, stratify=y
    )
    base = LogisticRegression(max_iter=max_iter, random_state=seed)
    base.fit(X_train, y_train)
    base_proba = base.predict_proba(X_test)[:, 1]
    base_auc = _safe_auc(y_test, base_proba)

    abs_coef_order = np.argsort(-np.abs(base.coef_[0]))
    selected = abs_coef_order[:top_n]

    rows: list[dict[str, Any]] = []
    for j in selected:
        cols = [k for k in range(X.shape[1]) if k != j]
        m = LogisticRegression(max_iter=max_iter, random_state=seed)
        m.fit(X_train[:, cols], y_train)
        proba = m.predict_proba(X_test[:, cols])[:, 1]
        auc = _safe_auc(y_test, proba)
        delta = (base_auc - auc) if not math.isnan(base_auc) and not math.isnan(auc) else float("nan")
        rows.append({
            "removed": feats[j],
            "auc_without": round(auc, 4) if not math.isnan(auc) else None,
            "auc_drop": round(delta, 4) if not math.isnan(delta) else None,
        })
    rows.sort(key=lambda d: (d["auc_drop"] or 0), reverse=True)
    return {
        "base_auc": round(base_auc, 4) if not math.isnan(base_auc) else None,
        "ablation": rows,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="v2 公式反向验证")
    ap.add_argument("--features", required=True, help="features CSV (含 v2 全部列)")
    ap.add_argument("--baseline", required=True, help="baseline_v2*.json (训练好的 schema=v2)")
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--bootstrap", type=int, default=200)
    ap.add_argument("--time-segments", type=int, default=4)
    ap.add_argument("--ablation-top", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        import numpy as np  # noqa: F401
        from sklearn.linear_model import LogisticRegression  # noqa: F401
    except ImportError:
        print("请先安装 research/requirements-research.txt", flush=True)
        return 2

    fp_features = Path(args.features).resolve()
    fp_baseline = Path(args.baseline).resolve()
    if not fp_features.is_file():
        print(f"找不到 features CSV: {fp_features}", flush=True)
        return 2
    if not fp_baseline.is_file():
        print(f"找不到 baseline JSON: {fp_baseline}", flush=True)
        return 2

    payload = json.loads(fp_baseline.read_text(encoding="utf-8-sig"))
    if payload.get("schema") != "feature_schema_v2":
        print(f"错误：baseline schema 必须为 feature_schema_v2，实际 {payload.get('schema')}", flush=True)
        return 2

    _log(f"加载 features: {fp_features.name}")
    _log(f"加载 baseline: {fp_baseline.name}  (schema=v2)")

    X, y, feats, _prov, df_aligned = build_design_matrix_with_frame(
        fp_features, feature_schema="v2", allow_mixed_batch=True,
    )
    _log(f"样本: n={len(y)}, 阳性={int(y.sum())} ({y.mean():.1%}), 特征={len(feats)}")

    # 1. Bootstrap 系数区间
    _log(f"\n━━━ 1/4  Bootstrap 系数区间 (n_iter={args.bootstrap}) ━━━")
    boot = bootstrap_coefficients(X, y, feats, n_iter=args.bootstrap, seed=args.seed)
    stable = [f for f in boot["features"] if f["verdict"] == "STABLE"]
    _log(f"  稳定特征 (95% CI 不跨 0 + 同号率≥95%): {len(stable)}/{len(feats)}")
    for f in boot["features"][:8]:
        sym = "+" if f["coef_mean"] >= 0 else "-"
        _log(
            f"    {sym}{abs(f['coef_mean']):.3f} +/- {f['coef_std']:.3f}  "
            f"[{f['ci95_lo']:+.3f}, {f['ci95_hi']:+.3f}]  {f['name']:24s}  {f['verdict']}"
        )

    # 2. precision@K（重新训练 + 时间外切分）
    _log("\n━━━ 2/4  precision@K (时间外测试集) ━━━")
    if "published_at" in df_aligned.columns:
        ts = df_aligned["published_at"].astype(str).map(_utc_from_published_cell)
        valid_mask = ts.notna()
        order = ts[valid_mask].argsort(kind="mergesort").values
        import numpy as np
        idx_sorted = np.where(valid_mask)[0][order]
        n_split = int(round(len(idx_sorted) * 0.7))
        train_idx = idx_sorted[:n_split]
        test_idx = idx_sorted[n_split:]
        clf = LogisticRegression(max_iter=2000, random_state=args.seed)
        clf.fit(X[train_idx], y[train_idx])
        proba = clf.predict_proba(X[test_idx])[:, 1]
        prec_block = precision_at_k(y[test_idx], proba)
        prec_block["split"] = "time"
        prec_block["n_train"] = int(len(train_idx))
        prec_block["holdout_auc"] = round(_safe_auc(y[test_idx], proba), 4)
    else:
        prec_block = {"skipped": True, "reason": "no_published_at"}
    if not prec_block.get("skipped"):
        _log(f"  base rate (整体阳性率): {prec_block['base_rate']:.2%}  hold-out AUC={prec_block['holdout_auc']}")
        for k in prec_block["ks"]:
            lift = k["lift_over_base"]
            lift_str = f"x{lift:.2f}" if lift is not None else "n/a"
            _log(
                f"    top-{int(k['k_ratio']*100):>2d}%  (k={k['k']:>3d})  "
                f"precision={k['precision']:.2%}  lift={lift_str}"
            )

    # 3. 子样本（时间）稳定性
    _log(f"\n━━━ 3/4  时间分段稳定性 (n_segments={args.time_segments}) ━━━")
    stab = time_segment_stability(df_aligned, X, y, feats,
                                  n_segments=args.time_segments, seed=args.seed)
    if stab.get("skipped"):
        _log(f"  跳过: {stab.get('reason')}")
    else:
        stable_seg = [f for f in stab["features"] if f["verdict"] == "STABLE"]
        _log(
            f"  跨段稳定特征 (符号一致率≥75%): {len(stable_seg)}/{len(feats)}  "
            f"(有效段 {stab['n_segments_valid']}/{stab['n_segments_requested']})"
        )
        for f in stab["features"][:8]:
            _log(
                f"    coef={f['mean_coef']:+.3f} (std {f['std_coef']:.3f})  "
                f"一致率={f['sign_consistency']:.0%}  {f['name']:24s}  {f['verdict']}"
            )

    # 4. 特征剔除消融
    _log(f"\n━━━ 4/4  Top-{args.ablation_top} 特征剔除消融 ━━━")
    abl = feature_ablation(X, y, feats, top_n=args.ablation_top, seed=args.seed)
    _log(f"  基线 AUC: {abl['base_auc']}")
    for r in abl["ablation"]:
        drop = r["auc_drop"]
        drop_str = f"{drop:+.4f}" if drop is not None else "n/a"
        _log(
            f"    剔除 {r['removed']:24s}  AUC→{r['auc_without']}  Δ={drop_str}"
        )

    # ── 综合判决 ─────────────────────────────────────────────────────────────
    _log("\n━━━ 综合判决 ━━━")
    n_stable_boot = sum(1 for f in boot["features"] if f["verdict"] == "STABLE")
    n_stable_seg = (
        0 if stab.get("skipped")
        else sum(1 for f in stab["features"] if f["verdict"] == "STABLE")
    )
    holdout_auc = prec_block.get("holdout_auc") if not prec_block.get("skipped") else None
    top_lift = max(
        (k["lift_over_base"] for k in prec_block.get("ks", []) if k["lift_over_base"]),
        default=None,
    ) if not prec_block.get("skipped") else None

    if n_stable_boot == 0:
        verdict = "FAIL_no_stable_signal"
        verdict_msg = "Bootstrap 下没有特征通过稳定性检验，公式没有可信信号"
    elif top_lift and top_lift >= 1.5 and n_stable_boot >= 2:
        verdict = "PASS_actionable"
        verdict_msg = f"top-K lift={top_lift:.2f}x + {n_stable_boot} 个稳定特征，公式可用于排序"
    elif top_lift and top_lift >= 1.2:
        verdict = "WEAK_signal_only"
        verdict_msg = f"top-K lift={top_lift:.2f}x，弱排序信号，仅可作辅助"
    else:
        verdict = "INSUFFICIENT"
        verdict_msg = f"top-K lift={top_lift}, 稳定特征={n_stable_boot}，公式不足以指导生产"

    _log(f"  → {verdict}: {verdict_msg}")

    # ── 写产出 ──────────────────────────────────────────────────────────────
    out_payload = {
        "schema": "formula_validation_v2_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_features_path": str(fp_features),
        "input_baseline_path": str(fp_baseline),
        "n_samples": int(len(y)),
        "n_positive": int(y.sum()),
        "feature_count": len(feats),
        "bootstrap": boot,
        "precision_at_k": prec_block,
        "time_segment_stability": stab,
        "feature_ablation": abl,
        "summary": {
            "n_stable_bootstrap": n_stable_boot,
            "n_stable_time_segment": n_stable_seg,
            "holdout_auc_time": holdout_auc,
            "top_lift": top_lift,
            "verdict": verdict,
            "verdict_msg": verdict_msg,
        },
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[完成] -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
