"""
A↔D 短路径 smoke probe —— 验证"Hermes 写 CLI 文件 → 窗 A 检测 SHA 变化 → 重启爬虫带新 --keywords"链路。

本 probe 分两层：
  [静态部分] 默认必跑，零副作用。只做纯文件/字符串/SHA 层面的兼容性检查：
    P1  `_bridge_pool_to_cli` 写入的文件格式（逗号分隔 UTF-8 无 BOM）与窗 A
        `Read-KeywordsLine` 所期望的格式一致。
    P2  Python 的 SHA256 与 PowerShell `Get-FileHash -Algorithm SHA256` 输出一致
        （证明两边"比 SHA 判断是否变了"的算法对齐，不会漂移）。
    P3  窗 A 脚本本身的关键代码特征仍在（防误改破坏 smoke 前提）。
    P4  若 `logs/mediacrawler-watch.log` 存在则报告其最新行；否则提示"窗 A 未启动"。

  [动态部分] `--live` 开启。**有副作用**：
    1. 备份当前 `research/keyword_candidates_for_cli.txt`
    2. 往末尾**追加一个唯一探针关键词** `hermes_probe_ab_<uuid8>`
    3. 监视 `logs/mediacrawler-watch.log` tail 最多 `--live-timeout-sec` 秒
    4. 成功条件（两个锚点任一出现在注入后新增日志里即可）：
       a) `Keywords or MediaCrawler config changed (sig prefix XXX...). Restarting crawler.`
          ←窗 A 检测到 SHA 变化并主动 taskkill 子进程的最精准信号
       b) `Starting MediaCrawler with --keywords (len=N)` 且 len 相对注入前变化
          ←新子进程已用新 keyword 列启动（新 len 反映探针 token 加长）
    5. **无论成功/失败都立即还原** CLI 文件到备份内容（窗 A 会再一次检测到
       SHA 变化 → 再启一次，这是预期代价）

安全说明（`--live`）:
  - 会真的让窗 A taskkill 一次 MediaCrawler 然后带新关键词重启 2 次（探针注入 1 次 +
    还原 1 次）。**请确保你接受爬虫在此期间的短暂抖动**。
  - 探针 keyword 形如 `hermes_probe_ab_abc12345`，极少可能命中真笔记；即便短暂被
    MediaCrawler 用去查询，拿到的也是空结果，不污染 features 表。

用法:
    python scripts/_probe_ab_loop.py              # 只跑静态
    python scripts/_probe_ab_loop.py --live       # 跑静态 + 动态（需窗 A 在跑）
    python scripts/_probe_ab_loop.py --live --live-timeout-sec 90
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CLI_KEYWORDS_FILE = REPO_ROOT / "research" / "keyword_candidates_for_cli.txt"
WATCH_LOG_FILE = REPO_ROOT / "logs" / "mediacrawler-watch.log"
WATCH_SCRIPT = REPO_ROOT / "scripts" / "run-mediacrawler-xhs-keywords-watch.ps1"


@dataclass
class ProbeStep:
    name: str
    ok: bool
    detail: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file_py(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_file_pwsh(path: Path) -> str:
    """用 PowerShell Get-FileHash 算 SHA256。"""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "(Get-FileHash -Algorithm SHA256 -LiteralPath "
            + f"'{path}' ).Hash",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip().lower()


# -------------------- 静态检查 --------------------

def check_bridge_format_compat() -> ProbeStep:
    """P1：bridge 写的格式 ↔ 窗 A Read-KeywordsLine 读的格式。

    具体：把一组关键词通过 bridge 的规范路径走一遍（调 tick 的 _bridge_pool_to_cli
    对一个临时文件写入），读回内容，断言它符合窗 A 的契约：
      - 单行（可能有末尾换行），逗号分隔
      - UTF-8 无 BOM
      - 每个 token trim 后非空，无内部逗号
    """
    from tempfile import TemporaryDirectory

    import scripts.hermes_closed_loop_tick as tick_mod  # noqa: E402

    with TemporaryDirectory(prefix="probe_ab_") as td:
        tmp_cli = Path(td) / "cli.txt"
        saved_path = tick_mod.CLI_KEYWORDS_FILE
        tick_mod.CLI_KEYWORDS_FILE = tmp_cli
        try:
            wrote, note = tick_mod._bridge_pool_to_cli(  # type: ignore[attr-defined]
                ["测试词A", "测试词B", "keyword_with_underscore"]
            )
            if not wrote:
                return ProbeStep("P1_bridge_format", False, f"bridge 第一次写失败: {note}")
            raw = tmp_cli.read_bytes()
            text = tmp_cli.read_text(encoding="utf-8-sig")
            line = text.rstrip("\n\r")
            parts = [p.strip() for p in line.split(",")]
            parts = [p for p in parts if p]
            problems: list[str] = []
            if raw[:3] == b"\xef\xbb\xbf":
                problems.append("含 BOM（窗 A 读的是 utf-8 无 BOM）")
            if "\n" in line or "\r" in line:
                problems.append("首行内含换行（窗 A 只读第一行）")
            if len(parts) != 3:
                problems.append(f"分隔后条数 {len(parts)}，期望 3")
            if problems:
                return ProbeStep("P1_bridge_format", False, "; ".join(problems))
            return ProbeStep("P1_bridge_format", True, f"ok, tokens={parts}")
        finally:
            tick_mod.CLI_KEYWORDS_FILE = saved_path


def check_sha256_algo_agreement() -> ProbeStep:
    """P2：Python 的 SHA256 与 PowerShell 的 Get-FileHash -SHA256 一致。"""
    from tempfile import NamedTemporaryFile

    payload = f"alpha,beta,gamma,probe_{uuid.uuid4().hex[:8]}".encode("utf-8")
    tmp = NamedTemporaryFile(delete=False, suffix=".txt")
    try:
        tmp.write(payload)
        tmp.close()
        path = Path(tmp.name)
        py_hash = _sha256_file_py(path)
        ps_hash = _sha256_file_pwsh(path)
        if not ps_hash:
            return ProbeStep("P2_sha256_agreement", False, "PowerShell Get-FileHash 无输出，可能环境不支持")
        if py_hash != ps_hash:
            return ProbeStep(
                "P2_sha256_agreement", False,
                f"不一致! python={py_hash} pwsh={ps_hash}"
            )
        return ProbeStep("P2_sha256_agreement", True, f"ok, sha={py_hash[:16]}...")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def check_window_a_script_intact() -> ProbeStep:
    """P3：窗 A 脚本存在且含关键代码特征。"""
    if not WATCH_SCRIPT.is_file():
        return ProbeStep("P3_watch_script", False, f"脚本缺失: {WATCH_SCRIPT}")
    src = WATCH_SCRIPT.read_text(encoding="utf-8", errors="replace")
    checks = {
        "keywords_file_param": "$KeywordsFile" in src,
        "debounce_param": "$DebounceSeconds" in src,
        "sha_signature": "Get-WatchSignature" in src,
        "pass_keywords_to_py": '--keywords' in src,
        "taskkill_fence": "taskkill.exe" in src,
        "restart_log_marker": "Starting MediaCrawler with --keywords" in src,
    }
    missing = [k for k, v in checks.items() if not v]
    if missing:
        return ProbeStep("P3_watch_script", False, f"关键特征缺失: {missing}")
    return ProbeStep("P3_watch_script", True, f"ok, 全部 6 项特征存在 ({WATCH_SCRIPT.name})")


def check_watch_log_liveness(max_age_sec: int = 600) -> ProbeStep:
    """P4：窗 A 运行指标 —— mediacrawler-watch.log 最新行的 mtime 是否在近 max_age_sec 秒内。"""
    if not WATCH_LOG_FILE.is_file():
        return ProbeStep(
            "P4_watch_log_live", False,
            f"不存在: {WATCH_LOG_FILE.relative_to(REPO_ROOT)}（窗 A 从未启动过，或刚启动没写出日志）"
        )
    mtime = WATCH_LOG_FILE.stat().st_mtime
    age = time.time() - mtime
    tail = ""
    try:
        lines = WATCH_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines:
            tail = lines[-1]
    except Exception as e:  # noqa: BLE001
        tail = f"(读取尾行失败: {e})"
    if age > max_age_sec:
        return ProbeStep(
            "P4_watch_log_live", False,
            f"窗 A 日志已 {int(age)}s 未更新（> {max_age_sec}s），窗 A 可能没开 / 已退出。tail: {tail[:120]}"
        )
    return ProbeStep(
        "P4_watch_log_live", True,
        f"ok, 日志 {int(age)}s 前更新过 | tail: {tail[:160]}"
    )


# -------------------- 动态检查（--live） --------------------

def run_live_roundtrip(timeout_sec: int = 120) -> ProbeStep:
    """动态：向真实 CLI 文件注入探针关键词，等窗 A 重启事件；总是还原。"""
    if not CLI_KEYWORDS_FILE.is_file():
        return ProbeStep("D1_live_roundtrip", False, f"CLI 文件不存在: {CLI_KEYWORDS_FILE}")
    if not WATCH_LOG_FILE.is_file():
        return ProbeStep("D1_live_roundtrip", False, "watch 日志不存在，窗 A 未启动")

    original_bytes = CLI_KEYWORDS_FILE.read_bytes()
    original_text = original_bytes.decode("utf-8-sig").rstrip("\r\n")

    probe_token = f"hermes_probe_ab_{uuid.uuid4().hex[:8]}"
    new_text = original_text + ("," if original_text else "") + probe_token

    try:
        initial_log_size = WATCH_LOG_FILE.stat().st_size

        CLI_KEYWORDS_FILE.write_text(new_text, encoding="utf-8")
        injected_at = time.time()

        saw_change_detected = False
        saw_start_with_new_len = False
        first_match_line = ""
        change_line = ""
        start_line = ""
        observed_new_len = -1
        elapsed = 0.0
        pattern_change = re.compile(r"Keywords or MediaCrawler config changed")
        pattern_start_len = re.compile(r"Starting MediaCrawler with --keywords \(len=(\d+)\)")

        baseline_len = -1
        try:
            with WATCH_LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = pattern_start_len.search(line)
                    if m:
                        try:
                            baseline_len = int(m.group(1))
                        except ValueError:
                            pass
        except Exception:
            pass

        while elapsed < timeout_sec:
            time.sleep(2.0)
            elapsed = time.time() - injected_at
            try:
                with WATCH_LOG_FILE.open("rb") as f:
                    f.seek(initial_log_size)
                    new_chunk = f.read()
                text_chunk = new_chunk.decode("utf-8", errors="replace")
                for line in text_chunk.splitlines():
                    if pattern_change.search(line):
                        saw_change_detected = True
                        if not change_line:
                            change_line = line
                    m = pattern_start_len.search(line)
                    if m:
                        try:
                            observed_len = int(m.group(1))
                            if baseline_len < 0 or observed_len != baseline_len:
                                saw_start_with_new_len = True
                                observed_new_len = observed_len
                                if not start_line:
                                    start_line = line
                        except ValueError:
                            pass
                if saw_change_detected and saw_start_with_new_len:
                    break
            except Exception as e:  # noqa: BLE001
                return ProbeStep("D1_live_roundtrip", False, f"读 watch log 增量失败: {e}")

        first_match_line = start_line or change_line

        if saw_change_detected and saw_start_with_new_len:
            return ProbeStep(
                "D1_live_roundtrip", True,
                f"ok, 探针 token='{probe_token}' 注入 {int(elapsed)}s 内观察到 "
                f"SHA 变化 + 新 keywords 启动 (baseline_len={baseline_len} -> new_len={observed_new_len}): "
                f"{first_match_line.strip()[:220]}"
            )
        if saw_change_detected:
            return ProbeStep(
                "D1_live_roundtrip", False,
                f"部分成功：{int(elapsed)}s 内窗 A 检测到 SHA 变化并重启（{change_line.strip()[:160]}），"
                f"但未在超时内看到新 'Starting MediaCrawler with --keywords (len!={baseline_len})'。"
                f"token={probe_token}"
            )
        return ProbeStep(
            "D1_live_roundtrip", False,
            f"超时 {timeout_sec}s 仍未看到 'Keywords or MediaCrawler config changed'；"
            f"窗 A 可能卡在 quickExit backoff、debounce 未过、或 give-up 停止。"
            f"token={probe_token}"
        )
    finally:
        try:
            CLI_KEYWORDS_FILE.write_bytes(original_bytes)
            print(f"[restore] 已把 {CLI_KEYWORDS_FILE.relative_to(REPO_ROOT)} 还原到探针前状态", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[restore] !! 还原失败，请手动恢复 CLI 文件: {e}", flush=True)


# -------------------- 顶层调度 --------------------

def _print_step(step: ProbeStep) -> None:
    mark = "PASS" if step.ok else "FAIL"
    print(f"  [{mark}] {step.name}: {step.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A↔D 短路径 smoke probe")
    parser.add_argument("--live", action="store_true", help="启用动态检查（需窗 A 在跑；有副作用）")
    parser.add_argument("--live-timeout-sec", type=int, default=120, help="动态检查等待窗 A 重启事件的最大秒数")
    parser.add_argument("--allow-fail-p4", action="store_true", help="P4（窗 A liveness）失败不退出非 0；默认静态总分计入 P4")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    print(f"=== A↔D smoke probe @ {_now_iso()} ===")
    print(f"  REPO_ROOT={REPO_ROOT}")
    print(f"  CLI_FILE ={CLI_KEYWORDS_FILE.relative_to(REPO_ROOT)}")
    print(f"  WATCH_LOG={WATCH_LOG_FILE.relative_to(REPO_ROOT)}")
    print()

    print("[static]")
    static_steps: list[ProbeStep] = [
        check_bridge_format_compat(),
        check_sha256_algo_agreement(),
        check_window_a_script_intact(),
        check_watch_log_liveness(),
    ]
    for s in static_steps:
        _print_step(s)

    static_ok_required = all(
        s.ok for s in static_steps if s.name != "P4_watch_log_live"
    )
    p4_ok = static_steps[-1].ok

    print()
    if not static_ok_required:
        print("=== static 必过项失败，停在这里 ===")
        return 2

    if not args.live:
        if p4_ok:
            print("=== static ALL OK（窗 A liveness 也 ok）===")
            print("提示：加 --live 跑动态注入探针关键词的完整链路测试。")
            return 0
        print("=== static 必过项 PASS；P4 liveness FAIL（窗 A 未启动）===")
        return 0 if args.allow_fail_p4 else 3

    if not p4_ok:
        print("=== --live 要求窗 A 在跑，但 P4 liveness FAIL ===")
        print("请先启动窗 A：")
        print("  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\run-mediacrawler-xhs-keywords-watch.ps1 -McRoot D:\\MediaCrawler")
        return 4

    print("[live]")
    print("  即将向真实 CLI 文件注入探针关键词并观察窗 A 响应...")
    print(f"  将等待最多 {args.live_timeout_sec}s")
    print()
    live_step = run_live_roundtrip(timeout_sec=args.live_timeout_sec)
    _print_step(live_step)
    print()
    if live_step.ok:
        print("=== ALL OK (static + live) ===")
        return 0
    print("=== static OK, live FAIL ===")
    return 5


if __name__ == "__main__":
    sys.exit(main())
