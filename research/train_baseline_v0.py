"""
在 features_v0.csv 上拟合简单基线（逻辑回归），产出可审计的系数 JSON。

要求 CSV 含数值列 title_len, body_len, log1p_like 且 y_rule 为 0/1（先带 --viral-threshold 导出）。

用法（仓库根目录）:

  pip install -r research/requirements-research.txt
  python research/train_baseline_v0.py --features research/features_v0.csv --out research/artifacts/baseline_v0.json
  python research/train_baseline_v0.py ... --feature-schema v1 --out research/artifacts/baseline_v1.json

可选：写入与特征导出一致的标签契约（仅原样嵌入 JSON 对象，不编造字段）:

  python research/train_baseline_v0.py --features ... --out ... --labels-spec research/labels_spec.json

输出 JSON 含特征名、系数、截距、样本量、hold-out **ROC-AUC** 与 **Brier**；可选 **`--cv-folds K`** 分层交叉验证的 AUC/Brier 均值与标准差（小样本时有效折数自动下调）。无 sklearn 时退出并提示安装。

**feature_schema v1**（`--feature-schema v1`）：在 v0 三列基础上增加 `log1p_comment`、`log1p_collect`、`log1p_share`、`age_days`（由 CSV 的 `comment_proxy`、`collect_proxy`、`share_proxy`、`published_at` 派生）；artifact 的 `schema` 为 **`feature_schema_v1`**，工厂侧见 `.env.example` 与 `xhs_factory._baseline_lr_logistic_p`。

产出后可用 **`research/evaluate_baseline_weights.py`** 做权重评估（标准化系数、bootstrap 区间、置换基线、显式 warnings），避免仅凭点估计下结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _features_provenance_report(df: Any) -> dict[str, Any]:
    """从特征表提取批次元数据；仅使用已存在列，不推断。"""
    out: dict[str, Any] = {
        "unique_batch_ids": [],
        "unique_feed_digest_sha256": [],
        "batch_id_conflict": False,
        "feed_digest_sha256_conflict": False,
    }
    if "batch_id" in df.columns:
        u = {str(x).strip() for x in df["batch_id"].dropna() if str(x).strip()}
        out["unique_batch_ids"] = sorted(u)
        out["batch_id_conflict"] = len(u) > 1
    if "feed_digest_sha256" in df.columns:
        u2 = {str(x).strip() for x in df["feed_digest_sha256"].dropna() if str(x).strip()}
        out["unique_feed_digest_sha256"] = sorted(u2)
        out["feed_digest_sha256_conflict"] = len(u2) > 1
    return out


def _load_labels_spec_dict(path: Path) -> dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
        raw = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取或解析 labels_spec: {path} ({e})") from e
    if not isinstance(raw, dict):
        raise ValueError(f"labels_spec 必须是 JSON 对象: {path}")
    return raw


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_V0_FEAT: tuple[str, ...] = ("title_len", "body_len", "log1p_like")
_V1_FEAT: tuple[str, ...] = (
    "title_len",
    "body_len",
    "log1p_like",
    "log1p_comment",
    "log1p_collect",
    "log1p_share",
    "age_days",
)


def _utc_from_published_cell(s: Any) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, float) and math.isnan(s):
        return None
    s = str(s).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        s_iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _build_v1_numeric_frame(df: Any, pd: Any, np: Any) -> tuple[Any, list[str]]:
    """返回 (DataFrame 仅含 _V1_FEAT 列, 错误信息列表)。"""
    errs: list[str] = []
    for c in ("comment_proxy", "collect_proxy", "share_proxy", "published_at"):
        if c not in df.columns:
            errs.append(f"v1 需要列 {c!r}（请用 export_features_v0 重新导出）")
    if errs:
        return df, errs
    cmt = pd.to_numeric(df["comment_proxy"].replace("", np.nan), errors="coerce").fillna(0).clip(lower=0)
    col = pd.to_numeric(df["collect_proxy"].replace("", np.nan), errors="coerce").fillna(0).clip(lower=0)
    shr = pd.to_numeric(df["share_proxy"].replace("", np.nan), errors="coerce").fillna(0).clip(lower=0)
    out = pd.DataFrame(
        {
            "title_len": df["title_len"].astype(float),
            "body_len": df["body_len"].astype(float),
            "log1p_like": df["log1p_like"].astype(float),
            "log1p_comment": np.log1p(cmt.astype(float)),
            "log1p_collect": np.log1p(col.astype(float)),
            "log1p_share": np.log1p(shr.astype(float)),
        }
    )
    ref_dates: list[datetime] = []
    for v in df["published_at"].astype(str).tolist():
        dt = _utc_from_published_cell(v)
        if dt is not None:
            ref_dates.append(dt)
    ref = max(ref_dates) if ref_dates else datetime.now(timezone.utc)
    ages: list[float] = []
    for v in df["published_at"].astype(str).tolist():
        dt = _utc_from_published_cell(v)
        if dt is None:
            ages.append(float("nan"))
        else:
            ages.append(max(0.0, (ref - dt).total_seconds() / 86400.0))
    age_arr = np.array(ages, dtype=float)
    med = float(np.nanmedian(age_arr)) if np.any(~np.isnan(age_arr)) else 0.0
    age_filled = np.where(np.isnan(age_arr), med, age_arr)
    out["age_days"] = age_filled
    return out, []


def build_design_matrix_with_frame(
    fp: Path,
    *,
    feature_schema: str,
    allow_mixed_batch: bool,
    target_column: str = "y_rule",
) -> tuple[Any, Any, list[str], dict[str, Any], Any]:
    """
    从 features CSV 构建 (X, y, feature_names, provenance, df_aligned)。
    df_aligned 为对齐后的 DataFrame（含 published_at 等），供时间外划分等使用。
    """
    import numpy as np
    import pandas as pd

    if not fp.is_file():
        raise ValueError(f"找不到特征文件: {fp}")
    df = pd.read_csv(fp, encoding="utf-8")
    prov = _features_provenance_report(df)
    if prov["batch_id_conflict"] or prov["feed_digest_sha256_conflict"]:
        if not allow_mixed_batch:
            raise ValueError(
                "特征表含多个 batch_id 或多个 feed_digest_sha256，已拒绝（避免无意混批）。"
                "若确有需要请 allow_mixed_batch=True。"
                f" unique_batch_ids={prov['unique_batch_ids']} "
                f"unique_feed_digest_sha256={prov['unique_feed_digest_sha256']}"
            )
    tc = str(target_column).strip()
    if tc not in df.columns:
        raise ValueError(
            f"CSV 缺标签列 {tc!r}；主标签为 y_rule，次标签为 y_rule_alt（须 export_features + labels_spec）"
        )
    if df[tc].isna().all():
        raise ValueError(
            f"{tc} 全空：请用 export_features_v0.py --labels-spec 导出（含 viral_like_threshold 等）"
        )
    df = df.dropna(subset=[tc]).copy()
    df[tc] = df[tc].astype(int)
    fs = str(feature_schema).strip().lower()
    if fs == "v1":
        v1_df, verr = _build_v1_numeric_frame(df, pd, np)
        if verr:
            raise ValueError("; ".join(verr))
        feats = list(_V1_FEAT)
        X = v1_df[list(_V1_FEAT)].values.astype(float)
    elif fs == "v0":
        feats = list(_V0_FEAT)
        for c in feats:
            if c not in df.columns:
                raise ValueError(f"缺列: {c}")
        X = df[feats].values.astype(float)
    else:
        raise ValueError(f"unknown feature_schema: {feature_schema!r} (use v0 or v1)")
    y = df[tc].values.astype(int)
    if len(np.unique(y)) < 2:
        raise ValueError(f"{tc} 只有一个类别，无法构建设计矩阵")
    df = df.reset_index(drop=True)
    return X, y, feats, prov, df


def build_design_matrix(
    fp: Path,
    *,
    feature_schema: str,
    allow_mixed_batch: bool,
    target_column: str = "y_rule",
) -> tuple[Any, Any, list[str], dict[str, Any]]:
    """
    从 features CSV 构建 (X, y, feature_names, provenance)。
    与 main() 内逻辑一致，供 evaluate_baseline_weights 等复用。
    """
    X, y, feats, prov, _df = build_design_matrix_with_frame(
        fp,
        feature_schema=feature_schema,
        allow_mixed_batch=allow_mixed_batch,
        target_column=target_column,
    )
    return X, y, feats, prov


def main() -> int:
    try:
        import numpy as np
        import pandas as pd
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import brier_score_loss, roc_auc_score
        from sklearn.model_selection import StratifiedKFold, train_test_split
    except ImportError:
        print("请先安装: pip install -r research/requirements-research.txt", flush=True)
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--features", type=str, default="research/features_v0.csv")
    ap.add_argument("--out", type=str, default="research/artifacts/baseline_v0.json")
    ap.add_argument(
        "--labels-spec",
        type=str,
        default="",
        help="可选；若提供则必须存在，其 JSON 对象原样写入产物的 labels_spec 字段（与 export_features_v0 契约对齐）",
    )
    ap.add_argument("--test-size", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--allow-mixed-batch",
        action="store_true",
        help="允许 CSV 内出现多个非空 batch_id 或多个 feed_digest_sha256（默认禁止，避免无意混批训练）",
    )
    ap.add_argument(
        "--cv-folds",
        type=int,
        default=0,
        help=">0 时做分层 K 折交叉验证并写入 cross_validation；0 表示不做（默认）。有效折数不超过少数类条数。",
    )
    ap.add_argument(
        "--feature-schema",
        choices=("v0", "v1"),
        default="v0",
        help="v0为三特征；v1 增加 log1p(comment/collect/share) 与 age_days（须 CSV 含对应列）",
    )
    ap.add_argument(
        "--target-column",
        type=str,
        default="y_rule",
        help="标签列：y_rule（主）或 y_rule_alt（次，须 export 时 labels_spec 含 viral_like_threshold_alt）",
    )
    args = ap.parse_args()

    fp = Path(args.features).resolve()
    if not fp.is_file():
        print(f"找不到 {fp}", flush=True)
        return 2

    labels_spec: dict[str, Any] | None = None
    labels_spec_path_str: str | None = None
    if (args.labels_spec or "").strip():
        spec_path = Path(args.labels_spec).expanduser().resolve()
        if not spec_path.is_file():
            print(f"找不到 --labels-spec文件: {spec_path}", flush=True)
            return 2
        try:
            labels_spec = _load_labels_spec_dict(spec_path)
        except ValueError as e:
            print(str(e), flush=True)
            return 2
        labels_spec_path_str = str(spec_path)

    schema_tag = (
        "feature_schema_v1"
        if str(args.feature_schema).strip().lower() == "v1"
        else "feature_schema_v0"
    )
    tc = str(args.target_column).strip()
    if tc not in ("y_rule", "y_rule_alt"):
        print("--target-column 须为 y_rule 或 y_rule_alt", flush=True)
        return 2
    try:
        X, y, feats, prov = build_design_matrix(
            fp,
            feature_schema=str(args.feature_schema).strip().lower(),
            allow_mixed_batch=bool(args.allow_mixed_batch),
            target_column=tc,
        )
    except ValueError as e:
        print(str(e), flush=True)
        return 2
    if prov["batch_id_conflict"] or prov["feed_digest_sha256_conflict"]:
        print(
            f"警告：混批训练（你已允许）。"
            f" unique_batch_ids={prov['unique_batch_ids']} "
            f"unique_feed_digest_sha256={prov['unique_feed_digest_sha256']}",
            flush=True,
        )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )
    clf = LogisticRegression(max_iter=200, random_state=args.seed)
    clf.fit(X_train, y_train)
    proba = clf.predict_proba(X_test)[:, 1]
    auc = float(roc_auc_score(y_test, proba))
    brier_hold = float(brier_score_loss(y_test, proba))

    cv_block: dict[str, Any] | None = None
    if int(args.cv_folds) > 0:
        min_class = int(pd.Series(y).value_counts().min())
        k_req = int(args.cv_folds)
        # 分层 K 折：折数不能超过少数类条数（否则无法每折分层）
        k_eff = min(k_req, min_class)
        if k_eff < 2:
            cv_block = {
                "skipped": True,
                "reason": "minority_class_too_small_for_cv",
                "minority_count": min_class,
                "n_folds_requested": k_req,
            }
            print(
                f"CV跳过：少数类仅 {min_class} 条，无法满足折数≥2（请求 {k_req}）",
                flush=True,
            )
        else:
            aucs: list[float] = []
            brs: list[float] = []
            skf = StratifiedKFold(n_splits=k_eff, shuffle=True, random_state=args.seed)
            for tr, va in skf.split(X, y):
                m = LogisticRegression(max_iter=200, random_state=args.seed)
                m.fit(X[tr], y[tr])
                pv = m.predict_proba(X[va])[:, 1]
                aucs.append(float(roc_auc_score(y[va], pv)))
                brs.append(float(brier_score_loss(y[va], pv)))
            cv_block = {
                "skipped": False,
                "n_folds_requested": k_req,
                "n_folds_effective": k_eff,
                "n_folds_capped_by_minority_class": k_eff < k_req,
                "minority_class_count": min_class,
                "stratified": True,
                "shuffle": True,
                "random_seed": int(args.seed),
                "roc_auc_mean": float(np.mean(aucs)),
                "roc_auc_std": float(np.std(aucs, ddof=1)) if len(aucs) > 1 else 0.0,
                "brier_mean": float(np.mean(brs)),
                "brier_std": float(np.std(brs, ddof=1)) if len(brs) > 1 else 0.0,
            }
            print(
                f"CV({k_eff}-fold): ROC-AUC mean={cv_block['roc_auc_mean']:.4f} std={cv_block['roc_auc_std']:.4f} | "
                f"Brier mean={cv_block['brier_mean']:.4f} std={cv_block['brier_std']:.4f}",
                flush=True,
            )

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": schema_tag,
        "feature_names": feats,
        "intercept": float(clf.intercept_[0]),
        "coefficients": {feats[i]: float(clf.coef_[0][i]) for i in range(len(feats))},
        "n_samples": int(len(y)),
        "n_train": int(len(y_train)),
        "n_test": int(len(y_test)),
        "holdout_roc_auc": auc,
        "holdout_brier_score": brier_hold,
        "train_test_split": {
            "test_size": float(args.test_size),
            "random_seed": int(args.seed),
            "stratify": True,
        },
        "input_features_path": str(fp),
        "input_features_sha256": _sha256_file(fp),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": f"操作化标签列 {tc}；不得解释为平台真实爆文概率；外推需重新校准"
        + ("；v1 含互动与稿龄特征，线上缺参时用环境变量/params 默认" if schema_tag == "feature_schema_v1" else ""),
        "target_column": tc,
        "features_provenance": prov,
    }
    if labels_spec is not None:
        payload["labels_spec_path"] = labels_spec_path_str
        payload["labels_spec"] = labels_spec
    if cv_block is not None:
        payload["cross_validation"] = cv_block
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    print(f"Hold-out ROC-AUC: {auc:.4f}  Brier: {brier_hold:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
