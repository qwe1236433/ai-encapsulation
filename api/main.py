"""
本地流程控制台 API：与 Hermes / OpenClaw / export / bench 串联。
仅绑定 127.0.0.1，勿暴露公网。

启动（仓库根目录）:
  python -m pip install -r api/requirements.txt
  python -m uvicorn api.main:app --host 127.0.0.1 --port 8099
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
WEB_DIR = REPO_ROOT / "web"
OUTPUT_RUNS = REPO_ROOT / "outputs" / "xhs-runs"

DEFAULT_HERMES = os.environ.get("FLOW_API_HERMES_URL", "http://127.0.0.1:8080/")
DEFAULT_OPENCLAW = os.environ.get("FLOW_API_OPENCLAW_URL", "http://127.0.0.1:3000/")
DEFAULT_MC_IN = os.environ.get("FLOW_API_MEDIACRAWLER_JSONL", r"D:\MediaCrawler\data\xhs\jsonl")
DEFAULT_FEED_OUT = os.environ.get(
    "FLOW_API_FEED_OUT",
    str(REPO_ROOT / "openclaw" / "data" / "xhs-feed" / "samples.json"),
)
DEFAULT_GOAL_REL = os.environ.get("FLOW_API_BENCH_GOAL", "scripts/bench-goal-example.txt")
RUNTIME_GOALS_DIR = REPO_ROOT / "outputs" / ".runtime-goals"
GOAL_TEXT_MAX_CHARS = 50_000

_EXPORT_DEDUPE_ALLOWED = frozenset({"none", "key", "content"})


def _resolve_export_dedupe(override: str | None) -> str:
    """
    合并脚本 --dedupe。override 非空时优先（API 查询/请求体）；
    否则读 FLOW_API_EXPORT_DEDUPE；非法环境值视为 none。
    """
    if override is not None:
        o = override.strip().lower()
        if o in _EXPORT_DEDUPE_ALLOWED:
            return o
        raise ValueError(f"dedupe must be one of {sorted(_EXPORT_DEDUPE_ALLOWED)}, got {override!r}")
    env_v = (os.environ.get("FLOW_API_EXPORT_DEDUPE") or "none").strip().lower()
    if env_v in _EXPORT_DEDUPE_ALLOWED:
        return env_v
    return "none"


def _export_to_feed_argv(
    mc: Path,
    out: Path,
    dedupe: str,
    digest_out: str = "",
    batch_id: str = "",
) -> list[str]:
    argv: list[str] = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "export_to_xhs_feed.py"),
        "--in",
        str(mc),
        "--out",
        str(out),
        "--dedupe",
        dedupe,
    ]
    if (digest_out or "").strip():
        argv.extend(["--digest-out", digest_out.strip()])
    if (batch_id or "").strip():
        argv.extend(["--batch-id", batch_id.strip()])
    return argv

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _job_update(jid: str, **kwargs: Any) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kwargs)


def _run_subprocess(job_id: str, argv: list[str], cwd: Path, timeout: int) -> None:
    _job_update(job_id, status="running", argv=" ".join(argv))
    try:
        p = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        _job_update(
            job_id,
            status="done",
            returncode=p.returncode,
            stdout=(p.stdout or "")[-16000:],
            stderr=(p.stderr or "")[-12000:],
        )
    except subprocess.TimeoutExpired:
        _job_update(job_id, status="error", error=f"timeout after {timeout}s")
    except Exception as e:
        _job_update(job_id, status="error", error=str(e))


def _pipeline_log(jid: str, msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return
        arr = j.setdefault("pipeline_logs", [])
        arr.append(line)
        if len(arr) > 400:
            del arr[:-400]


def _set_pipeline_stage(jid: str, stage: str, index: int) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["stage"] = stage
            _jobs[jid]["stage_index"] = index


def _run_full_pipeline(
    jid: str,
    goal_path: Path,
    max_attempts: str,
    skip_export: bool,
    merge_dedupe: str = "none",
) -> None:
    """顺序：合并 Feed → docker up → bench（单作业内分阶段）。"""
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["status"] = "running"
    mc = Path(DEFAULT_MC_IN)
    out = Path(DEFAULT_FEED_OUT)
    script = REPO_ROOT / "scripts" / "export_to_xhs_feed.py"

    if not skip_export:
        _set_pipeline_stage(jid, "merge", 1)
        _pipeline_log(jid, f"步骤 1/3：合并爬虫数据 → samples.json（dedupe={merge_dedupe}）…")
        if not mc.exists():
            _pipeline_log(jid, f"失败：找不到 {mc}。请先跑 MediaCrawler，或在本机 .env 里改 FLOW_API_MEDIACRAWLER_JSONL。")
            _job_update(jid, status="error", error="mediacrawler path missing")
            return
        if not script.is_file():
            _job_update(jid, status="error", error="export script missing")
            return
        digest_out = (os.environ.get("FLOW_API_FEED_DIGEST_OUT") or "").strip()
        batch_id = (os.environ.get("FLOW_API_FEED_BATCH_ID") or "").strip()
        argv = _export_to_feed_argv(mc, out, merge_dedupe, digest_out, batch_id)
        try:
            p = subprocess.run(
                argv,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            _job_update(jid, status="error", error=str(e))
            _pipeline_log(jid, f"合并异常：{e}")
            return
        if p.returncode != 0:
            _job_update(
                jid,
                status="error",
                error="merge failed",
                returncode=p.returncode,
                stderr=(p.stderr or "")[-8000:],
                stdout=(p.stdout or "")[-8000:],
            )
            _pipeline_log(jid, "合并失败（见下方技术日志）")
            return
        _pipeline_log(jid, "步骤 1 完成。")
    else:
        _pipeline_log(jid, "已跳过合并（使用现有 samples.json）")

    _set_pipeline_stage(jid, "docker", 2)
    _pipeline_log(jid, "步骤 2/3：docker compose up -d …")
    try:
        p2 = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        _job_update(jid, status="error", error=str(e))
        _pipeline_log(jid, f"Docker 异常：{e}")
        return
    if p2.returncode != 0:
        _job_update(
            jid,
            status="error",
            error="docker failed",
            returncode=p2.returncode,
            stderr=(p2.stderr or "")[-8000:],
            stdout=(p2.stdout or "")[-8000:],
        )
        _pipeline_log(jid, "Docker 失败。请确认本机已装 Docker Desktop 且在项目根能执行 docker compose。")
        return
    _pipeline_log(jid, "步骤 2 完成。")

    _set_pipeline_stage(jid, "bench", 3)
    _pipeline_log(jid, "步骤 3/3：生成文案（通常 1～3 分钟，请稍候）…")
    ps1 = REPO_ROOT / "bench-hermes-xhs-sync.ps1"
    if not ps1.is_file():
        _job_update(jid, status="error", error="bench script missing")
        return
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
        "-GoalPath",
        str(goal_path),
        "-MaxAttempts",
        max_attempts,
    ]
    try:
        p3 = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=720,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        _job_update(jid, status="error", error=str(e))
        _pipeline_log(jid, f"生成异常：{e}")
        return
    ok = p3.returncode == 0
    _job_update(
        jid,
        status="done" if ok else "error",
        returncode=p3.returncode,
        stdout=(p3.stdout or "")[-16000:],
        stderr=(p3.stderr or "")[-12000:],
    )
    if ok:
        _pipeline_log(jid, "全部完成。下方可查看最新导出正文。")
        _set_pipeline_stage(jid, "done", 4)
    else:
        _pipeline_log(jid, "生成未成功（见技术日志）。可确认 Hermes/OpenClaw 是否在线。")


def _resolve_goal_for_bench(job_id: str, goal_text: str | None, goal_path: str | None) -> Path:
    """解析为 UTF-8文件路径：优先使用本次输入的 goal 正文，否则使用仓库内 goal 文件。"""
    text = (goal_text or "").strip()
    if text:
        if len(text) > GOAL_TEXT_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"goal_text too long (max {GOAL_TEXT_MAX_CHARS} characters)",
            )
        RUNTIME_GOALS_DIR.mkdir(parents=True, exist_ok=True)
        out = (RUNTIME_GOALS_DIR / f"{job_id}.txt").resolve()
        repo_resolved = REPO_ROOT.resolve()
        if not str(out).startswith(str(repo_resolved)):
            raise HTTPException(status_code=500, detail="runtime goal path invalid")
        out.write_text(text, encoding="utf-8")
        return out
    rel = (goal_path or DEFAULT_GOAL_REL).strip().replace("/", os.sep)
    goal = (REPO_ROOT / rel).resolve()
    if not str(goal).startswith(str(REPO_ROOT.resolve())):
        raise HTTPException(status_code=400, detail="goal_path must stay under repo root")
    if not goal.is_file():
        raise HTTPException(status_code=400, detail=f"goal file not found: {goal}")
    return goal


def _powershell_bench(job_id: str, goal_path: Path, max_attempts: str) -> None:
    ps1 = REPO_ROOT / "bench-hermes-xhs-sync.ps1"
    if not ps1.is_file():
        _job_update(job_id, status="error", error=f"missing {ps1}")
        return
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
        "-GoalPath",
        str(goal_path),
        "-MaxAttempts",
        max_attempts,
    ]
    _run_subprocess(job_id, argv, REPO_ROOT, timeout=720)


app = FastAPI(title="Traffic Lab Flow API", version="1.0.0")


class BenchBody(BaseModel):
    goal_path: str | None = Field(
        default=None,
        description="相对仓库根的 goal 文件；若填写 goal_text 则忽略此项",
    )
    goal_text: str | None = Field(
        default=None,
        description="本次创作主题与要求（纯文本，优先于 goal_path）",
    )
    max_attempts: int = Field(default=6, ge=1, le=20)


class FullPipelineBody(BaseModel):
    """小白一键：合并 → Docker → Bench。"""

    goal_path: str | None = Field(
        default=None,
        description="相对仓库根的 UTF-8 goal 文件；goal_text 非空时忽略",
    )
    goal_text: str | None = Field(
        default=None,
        description="本次创作主题与要求（纯文本，优先于 goal_path）",
    )
    max_attempts: int = Field(default=6, ge=1, le=20)
    skip_export: bool = Field(
        default=False,
        description="为 true 时跳过合并（已有人工维护的 samples.json）",
    )
    export_dedupe: str | None = Field(
        default=None,
        description="合并去重 none|key|content；省略则使用环境变量 FLOW_API_EXPORT_DEDUPE（默认 none）",
    )


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    hermes_ok = openclaw_ok = False
    with httpx.Client(timeout=2.0) as client:
        try:
            r = client.get(DEFAULT_HERMES.rstrip("/") + "/")
            hermes_ok = r.status_code < 500
        except Exception:
            pass
        try:
            r = client.get(DEFAULT_OPENCLAW.rstrip("/") + "/")
            openclaw_ok = r.status_code < 500
        except Exception:
            pass
    return {
        "hermes": {"url": DEFAULT_HERMES.rstrip("/"), "ok": hermes_ok},
        "openclaw": {"url": DEFAULT_OPENCLAW.rstrip("/"), "ok": openclaw_ok},
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    """非敏感路径，供页面展示。"""
    return {
        "repo_root": str(REPO_ROOT),
        "mediacrawler_jsonl": DEFAULT_MC_IN,
        "feed_out": DEFAULT_FEED_OUT,
        "bench_goal_default": DEFAULT_GOAL_REL,
        "hermes_url": DEFAULT_HERMES.rstrip("/"),
        "openclaw_url": DEFAULT_OPENCLAW.rstrip("/"),
        "export_dedupe_default": _resolve_export_dedupe(None),
        "feed_digest_out": (os.environ.get("FLOW_API_FEED_DIGEST_OUT") or "").strip() or None,
        "feed_batch_id": (os.environ.get("FLOW_API_FEED_BATCH_ID") or "").strip() or None,
    }


@app.get("/api/runs/latest")
def api_runs_latest() -> dict[str, Any]:
    if not OUTPUT_RUNS.is_dir():
        return {"found": False, "name": None, "modified": None, "text": ""}
    files = [f for f in OUTPUT_RUNS.glob("*.txt") if f.is_file()]
    if not files:
        return {"found": False, "name": None, "modified": None, "text": ""}
    best = max(files, key=lambda p: p.stat().st_mtime)
    text = best.read_text(encoding="utf-8", errors="replace")
    if len(text) > 200_000:
        text = text[:200_000] + "\n\n... (truncated)"
    return {
        "found": True,
        "name": best.name,
        "modified": datetime.fromtimestamp(best.stat().st_mtime).isoformat(timespec="seconds"),
        "text": text,
    }


@app.post("/api/export-feed")
def api_export_feed(
    background_tasks: BackgroundTasks,
    dedupe: str | None = Query(
        default=None,
        description="none|key|content；省略则读 FLOW_API_EXPORT_DEDUPE",
    ),
) -> dict[str, Any]:
    mc = Path(DEFAULT_MC_IN)
    out = Path(DEFAULT_FEED_OUT)
    if not mc.exists():
        raise HTTPException(status_code=400, detail=f"mediacrawler path missing: {mc}")
    script = REPO_ROOT / "scripts" / "export_to_xhs_feed.py"
    if not script.is_file():
        raise HTTPException(status_code=500, detail="export script missing")
    try:
        mode = _resolve_export_dedupe(dedupe)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    digest_out = (os.environ.get("FLOW_API_FEED_DIGEST_OUT") or "").strip()
    batch_id = (os.environ.get("FLOW_API_FEED_BATCH_ID") or "").strip()
    argv = _export_to_feed_argv(mc, out, mode, digest_out, batch_id)
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "kind": "export-feed",
            "status": "queued",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    background_tasks.add_task(_run_subprocess, jid, argv, REPO_ROOT, 120)
    return {"job_id": jid, "message": "export started"}


@app.post("/api/docker-up")
def api_docker_up(background_tasks: BackgroundTasks) -> dict[str, Any]:
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "kind": "docker-up",
            "status": "queued",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    background_tasks.add_task(_run_subprocess, jid, ["docker", "compose", "up", "-d"], REPO_ROOT, 180)
    return {"job_id": jid, "message": "docker compose up -d started"}


@app.post("/api/run/full-pipeline")
def api_run_full_pipeline(body: FullPipelineBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        merge_dedupe = _resolve_export_dedupe(body.export_dedupe)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    jid = str(uuid.uuid4())
    goal = _resolve_goal_for_bench(jid, body.goal_text, body.goal_path)
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "kind": "full-pipeline",
            "status": "queued",
            "stage": "pending",
            "stage_index": 0,
            "pipeline_logs": [],
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    background_tasks.add_task(
        _run_full_pipeline,
        jid,
        goal,
        str(body.max_attempts),
        body.skip_export,
        merge_dedupe,
    )
    return {
        "job_id": jid,
        "message": "full pipeline started (merge → docker → bench)",
    }


@app.post("/api/run/bench")
def api_run_bench(body: BenchBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    jid = str(uuid.uuid4())
    goal = _resolve_goal_for_bench(jid, body.goal_text, body.goal_path)
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "kind": "bench",
            "status": "queued",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    background_tasks.add_task(_powershell_bench, jid, goal, str(body.max_attempts))
    return {"job_id": jid, "message": "bench started (may take several minutes)"}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        j = _jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return j


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
