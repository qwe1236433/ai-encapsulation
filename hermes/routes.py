"""HTTP API：TAVC 派发与观测端点。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import client
import metrics
import models
import negative_pool
import runner
import scoring
import storage
import verify


def register_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health():
        return {"status": "ok", "role": "orchestrator"}

    @app.post("/dispatch")
    def dispatch_endpoint(req: models.DispatchRequest):
        envelope = client.dispatch(req.action, req.params, task_id=req.task_id)
        oc = envelope.get("openclaw") if isinstance(envelope.get("openclaw"), dict) else None
        rules_ok, rules_reason = verify.verify_flow_rules(req.action, oc)
        mraw = (oc.get("result") or {}) if (oc and isinstance(oc.get("result"), dict)) else {}
        mets = metrics.metrics_from_executor(req.action, mraw) if (oc and oc.get("status") == "success") else {}
        hard_ok, hard_reason = (
            metrics.hard_verify_metrics(req.action, mets)
            if (oc and oc.get("status") == "success" and rules_ok)
            else (False, "skipped")
        )
        soft_ok, soft_reason = (
            verify.soft_review(req.action, "", oc)
            if (oc and oc.get("status") == "success")
            else (False, "skipped")
        )
        if oc and oc.get("status") == "success" and not rules_ok:
            hard_ok, hard_reason = False, "rules_failed_first"
            soft_ok, soft_reason = True, "soft_skipped_rules_failed"
        review_ok = soft_ok
        review_reason = f"hard:{hard_reason}; soft:{soft_reason}"
        if mets:
            score, score_ctx = scoring.compute_verify_score(
                mets,
                hard_ok=(rules_ok and hard_ok and soft_ok),
                success_history=None,
            )
        else:
            score, score_ctx = 0.0, {}
        verified = bool(
            rules_ok and hard_ok and soft_ok and verify.verify_openclaw_response(req.action, oc)
        )
        return {
            **envelope,
            "verified": verified,
            "rules_ok": rules_ok,
            "rules_reason": rules_reason,
            "review_ok": review_ok,
            "review_reason": review_reason,
            "hard_ok": hard_ok,
            "hard_reason": hard_reason,
            "soft_ok": soft_ok,
            "soft_reason": soft_reason,
            "score": score,
            "score_mode": "log" if scoring.score_use_log() else "linear",
            "score_context": score_ctx,
        }

    @app.post("/task")
    async def task_submit(req: models.TaskRequest):
        task_id = str(uuid.uuid4())
        path = storage.session_path(task_id)
        initial = {
            "task_id": task_id,
            "status": "pending",
            "goal": req.goal,
            "max_attempts": req.max_attempts,
            "trajectory": [],
            "final_envelope": None,
            "final_pass": None,
            "final_status": None,
            "last_reason": "",
            "error": None,
            "updated_at": storage.utc_now_iso(),
        }
        storage.atomic_write_session(path, initial)
        t = threading.Thread(target=runner.spawn_tavc, args=(task_id, req.goal, req.max_attempts), daemon=True)
        t.start()
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id, "status": "accepted", "poll_url": f"/task/{task_id}"},
        )

    @app.post("/task/sync")
    async def task_sync(req: models.TaskRequest):
        loop = asyncio.get_event_loop()
        task_id = str(uuid.uuid4())
        path = storage.session_path(task_id)
        initial = {
            "task_id": task_id,
            "status": "pending",
            "goal": req.goal,
            "max_attempts": req.max_attempts,
            "trajectory": [],
            "final_envelope": None,
            "final_pass": None,
            "final_status": None,
            "last_reason": "",
            "error": None,
            "updated_at": storage.utc_now_iso(),
        }
        storage.atomic_write_session(path, initial)

        def _run() -> models.TAVCRunResult:
            return runner.TAVCRunner(task_id).run(req.goal, req.max_attempts)

        result = await loop.run_in_executor(None, _run)
        return result.model_dump(mode="json")

    @app.get("/obs/negative-pool")
    def obs_negative_pool():
        path = negative_pool.negative_obs_aggregate_path()
        data: dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = {"error": "invalid aggregate file", "path": path}
        else:
            data = {"note": "no aggregate yet", "path": path}
        lock = negative_pool.stale_outcome_lock()
        dq = negative_pool.stale_retry_outcomes()
        with lock:
            stale_recent = list(dq)[-100:]
        sr = (sum(stale_recent) / len(stale_recent)) if stale_recent else None
        sg = data.get("stale_retry_global") if isinstance(data, dict) else None
        g_rate = None
        if isinstance(sg, dict):
            att_g = int(sg.get("attempted") or 0)
            if att_g:
                g_rate = int(sg.get("success") or 0) / att_g
        survival = data.get("strike_survival_totals") if isinstance(data, dict) else None
        survival_rates: dict[str, Any] = {}
        if isinstance(survival, dict):
            for k, row in survival.items():
                if not isinstance(row, dict):
                    continue
                n = int(row.get("n") or 0)
                ls = int(row.get("later_success") or 0)
                survival_rates[str(k)] = {
                    "n": n,
                    "later_success": ls,
                    "negative_sample_survival_rate": (ls / n) if n else None,
                }
        return {
            "aggregate_file": path,
            "strike_survival_totals": survival_rates or survival,
            "stale_retry_global": sg,
            "stale_retry_global_success_rate": g_rate,
            "process_stale_window_len": len(stale_recent),
            "process_stale_window_success_rate": sr,
            "dynamic_mode": negative_pool.negative_pool_dynamic_mode(),
            "effective_stale_retry_prob": negative_pool.effective_stale_retry_prob(),
        }

    @app.post("/task/{task_id}/xhs-sync")
    async def task_xhs_sync(task_id: str, req: models.XhsSyncRequest):
        """小红书手动发帖完成后：绑定 real_note_id 并进入 monitoring（多档 fetch + S3）。"""
        if not storage.safe_task_filename(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        path = storage.session_path(task_id)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="task not found")
        loop = asyncio.get_event_loop()

        def _go() -> models.TAVCRunResult:
            pat = (req.published_at or "").strip() or None
            return runner.resume_xhs_monitoring(task_id, req.real_note_id.strip(), pat)

        result = await loop.run_in_executor(None, _go)
        return result.model_dump(mode="json")

    @app.get("/task/{task_id}")
    def task_get(task_id: str):
        if not storage.safe_task_filename(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        path = storage.session_path(task_id)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="task not found")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @app.get("/test-link")
    def test_link():
        base = (os.environ.get("OPENCLAW_URL") or "").strip().rstrip("/")
        if not base:
            return {
                "ok": False,
                "role": "orchestrator",
                "peer": "openclaw",
                "target": None,
                "peer_health": None,
                "error": "OPENCLAW_URL is empty",
            }
        target = f"{base}/health"
        data, err = client.fetch_json(target)
        peer_ok = (
            err is None
            and isinstance(data, dict)
            and data.get("status") == "ok"
            and data.get("role") == "executor"
        )
        return {
            "ok": peer_ok,
            "role": "orchestrator",
            "peer": "openclaw",
            "target": target,
            "peer_health": data,
            "error": err,
        }

    @app.get("/")
    def root():
        return {
            "service": "hermes",
            "mode": "traffic_lab_tavc",
            "architecture": "Traffic-Lab v1: Hermes(strategic) + OpenClaw(tactical)",
            "openclaw_url": os.environ.get("OPENCLAW_URL", ""),
            "sessions_dir": storage.sessions_dir(),
            "modules": [
                "brain",
                "runner",
                "scoring",
                "negative_pool",
                "memory",
                "verify",
                "metrics",
                "client",
            ],
            "endpoints": [
                "POST /task (202)",
                "POST /task/sync",
                "POST /task/{task_id}/xhs-sync",
                "GET /task/{task_id}",
                "GET /obs/negative-pool",
                "/dispatch",
                "/test-link",
                "/health",
            ],
        }
