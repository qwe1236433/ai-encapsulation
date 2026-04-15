"""
在 features_v0.csv 上拟合简单基线（逻辑回归），产出可审计的系数 JSON。

要求 CSV 含数值列 title_len, body_len, log1p_like 且 y_rule 为 0/1（先带 --viral-threshold 导出）。

用法（仓库根目录）:

  pip install -r research/requirements-research.txt
  python research/train_baseline_v0.py --features research/features_v0.csv --out research/artifacts/baseline_v0.json

输出 JSON 含特征名、系数、截距、样本量、测试集 AUC（简单 hold-out）；无 sklearn 时退出并提示安装。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    try:
        import numpy as np
        import pandas as pd
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("请先安装: pip install -r research/requirements-research.txt", flush=True)
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--features", type=str, default="research/features_v0.csv")
    ap.add_argument("--out", type=str, default="research/artifacts/baseline_v0.json")
    ap.add_argument("--test-size", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    fp = Path(args.features).resolve()
    if not fp.is_file():
        print(f"找不到 {fp}", flush=True)
        return 2

    df = pd.read_csv(fp, encoding="utf-8")
    if "y_rule" not in df.columns or df["y_rule"].isna().all():
        print("y_rule 全空：请用 export_features_v0.py --viral-threshold 重新导出", flush=True)
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

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "feature_schema_v0",
        "feature_names": feats,
        "intercept": float(clf.intercept_[0]),
        "coefficients": {feats[i]: float(clf.coef_[0][i]) for i in range(len(feats))},
        "n_samples": int(len(df)),
        "n_train": int(len(y_train)),
        "n_test": int(len(y_test)),
        "holdout_roc_auc": auc,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "操作化标签 y_rule；不得解释为平台真实爆文概率；外推需重新校准",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    print(f"Hold-out ROC-AUC: {auc:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
