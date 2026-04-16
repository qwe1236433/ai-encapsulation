"""
对 train_baseline_v0产出的 artifact做可审计的权重/不确定性评估（科学表述，避免过度解读）。

- 校验特征 CSV sha256 与 artifact 一致（可选 --relaxed-sha）
- 全样本标准化后重拟合逻辑回归：得到「每改变 1 个训练集标准差」方向上的系数（仅探索性，非因果）
- 分层 bootstrap 重采样：系数 95% 分位区间（反映抽样波动，非外部有效性）
- 单次标签置换：近似零模型 hold-out AUC（预期接近 0.5；仅作 sanity check）
- 自动 warnings：小样本、少数类过少、AUC 近 1.0、特征高相关

用法（仓库根）:

  python research/evaluate_baseline_weights.py --artifact research/artifacts/baseline_v0.json
  python research/evaluate_baseline_weights.py --artifact ... --features research/features_v0.csv --bootstrap 400
  python research/evaluate_baseline_weights.py --artifact ... --time-holdout-fraction 0.2

无 sklearn 时退出码 2。

--time-holdout-fraction：在可解析 published_at 的子集上按时间升序切分（较早训练、最后比例为测试），
写入 time_ordered_holdout；与 artifact 随机分层 hold-out 互补。标签列默认取 artifact 的 target_column（y_rule / y_rule_alt）。
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from train_baseline_v0 import (
    _sha256_file,
    _utc_from_published_cell,
    build_design_matrix_with_frame,
)


def _schema_to_fs(schema: str) -> str:
    s = (schema or "").strip().lower()
    if s == "feature_schema_v1":
        return "v1"
    if s == "feature_schema_v0":
        return "v0"
    raise ValueError(f"unsupported artifact schema: {schema!r}")


def _collect_warnings(
    n: int,
    y: Any,
    holdout_auc: float | None,
    corr_max_offdiag: float,
    *,
    time_holdout_auc: float | None = None,
) -> list[str]:
    w: list[str] = []
    vc = __import__("pandas").Series(y).value_counts()
    mn = int(vc.min()) if len(vc) >= 2 else 0
    if n < 50:
        w.append("small_sample_n_lt_50")
    if mn < 5:
        w.append("minority_class_lt_5")
    if holdout_auc is not None and holdout_auc >= 0.99:
        w.append("holdout_auc_very_high_check_overfit")
    if time_holdout_auc is not None and time_holdout_auc >= 0.99:
        w.append("time_holdout_auc_very_high_check_leakage_or_small_test")
    if corr_max_offdiag >= 0.95:
        w.append("feature_correlation_ge_0_95")
    return w


def _time_ordered_holdout_eval(
    X: Any,
    y: Any,
    df_frame: Any,
    *,
    holdout_fraction: float,
    seed: int,
) -> dict[str, Any]:
    """按 published_at 升序：较早训练、最后 fraction 为测试。仅使用可解析时间的行。"""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score

    frac = float(holdout_fraction)
    if frac <= 0.0 or frac >= 1.0:
        return {
            "skipped": True,
            "reason": "holdout_fraction_not_in_open_0_1",
            "holdout_fraction": frac,
        }
    if "published_at" not in getattr(df_frame, "columns", []):
        return {
            "skipped": True,
            "reason": "missing_published_at_column",
            "holdout_fraction": frac,
        }

    times: list[Any] = []
    for v in df_frame["published_at"].tolist():
        times.append(_utc_from_published_cell(v))

    idx_valid = [i for i, t in enumerate(times) if t is not None]
    n = len(idx_valid)
    if n < 4:
        return {
            "skipped": True,
            "reason": "too_few_rows_with_parseable_published_at",
            "holdout_fraction": frac,
            "n_rows_time_ok": n,
        }

    idx_valid.sort(key=lambda i: times[i])
    n_test = int(round(n * frac))
    n_test = max(1, min(n - 1, n_test))
    n_train = n - n_test
    if n_train < 1 or n_test < 1:
        return {
            "skipped": True,
            "reason": "cannot_form_train_test_after_sort",
            "holdout_fraction": frac,
            "n_rows_time_ok": n,
        }

    train_idx = np.array(idx_valid[:n_train], dtype=int)
    test_idx = np.array(idx_valid[n_train:], dtype=int)
    X_tr = X[train_idx].astype(float)
    X_te = X[test_idx].astype(float)
    y_tr = y[train_idx]
    y_te = y[test_idx]

    if len(np.unique(y_te)) < 2:
        return {
            "skipped": True,
            "reason": "test_set_single_class",
            "holdout_fraction": frac,
            "n_train": int(n_train),
            "n_test": int(n_test),
        }

    clf = LogisticRegression(max_iter=200, random_state=int(seed))
    clf.fit(X_tr, y_tr)
    proba = clf.predict_proba(X_te)[:, 1]
    try:
        auc_te = float(roc_auc_score(y_te, proba))
    except ValueError:
        auc_te = float("nan")
    try:
        brier_te = float(brier_score_loss(y_te, proba))
    except ValueError:
        brier_te = float("nan")

    t_train = [times[i] for i in train_idx.tolist()]
    t_test = [times[i] for i in test_idx.tolist()]

    def _iso_min_max(ts: list[Any]) -> tuple[str | None, str | None]:
        if not ts:
            return None, None
        tmin = min(ts)
        tmax = max(ts)
        return tmin.isoformat(), tmax.isoformat()

    tr_lo, tr_hi = _iso_min_max(t_train)
    te_lo, te_hi = _iso_min_max(t_test)

    return {
        "skipped": False,
        "method": "sort_by_published_at_utc_asc_train_earlier_test_latest_fraction",
        "holdout_fraction": frac,
        "n_rows_total_design_matrix": int(X.shape[0]),
        "n_rows_with_parseable_published_at": n,
        "n_train": int(n_train),
        "n_test": int(n_test),
        "train_published_at_utc_min": tr_lo,
        "train_published_at_utc_max": tr_hi,
        "test_published_at_utc_min": te_lo,
        "test_published_at_utc_max": te_hi,
        "roc_auc": auc_te,
        "brier_score": brier_te,
        "note": (
            "Time-ordered split is not stratified; small test n makes AUC unstable. "
            "Rows with missing/unparseable published_at are excluded from this block only."
        ),
    }


def main() -> int:
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import train_test_split
        from sklearn.preprocessing import StandardScaler
        from sklearn.utils import resample
    except ImportError:
        print("请先安装: pip install -r research/requirements-research.txt", flush=True)
        return 2

    ap = argparse.ArgumentParser(description="Baseline artifact weight evaluation")
    ap.add_argument("--artifact", type=str, required=True)
    ap.add_argument("--features", type=str, default="", help="默认用 artifact 内 input_features_path")
    ap.add_argument("--out", type=str, default="", help="默认 artifact 同目录 eval_<stem>.json")
    ap.add_argument("--bootstrap", type=int, default=300, help="0 跳过 bootstrap")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--relaxed-sha",
        action="store_true",
        help="特征文件哈希与 artifact 不一致时仍继续（仅调试）",
    )
    ap.add_argument(
        "--allow-mixed-batch",
        action="store_true",
        help="与训练一致；若 artifact 含混批 provenance 会自动开启",
    )
    ap.add_argument(
        "--time-holdout-fraction",
        type=float,
        default=0.0,
        help=">0 且 <1 时做按 published_at 的时间序 hold-out（仅可解析时间的行）；0 表示跳过",
    )
    ap.add_argument(
        "--target-column",
        type=str,
        default="",
        help="标签列 y_rule / y_rule_alt；默认用 artifact.target_column",
    )
    args = ap.parse_args()

    art_path = Path(args.artifact).expanduser().resolve()
    if not art_path.is_file():
        print(f"找不到 artifact: {art_path}", flush=True)
        return 2
    try:
        artifact: dict[str, Any] = json.loads(art_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"无法读取 artifact JSON: {e}", flush=True)
        return 2

    schema = str(artifact.get("schema") or "")
    try:
        fs = _schema_to_fs(schema)
    except ValueError as e:
        print(str(e), flush=True)
        return 2

    feat_path = Path(
        (args.features or artifact.get("input_features_path") or "").strip()
    ).expanduser().resolve()
    if not feat_path.is_file():
        print(f"找不到特征 CSV: {feat_path}", flush=True)
        return 2

    exp_sha = (artifact.get("input_features_sha256") or "").strip()
    got_sha = _sha256_file(feat_path)
    sha_ok = not exp_sha or exp_sha == got_sha
    if not sha_ok and not args.relaxed_sha:
        print(
            f"特征 sha256 与 artifact 不一致（避免错配）。期望 {exp_sha[:16]}… 实际 {got_sha[:16]}…\n"
            "若确为同一逻辑批次可加 --relaxed-sha",
            flush=True,
        )
        return 2

    tc = (args.target_column or "").strip()
    if not tc:
        tc = str(artifact.get("target_column") or "y_rule").strip()
    if tc not in ("y_rule", "y_rule_alt"):
        print("--target-column 须为 y_rule 或 y_rule_alt（或 artifact 内为二者之一）", flush=True)
        return 2

    prov_art = artifact.get("features_provenance") or {}
    allow_mix = bool(args.allow_mixed_batch) or bool(
        prov_art.get("batch_id_conflict") or prov_art.get("feed_digest_sha256_conflict")
    )
    try:
        X, y, feats, prov, df_frame = build_design_matrix_with_frame(
            feat_path,
            feature_schema=fs,
            allow_mixed_batch=allow_mix,
            target_column=tc,
        )
    except ValueError as e:
        print(str(e), flush=True)
        return 2

    n = int(X.shape[0])
    p = int(X.shape[1])
    rng = np.random.RandomState(int(args.seed))

    # 特征相关（绝对值最大非对角）
    if p >= 2:
        c = np.corrcoef(X.astype(float), rowvar=False)
        off = c[np.triu_indices(p, k=1)]
        corr_max = float(np.nanmax(np.abs(off))) if off.size else 0.0
    else:
        corr_max = 0.0

    tt = artifact.get("train_test_split") or {}
    test_size = float(tt.get("test_size", 0.3))
    split_seed = int(tt.get("random_seed", args.seed))

    hold_auc_art = artifact.get("holdout_roc_auc")
    if isinstance(hold_auc_art, (int, float)):
        hold_auc_art = float(hold_auc_art)
    else:
        hold_auc_art = None

    th_frac = float(args.time_holdout_fraction)
    time_block = _time_ordered_holdout_eval(
        X,
        y,
        df_frame,
        holdout_fraction=th_frac,
        seed=int(args.seed),
    )
    time_auc: float | None = None
    if not time_block.get("skipped"):
        raw_auc = time_block.get("roc_auc")
        if isinstance(raw_auc, float) and math.isfinite(raw_auc):
            time_auc = raw_auc
    warnings = _collect_warnings(n, y, hold_auc_art, corr_max, time_holdout_auc=time_auc)
    if 0.0 < th_frac < 1.0 and time_block.get("skipped"):
        warnings.append(
            f"time_ordered_holdout_skipped:{str(time_block.get('reason') or 'unknown')}"
        )
    if not time_block.get("skipped"):
        nt_te = int(time_block.get("n_test") or 0)
        if nt_te < 5:
            warnings.append("time_holdout_n_test_lt_5_auc_unstable")

    # 全样本 Z-score 后重拟合（探索性可比系数）
    scaler = StandardScaler()
    Xz = scaler.fit_transform(X.astype(float))
    clf_z = LogisticRegression(max_iter=200, random_state=int(args.seed))
    clf_z.fit(Xz, y)
    coef_z = {feats[i]: float(clf_z.coef_[0][i]) for i in range(p)}
    intercept_z = float(clf_z.intercept_[0])
    feat_scale = {feats[i]: float(scaler.scale_[i]) for i in range(p) if scaler.scale_[i] > 0}
    feat_mean = {feats[i]: float(scaler.mean_[i]) for i in range(p)}

    # Bootstrap：分层重采样后全样本拟合（与 coef_z 可比）
    boot_block: dict[str, Any] | None = None
    n_boot = max(0, int(args.bootstrap))
    if n_boot > 0:
        coef_rows: list[np.ndarray] = []
        for b in range(n_boot):
            Xb, yb = resample(
                X,
                y,
                replace=True,
                random_state=int(args.seed) + b,
                stratify=y,
            )
            Xbz = StandardScaler().fit_transform(Xb.astype(float))
            m = LogisticRegression(max_iter=200, random_state=int(args.seed))
            m.fit(Xbz, yb)
            coef_rows.append(m.coef_[0].copy())
        arr = np.stack(coef_rows, axis=0)
        boot_ci: dict[str, dict[str, float]] = {}
        for i, name in enumerate(feats):
            col = arr[:, i]
            boot_ci[name] = {
                "p2_5": float(np.percentile(col, 2.5)),
                "p50": float(np.percentile(col, 50.0)),
                "p97_5": float(np.percentile(col, 97.5)),
            }
        boot_block = {
            "n_bootstrap": n_boot,
            "stratified": True,
            "on_standardized_full_resample_fit": True,
            "coefficient_ci95": boot_ci,
        }

    # 零模型：置换标签单次（sanity：AUC 应接近 0.5）
    y_perm = rng.permutation(y)
    X_tr, X_te, y_tr, y_te = train_test_split(
        Xz,
        y_perm,
        test_size=test_size,
        random_state=split_seed,
        stratify=y_perm,
    )
    null_m = LogisticRegression(max_iter=200, random_state=int(args.seed))
    null_m.fit(X_tr, y_tr)
    proba_null = null_m.predict_proba(X_te)[:, 1]
    try:
        auc_null = float(roc_auc_score(y_te, proba_null))
    except ValueError:
        auc_null = float("nan")

    n_te = int(len(y_te))
    null_note = (
        "Single permuted labels; expect AUC near 0.5. "
        "Small n_holdout makes AUC unstable (0 or 1 by chance)."
    )
    if n_te < 15:
        warnings.append("null_perm_auc_unreliable_n_test_lt_15")

    out_path = Path(args.out).expanduser().resolve() if (args.out or "").strip() else (
        art_path.parent / f"eval_{art_path.stem}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "schema": "baseline_weight_evaluation_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_path": str(art_path),
        "features_path": str(feat_path),
        "input_features_sha256_expected": exp_sha or None,
        "input_features_sha256_actual": got_sha,
        "sha256_match": sha_ok,
        "target_column": tc,
        "interpretation_constraints": [
            "操作化标签列（y_rule / y_rule_alt）仅反映阈值规则；系数与 CI 描述该 CSV 内共变，不构成因果或平台真实爆文机制。",
            "standardized_coefficients_full_sample 在全样本上标准化后拟合，用于相对比较；非部署时必检校准。",
            "bootstrap CI 反映有放回重采样下的参数不确定性，不代替独立外推验证；time_ordered_holdout 为粗粒度时间外检查（依赖 published_at 质量）。",
            "若 holdout_roc_auc 接近 1 且 n 较小，优先怀疑过拟合或标签泄漏，勿外推结论。",
        ],
        "time_ordered_holdout": time_block,
        "warnings": warnings,
        "n_samples": n,
        "n_features": p,
        "feature_names": feats,
        "class_counts": __import__("pandas").Series(y).value_counts().to_dict(),
        "artifact_holdout_roc_auc": hold_auc_art,
        "artifact_holdout_brier_score": artifact.get("holdout_brier_score"),
        "artifact_coefficients": artifact.get("coefficients"),
        "artifact_intercept": artifact.get("intercept"),
        "standardized_coefficients_full_sample": coef_z,
        "standardized_intercept_full_sample": intercept_z,
        "feature_mean": feat_mean,
        "feature_std": feat_scale,
        "max_abs_feature_correlation_offdiag": round(corr_max, 6),
        "null_permutation_single_run": {
            "method": "train_test_split_on_permuted_y_same_test_size_as_artifact",
            "test_size": test_size,
            "random_seed": split_seed,
            "n_holdout": n_te,
            "holdout_roc_auc": auc_null,
            "note": null_note,
        },
        "cross_validation_from_artifact": artifact.get("cross_validation"),
        "features_provenance": prov,
    }
    if boot_block is not None:
        payload["bootstrap"] = boot_block

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    if warnings:
        print(f"warnings: {', '.join(warnings)}", flush=True)
    print(f"standardized coef (full-sample Z): {coef_z}", flush=True)
    if boot_block:
        print(f"bootstrap done: {n_boot}", flush=True)
    print(f"null permuted-y holdout AUC: {auc_null:.4f}", flush=True)
    if 0.0 < th_frac < 1.0:
        if not time_block.get("skipped"):
            print(
                f"time-ordered holdout ROC-AUC: {time_block.get('roc_auc')} "
                f"n_train={time_block.get('n_train')} n_test={time_block.get('n_test')}",
                flush=True,
            )
        else:
            print(
                f"time-ordered holdout skipped: {time_block.get('reason')}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
