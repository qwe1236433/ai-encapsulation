"""
本地一键冒烟（不启动 Docker）：特征与契约校验 + OpenClaw baseline 预测（若文件存在）。

仓库根目录:

  python scripts/smoke_research.py

退出码：任一步失败则非 0。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    py = sys.executable
    feats = REPO / "research" / "features_v0.csv"
    spec = REPO / "research" / "labels_spec.json"
    verify = REPO / "scripts" / "verify_features_labels_spec.py"
    if feats.is_file() and spec.is_file():
        print("[smoke] verify_features_labels_spec …", flush=True)
        r = subprocess.run(
            [py, str(verify), "--features", str(feats), "--labels-spec", str(spec)],
            cwd=str(REPO),
        )
        if r.returncode != 0:
            return r.returncode
    else:
        print("[smoke] skip verify (missing research/features_v0.csv or labels_spec.json)", flush=True)

    art = REPO / "research" / "artifacts" / "baseline_v0.json"
    if not art.is_file():
        print("[smoke] skip predict (missing research/artifacts/baseline_v0.json)", flush=True)
        return 0

    oc = REPO / "openclaw"
    code = (
        "import xhs_factory as xf; "
        "r=xf.predict_viral_score("
        "'测\\n试'*20, {'viral_sop':'对照式','core_hook':'x','target_emotion':'共鸣'}, "
        "None, like_proxy_hint=100); "
        "assert 'baseline_lr' in r.get('score_breakdown',{}); "
        "print('[smoke] predict_viral_score baseline_lr ok', r['predicted_score'])"
    )
    env = os.environ.copy()
    env["XHS_FACTORY_BASELINE_JSON"] = str(art)
    env["XHS_FACTORY_BASELINE_MODE"] = "blend"
    print("[smoke] openclaw predict_viral_score …", flush=True)
    r = subprocess.run([py, "-c", code], cwd=str(oc), env=env)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
