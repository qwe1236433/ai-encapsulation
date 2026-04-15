"""
在 features_v0.csv 上拟合简单基线（逻辑回归），产出可审计的系数 JSON。

要求 CSV 含数值列 title_len, body_len, log1p_like 且 y_rule 为 0/1（先带 --viral-threshold 导出）。

用法（仓库根目录）:

  pip install -r research/requirements-research.txt
  python research/train_baseline_v0.py --features research/features_v0.csv --out research/artifacts/baseline_v0.json

可选：写入与特征导出一致的标签契约（仅原样嵌入 JSON 对象，不编造字段）:

  python research/train_baseline_v0.py --features ... --out ... --labels-spec research/labels_spec.json

输出 JSON 含特征名、系数、截距、样本量、hold-out **ROC-AUC** 与 **Brier**；可选 **`--cv-folds K`** 分层交叉验证的 AUC/Brier 均值与标准差（小样本时有效折数自动下调）。无 sklearn 时退出并提示安装。
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

    df = pd.read_csv(fp, encoding="utf-8")
    prov = _features_provenance_report(df)
    if prov["batch_id_conflict"] or prov["feed_digest_sha256_conflict"]:
        detail = (
            f"unique_batch_ids={prov['unique_batch_ids']} "
            f"unique_feed_digest_sha256={prov['unique_feed_digest_sha256']}"
        )
        if not args.allow_mixed_batch:
            print(
                "特征表含多个 batch_id 或多个 feed_digest_sha256，已拒绝训练（避免无意混批）。"
                "若确有需要请加 --allow-mixed-batch。"
                f" {detail}",
                flush=True,
            )
            return 2
        print(f"警告：混批训练（你已允许）。{detail}", flush=True)

    if "y_rule" not in df.columns or df["y_rule"].isna().all():
        print(
            "y_rule 全空：请用 export_features_v0.py --viral-threshold 或 --labels-spec 重新导出特征",
            flush=True,
        )
        return 2
    df = df.dropna(subset=["y_rule"])
    df["y_rule"] = df["y_rule"].astype(int)
    feats = ["title_len", "body_len", "log1p_like"]
    for c in feats:
        if c not in df.columns:
            print(f"缺列: {c}", flush=True)
            return 2

    X = df[feats].values.astype(float)
    y = df["y_rule"].values
    if len(np.unique(y)) < 2:
        print("y_rule 只有一个类别，无法训练分类器", flush=True)
        return 2

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
        "schema": "feature_schema_v0",
        "feature_names": feats,
        "intercept": float(clf.intercept_[0]),
        "coefficients": {feats[i]: float(clf.coef_[0][i]) for i in range(len(feats))},
        "n_samples": int(len(df)),
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
        "note": "操作化标签 y_rule；不得解释为平台真实爆文概率；外推需重新校准",
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
