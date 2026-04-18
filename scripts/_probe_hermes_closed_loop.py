"""闭环冒烟探针 —— **全部走真实管道**。

三类断言：
  1. [precheck]  v2 数据真就绪（features_v2.csv + baseline_v2.json）
  2. [bridge]    _bridge_pool_to_cli 对"空/新/同/变"四种情形行为正确
                （这条只是纯函数边界测试，不调 LLM，不算虚拟数据）
  3. [e2e_real]  真调一次 LLM（MiniMax），端到端跑 hermes_closed_loop_tick.main：
                 - features/baseline/keyword_pool 读真实生产文件
                 - --skip-bridge：不污染 research/keyword_candidates_for_cli.txt
                 - --force：绕过 "sha 没变就跳过" 的去重
                 - LOG_DIR/TICK_LOG/STATE_FILE 重定向到 tempdir，不污染 logs/
                 - 不预设 verdict（NO_PROPOSAL / APPROVED_AND_COMMITTED / REJECTED 都合法）
                 - 若 MINIMAX_API_KEY 缺失就直接报错退出（用户明确要求：不走虚拟数据）

跑法（仓库根）:
  python scripts/_probe_hermes_closed_loop.py

可选关闭真实 LLM 这轮（CI 调试或没配 key 时，**不推荐**，仅供临时）:
  $env:HERMES_PROBE_SKIP_LLM="1"; python scripts/_probe_hermes_closed_loop.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import hermes_closed_loop_tick as m  # noqa: E402


def probe_precheck() -> None:
    ok, reason, sha = m._precheck_ready()
    print(f"[precheck] ok={ok} reason={reason} sha_short={sha[:16] if sha else ''}")
    assert ok, f"precheck failed (先跑窗 C 产 baseline_v2.json)：{reason}"


def probe_bridge() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_cli = Path(td) / "cli.txt"
        orig = m.CLI_KEYWORDS_FILE
        try:
            m.CLI_KEYWORDS_FILE = tmp_cli
            wrote, note = m._bridge_pool_to_cli(["减脂餐", "低卡食谱", "高蛋白"])
            print(f"[bridge] first write: wrote={wrote} note={note}")
            assert wrote, "首次写入应该返回 True"
            assert tmp_cli.read_text(encoding="utf-8").strip() == "减脂餐,低卡食谱,高蛋白"

            wrote2, note2 = m._bridge_pool_to_cli(["减脂餐", "低卡食谱", "高蛋白"])
            print(f"[bridge] same content: wrote={wrote2} note={note2}")
            assert not wrote2, "内容一致时不应该重写"

            wrote3, note3 = m._bridge_pool_to_cli(["减脂餐", "低卡食谱", "高蛋白", "HIIT"])
            print(f"[bridge] add one kw: wrote={wrote3} note={note3}")
            assert wrote3, "池变化时应该覆盖"
            assert "HIIT" in tmp_cli.read_text(encoding="utf-8")

            wrote4, note4 = m._bridge_pool_to_cli([" "])
            print(f"[bridge] empty-after-normalize: wrote={wrote4} note={note4}")
            assert not wrote4, "空池不能写进去把窗 A 炸了"
        finally:
            m.CLI_KEYWORDS_FILE = orig


def probe_tick_entry_schema() -> None:
    expected = {"ts_utc", "reason", "force_kind", "force", "skip_bridge", "precheck"}
    parser = m._build_parser()
    ns = parser.parse_args([])
    assert ns.reason == "closed_loop_tick"
    assert ns.max_rounds == 3
    assert ns.force is False
    assert ns.skip_bridge is False
    print(f"[schema] parser defaults ok; tick 必写字段 = {sorted(expected)}")


def probe_e2e_real_llm() -> None:
    """真调 MiniMax，真读 features_v2.csv / baseline_v2.json / keyword_pool_active.json。

    **副作用范围**（诚实标注，不是"零副作用"）：
      隔离到 tempdir：
        - LOG_DIR / TICK_LOG / STATE_FILE（不污染 logs/hermes-closed-loop*.log）
        - --skip-bridge 保证不覆盖 research/keyword_candidates_for_cli.txt
      **仍会写入真实仓库**（这正是"真实管道"含义，不是 bug）：
        - research/artifacts/cycle_log.jsonl：本次 cycle 审计留痕（必追加）
        - research/artifacts/approved_tunings.jsonl：若 LLM 通过且落盘，追加 1 条
        - research/artifacts/keyword_pool_active.json：若通过 keyword_pool 提案，覆盖
        - research/artifacts/rejected_tunings.jsonl：若拒绝，追加
      这些是 Hermes cycle 自己的行为，不归 tick 管，也不能也不该 mock。

    LLM 看到的 = 当前**真实爬虫快照**（features_v2、真池、真基线）。
    """
    if os.environ.get("HERMES_PROBE_SKIP_LLM", "").strip() in {"1", "true", "TRUE"}:
        print("[e2e_real] SKIPPED by HERMES_PROBE_SKIP_LLM=1 (仅当临时调试用，生产必须关)")
        return

    from hermes._minimax import read_minimax_key

    if not read_minimax_key():
        raise SystemExit(
            "[e2e_real] MINIMAX_API_KEY 未配（.env 或 环境变量）。\n"
            "  用户已明确"
            "不走虚拟数据——此 probe 不接受 mock 跳过。\n"
            "  先把 key 配上再跑；或者临时 $env:HERMES_PROBE_SKIP_LLM='1'（强烈不推荐）。"
        )

    with tempfile.TemporaryDirectory() as td:
        tmp_log_dir = Path(td) / "logs"
        tmp_log_dir.mkdir()

        orig_log_dir = m.LOG_DIR
        orig_tick_log = m.TICK_LOG
        orig_state = m.STATE_FILE

        m.LOG_DIR = tmp_log_dir
        m.TICK_LOG = tmp_log_dir / "hermes-closed-loop.log"
        m.STATE_FILE = tmp_log_dir / "hermes-closed-loop-state.json"

        try:
            rc = m.main(["--reason", "probe_real_llm", "--force", "--skip-bridge", "--quiet"])
            print(f"[e2e_real] main rc = {rc}")
            assert rc == 0, f"真 LLM tick 非 0 退出（{rc}），检查 .env/网络/配额"

            assert m.TICK_LOG.is_file(), "tick log 没写出来"
            log_line = m.TICK_LOG.read_text(encoding="utf-8").strip()
            log_obj = json.loads(log_line)

            outcome = log_obj.get("outcome")
            verdict = log_obj.get("verdict")
            cycle_id = log_obj.get("cycle_id")
            auc = log_obj.get("baseline_auc")
            n = log_obj.get("sample_count")
            print(
                f"[e2e_real] outcome={outcome} verdict={verdict} "
                f"cycle_id={cycle_id} AUC={auc} n={n}"
            )

            assert outcome == "ran", f"期望 outcome=ran，实际 {outcome}"
            assert cycle_id and isinstance(cycle_id, str) and len(cycle_id) >= 8
            assert isinstance(n, int) and n > 0, "sample_count 应是正整数（真特征行数）"
            assert isinstance(auc, (int, float)) and 0.0 <= auc <= 1.0, \
                f"baseline_auc={auc} 不在 [0,1]，数据管道出问题"
            assert verdict in {
                "APPROVED_AND_COMMITTED",
                "REJECTED_AND_LOGGED",
                "NO_PROPOSAL",
            }, f"未识别的 verdict: {verdict}"

            assert log_obj.get("bridged_to_cli_file") is False, \
                "probe 强制了 --skip-bridge，任何 True 都说明脚本 bypass 了开关"

            assert m.STATE_FILE.is_file(), "state file 没写出来"
            state = json.loads(m.STATE_FILE.read_text(encoding="utf-8"))
            assert state.get("last_verdict") == verdict
            assert state.get("last_bridged") is False
            print(f"[e2e_real] state ok: verdict={state.get('last_verdict')} "
                  f"sha={state.get('last_baseline_sha256','')[:12]}")
        finally:
            m.LOG_DIR = orig_log_dir
            m.TICK_LOG = orig_tick_log
            m.STATE_FILE = orig_state


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    probe_precheck()
    probe_bridge()
    probe_tick_entry_schema()
    probe_e2e_real_llm()
    print("\nALL OK")
