"""TAVC 状态机：Think → Act → Verify → Correct；轨迹落盘。"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import brain
import client
import memory
import metrics
import models
import negative_pool
import scoring
import storage
import verify

logger = logging.getLogger(__name__)


def _xhs_poll_intervals() -> list[float]:
    """默认 6,12,24 秒便于本地联调；生产可设为逗号秒数列表（如 21600,43200,86400）。"""
    raw = (os.environ.get("HERMES_XHS_POLL_INTERVALS_SEC") or "6,12,24").strip()
    out: list[float] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(max(0.5, float(p)))
        except ValueError:
            continue
    return out if out else [6.0, 12.0, 24.0]


def resume_xhs_monitoring(task_id: str, real_note_id: str, published_at: str | None = None) -> models.TAVCRunResult:
    """手动发帖完成后：绑定 note_id 并执行多档 fetch_xhs_metrics + S3。"""
    return TAVCRunner(task_id)._run_xhs_monitoring_phase(real_note_id, published_at)


class TAVCRunner:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.session_file = storage.session_path(task_id)
        self.max_default = int(os.environ.get("HERMES_TAVC_MAX_ATTEMPTS") or "3")
        self._state: dict[str, Any] = {}

    def _flush(self) -> None:
        self._state["updated_at"] = storage.utc_now_iso()
        storage.atomic_write_session(self.session_file, self._state)

    def run(self, goal: str, max_attempts: int | None = None) -> models.TAVCRunResult:
        ma = max_attempts if max_attempts is not None else self.max_default
        ma = max(1, min(8, ma))
        trajectory: list[dict[str, Any]] = []
        neg_pool: list[dict[str, Any]] = []

        logger.info("[%s] TAVC(flow-mock) start goal=%r max_attempts=%d", self.task_id, goal[:200], ma)

        self._state = {
            "task_id": self.task_id,
            "status": "running",
            "lifecycle_phase": "pending",
            "goal": goal,
            "max_attempts": ma,
            "trajectory": trajectory,
            "negative_sample_pool": neg_pool,
            "final_envelope": None,
            "final_pass": False,
            "final_status": None,
            "candidate_formula": None,
            "last_reason": "",
            "error": None,
            "success_metric_history": [],
            "obs_negative_pool": {
                "verify_traces": [],
                "strike_events": [],
                "stale_retry_attempted": 0,
                "stale_retry_success": 0,
                "stale_retry_latency_ms_total": 0.0,
            },
            "updated_at": storage.utc_now_iso(),
        }
        self._flush()

        try:
            self._state["lifecycle_phase"] = "thinking"
            think_out = brain.think(goal)
            logger.info("[%s] S1 Think: plan=%s", self.task_id, think_out)
            trajectory.append(models.trajectory_step_to_jsonable(models.TrajectoryThinkStep(plan=think_out)))
            self._state["trajectory"] = trajectory
            self._flush()

            retrieved_case_ids = [
                str(x) for x in (think_out.get("retrieved_case_ids") or []) if str(x).strip()
            ]
            market_ctx = think_out.get("market_context")
            if not isinstance(market_ctx, dict):
                market_ctx = memory.market_fingerprint(goal)

            current: dict[str, Any] = {
                "action": think_out["action"],
                "params": think_out["params"],
                "variant_id": think_out.get("variant_id", "v1.1"),
            }
            final_envelope: dict[str, Any] | None = None
            final_pass = False
            last_reason = ""

            for attempt in range(ma):
                self._state["lifecycle_phase"] = "acting"
                obs_np: dict[str, Any] = self._state.setdefault(
                    "obs_negative_pool",
                    {
                        "verify_traces": [],
                        "strike_events": [],
                        "stale_retry_attempted": 0,
                        "stale_retry_success": 0,
                        "stale_retry_latency_ms_total": 0.0,
                    },
                )
                stale_act = bool(current.get("stale_retry"))
                logger.info(
                    "[%s] S2 Act: attempt=%d/%d action=%s params=%s variant=%s stale_retry=%s",
                    self.task_id,
                    attempt + 1,
                    ma,
                    current["action"],
                    current["params"],
                    current.get("variant_id", ""),
                    stale_act,
                )
                envelope = client.dispatch(
                    current["action"],
                    current["params"],
                    task_id=f"{self.task_id}-a{attempt + 1}",
                )
                final_envelope = envelope
                if stale_act:
                    obs_np["stale_retry_attempted"] = int(obs_np.get("stale_retry_attempted") or 0) + 1
                    obs_np["stale_retry_latency_ms_total"] = float(
                        obs_np.get("stale_retry_latency_ms_total") or 0.0
                    ) + negative_pool.extract_latency_ms(envelope)
                act_obs = {"stale_retry": True} if stale_act else None
                trajectory.append(
                    models.trajectory_step_to_jsonable(
                        models.TrajectoryActStep(
                            attempt=attempt + 1,
                            variant_id=str(current.get("variant_id", "v1.0")),
                            envelope=envelope,
                            obs_meta=act_obs,
                        )
                    )
                )
                self._state["trajectory"] = trajectory
                self._flush()

                if (
                    current["action"] == "prepare_xhs_post"
                    and envelope.get("status") == "success"
                ):
                    oc_pre = envelope.get("openclaw") if isinstance(envelope.get("openclaw"), dict) else None
                    if oc_pre and oc_pre.get("status") == "success":
                        rpre = oc_pre.get("result") if isinstance(oc_pre.get("result"), dict) else {}
                        self._state["xhs"] = {
                            "variant_id": str(current.get("variant_id", "")),
                            "real_note_id": None,
                            "prepare_headline": rpre.get("headline"),
                        }
                        self._state["lifecycle_phase"] = "awaiting_manual_publish"
                        self._state["status"] = "paused_manual"
                        self._state["final_pass"] = False
                        self._state["last_reason"] = (
                            f"paused: XHS awaiting_manual_publish — POST /task/{self.task_id}/xhs-sync "
                            f"with JSON body {{\"real_note_id\": \"...\"}}"
                        )
                        self._flush()
                        return models.TAVCRunResult(
                            task_id=self.task_id,
                            goal=goal,
                            final_status="paused_manual",
                            final_pass=False,
                            last_reason=self._state["last_reason"],
                            trajectory=trajectory,
                            final_envelope=final_envelope,
                            session_file=self.session_file,
                            error=None,
                            lifecycle_phase="awaiting_manual_publish",
                            candidate_formula=None,
                            negative_sample_pool=list(neg_pool),
                            obs_metrics=None,
                            paused=True,
                            pause_reason="awaiting_manual_publish",
                        )

                self._state["lifecycle_phase"] = "verifying"
                oc = envelope.get("openclaw") if isinstance(envelope.get("openclaw"), dict) else None
                rules_ok, rules_reason = verify.verify_flow_rules(current["action"], oc)
                mraw = (oc.get("result") or {}) if (oc and isinstance(oc.get("result"), dict)) else {}
                metrics_d: dict[str, Any] = {}
                hard_ok = False
                hard_reason = "skipped"
                soft_ok = False
                soft_reason = "skipped"
                if oc and oc.get("status") == "success":
                    metrics_d = metrics.metrics_from_executor(current["action"], mraw)
                    if rules_ok:
                        hard_ok, hard_reason = metrics.hard_verify_metrics(current["action"], metrics_d)
                        soft_ok, soft_reason = verify.soft_review(current["action"], goal, oc)
                    else:
                        hard_ok, hard_reason = False, "rules_failed_first"
                        soft_ok, soft_reason = True, "soft_skipped_rules_failed"
                else:
                    hard_ok, hard_reason = False, "executor_not_success"
                    soft_ok, soft_reason = False, "executor_not_success"

                passed = bool(rules_ok and hard_ok and soft_ok)
                hist = list(self._state.get("success_metric_history") or [])
                if metrics_d:
                    score, score_ctx = scoring.compute_verify_score(
                        metrics_d, hard_ok=passed, success_history=hist
                    )
                else:
                    score, score_ctx = 0.0, {}
                scm = "log" if scoring.score_use_log() else "linear"
                review_ok = soft_ok
                review_reason = f"hard:{hard_reason}; soft:{soft_reason}"
                last_reason = (
                    f"rules:{rules_reason}; hard:{hard_reason}; soft:{soft_reason}; "
                    f"score:{score}; score_mode:{scm}"
                )
                fp_cur = negative_pool.params_fingerprint(str(current["action"]), dict(current.get("params") or {}))
                pool_row = next((x for x in neg_pool if x.get("fp") == fp_cur), None)
                strike_snapshot = int(pool_row["strike_count"]) if pool_row else None
                obs_verify = {
                    "param_fp": fp_cur,
                    "strike_snapshot": strike_snapshot,
                    "stale_retry_act": stale_act,
                }
                obs_np.setdefault("verify_traces", []).append(
                    {
                        "attempt": attempt + 1,
                        "passed": passed,
                        "action": current["action"],
                        **obs_verify,
                        "hard_ok": hard_ok,
                        "soft_ok": soft_ok,
                        "hard_reason": hard_reason,
                    }
                )
                if stale_act:
                    negative_pool.record_stale_retry_outcome(passed)
                    if passed:
                        obs_np["stale_retry_success"] = int(obs_np.get("stale_retry_success") or 0) + 1
                logger.info(
                    "[%s] S3 Verify: attempt=%d rules_ok=%s hard_ok=%s soft_ok=%s score=%s passed=%s "
                    "strike_snapshot=%s stale_retry_act=%s",
                    self.task_id,
                    attempt + 1,
                    rules_ok,
                    hard_ok,
                    soft_ok,
                    score,
                    passed,
                    strike_snapshot,
                    stale_act,
                )

                trajectory.append(
                    models.trajectory_step_to_jsonable(
                        models.TrajectoryVerifyStep(
                            attempt=attempt + 1,
                            passed=passed,
                            rules_ok=rules_ok,
                            rules_reason=rules_reason,
                            review_ok=review_ok,
                            review_reason=review_reason,
                            score=score,
                            hard_pass=hard_ok,
                            soft_pass=soft_ok,
                            hard_reason=hard_reason,
                            soft_reason=soft_reason,
                            metrics=metrics_d or None,
                            score_mode=scm,
                            score_context=score_ctx or None,
                            obs_verify=obs_verify,
                        )
                    )
                )
                self._state["trajectory"] = trajectory
                self._state["last_reason"] = last_reason
                self._flush()

                if not passed and oc and oc.get("status") == "success" and rules_ok and not hard_ok:
                    if hard_reason == "insufficient_sample_defer":
                        rec = None
                    else:
                        rec = negative_pool.negative_pool_record(
                            neg_pool,
                            current,
                            hard_reason,
                            tavc_round=attempt + 1,
                            metrics=metrics_d or None,
                        )
                    if rec:
                        fp_r, strike_after = rec
                        obs_np.setdefault("strike_events", []).append(
                            {
                                "attempt": attempt + 1,
                                "fp": fp_r,
                                "strike_after": strike_after,
                                "tavc_round": attempt + 1,
                            }
                        )

                if passed:
                    try:
                        memory.MemoryManager().apply_verify_feedback(
                            retrieved_case_ids,
                            market_ctx,
                            passed=True,
                            tavc_round=attempt + 1,
                            pool=None,
                        )
                    except Exception:
                        logger.exception("[%s] case_library verify feedback (pass) failed", self.task_id)
                    final_pass = True
                    self._state["candidate_formula"] = {
                        "variant_id": current.get("variant_id"),
                        "action": current["action"],
                        "params": dict(current.get("params") or {}),
                        "score": score,
                        "metrics": metrics_d,
                    }
                    sh = self._state.setdefault("success_metric_history", [])
                    try:
                        row: dict[str, Any] = {
                            "likes": int(metrics_d.get("likes") or 0),
                            "attempt": attempt + 1,
                        }
                        if metrics_d.get("ctr_pct") is not None:
                            row["ctr_pct"] = float(metrics_d["ctr_pct"])
                        sh.append(row)
                    except (TypeError, ValueError):
                        pass
                    cap = max(scoring.score_dynamic_k(), 48)
                    while len(sh) > cap:
                        sh.pop(0)
                    logger.info("[%s] TAVC success attempt=%d", self.task_id, attempt + 1)
                    break

                if attempt >= ma - 1:
                    logger.warning("[%s] TAVC exhausted attempts", self.task_id)
                    break

                self._state["lifecycle_phase"] = "correcting"
                fb: dict[str, Any] = {}
                try:
                    fb = memory.MemoryManager().apply_verify_feedback(
                        retrieved_case_ids,
                        market_ctx,
                        passed=False,
                        tavc_round=attempt + 1,
                        pool=neg_pool,
                    )
                except Exception:
                    logger.exception("[%s] case_library verify feedback (fail) failed", self.task_id)
                corrected = brain.pseudo_correct(
                    goal,
                    trajectory,
                    current,
                    failed_attempt=attempt + 1,
                    pool=neg_pool,
                    pool_check_round=attempt + 2,
                    cognitive_conflict_hint=str(fb.get("cognitive_hint") or ""),
                    force_explore=bool(fb.get("force_explore")),
                )
                logger.info("[%s] S4 Correct: revision=%s", self.task_id, corrected)
                trajectory.append(
                    models.trajectory_step_to_jsonable(
                        models.TrajectoryCorrectStep(attempt=attempt + 1, revision=corrected)
                    )
                )
                current = {
                    "action": corrected["action"],
                    "params": corrected["params"],
                    "variant_id": corrected.get("variant_id", f"v1.{attempt + 2}"),
                }
                if corrected.get("stale_retry"):
                    current["stale_retry"] = True
                self._state["trajectory"] = trajectory
                self._flush()

            obs_raw = self._state.get("obs_negative_pool") or {}
            strike_ev = list(obs_raw.get("strike_events") or [])
            obs_summary = negative_pool.finalize_obs_negative_pool(trajectory, strike_ev, obs_raw)
            self._state["obs_negative_pool_summary"] = obs_summary
            try:
                negative_pool.merge_negative_pool_global_aggregate(obs_summary)
            except Exception:
                logger.exception("[%s] merge negative pool aggregate failed", self.task_id)
            logger.info(
                "[%s] obs_negative_pool survival_by_strike=%s stale_retry_success_rate=%s stale_cost_ms=%s",
                self.task_id,
                obs_summary.get("negative_sample_survival_rate_by_strike"),
                obs_summary.get("stale_retry_success_rate"),
                obs_summary.get("stale_retry_compute_cost_ms"),
            )

            self._state["final_envelope"] = final_envelope
            self._state["final_pass"] = final_pass
            self._state["final_status"] = "success" if final_pass else "failed"
            self._state["status"] = "completed" if final_pass else "failed"
            self._state["lifecycle_phase"] = "completed" if final_pass else "failed"
            self._state["last_reason"] = last_reason
            self._flush()

            if final_pass:
                try:
                    archived = memory.MemoryManager().archive_session(dict(self._state))
                    if archived:
                        logger.info("[%s] case_library archived: %s", self.task_id, archived)
                except Exception:
                    logger.exception("[%s] archive_session failed", self.task_id)

            return models.TAVCRunResult(
                task_id=self.task_id,
                goal=goal,
                final_status=self._state["final_status"],
                final_pass=final_pass,
                last_reason=last_reason,
                trajectory=trajectory,
                final_envelope=final_envelope,
                session_file=self.session_file,
                error=None,
                lifecycle_phase=str(self._state.get("lifecycle_phase") or ""),
                candidate_formula=self._state.get("candidate_formula"),
                negative_sample_pool=list(self._state.get("negative_sample_pool") or []),
                obs_metrics=obs_summary,
                paused=False,
                pause_reason=None,
            )
        except Exception as e:
            logger.exception("[%s] TAVC fatal: %s", self.task_id, e)
            self._state["status"] = "failed"
            self._state["lifecycle_phase"] = "failed"
            self._state["error"] = str(e)
            self._state["final_status"] = "failed"
            self._state["final_pass"] = False
            self._flush()
            obs_raw_e = self._state.get("obs_negative_pool") or {}
            try:
                obs_summary_e = negative_pool.finalize_obs_negative_pool(
                    list(self._state.get("trajectory") or []),
                    list(obs_raw_e.get("strike_events") or []),
                    obs_raw_e,
                )
            except Exception:
                obs_summary_e = None
            return models.TAVCRunResult(
                task_id=self.task_id,
                goal=goal,
                final_status="failed",
                final_pass=False,
                last_reason=str(e),
                trajectory=self._state.get("trajectory", []),
                final_envelope=self._state.get("final_envelope"),
                session_file=self.session_file,
                error=str(e),
                lifecycle_phase=str(self._state.get("lifecycle_phase") or "failed"),
                candidate_formula=self._state.get("candidate_formula"),
                negative_sample_pool=list(self._state.get("negative_sample_pool") or []),
                obs_metrics=obs_summary_e,
                paused=False,
                pause_reason=None,
            )

    def _reload_session(self) -> None:
        import json

        with open(self.session_file, encoding="utf-8") as f:
            self._state = json.load(f)

    def _run_xhs_monitoring_phase(self, real_note_id: str, published_at: str | None) -> models.TAVCRunResult:
        self._reload_session()
        if self._state.get("lifecycle_phase") != "awaiting_manual_publish":
            return models.TAVCRunResult(
                task_id=self.task_id,
                goal=str(self._state.get("goal") or ""),
                final_status="failed",
                final_pass=False,
                last_reason="xhs_resume: session not in awaiting_manual_publish",
                trajectory=list(self._state.get("trajectory") or []),
                final_envelope=self._state.get("final_envelope"),
                session_file=self.session_file,
                error="bad_phase",
                lifecycle_phase=str(self._state.get("lifecycle_phase") or ""),
                candidate_formula=self._state.get("candidate_formula"),
                negative_sample_pool=list(self._state.get("negative_sample_pool") or []),
                obs_metrics=None,
                paused=False,
                pause_reason=None,
            )

        trajectory: list[dict[str, Any]] = list(self._state.get("trajectory") or [])
        neg_pool: list[dict[str, Any]] = list(self._state.get("negative_sample_pool") or [])
        goal = str(self._state.get("goal") or "")
        xhs = self._state.get("xhs") or {}
        vid = str(xhs.get("variant_id") or "")
        if not vid.strip():
            vid = "vXHS.unknown"

        retrieved_case_ids: list[str] = []
        market_ctx: dict[str, Any] = memory.market_fingerprint(goal)
        for step in trajectory:
            if isinstance(step, dict) and step.get("phase") == "think":
                pl = step.get("plan") or {}
                retrieved_case_ids = [str(x) for x in (pl.get("retrieved_case_ids") or []) if str(x).strip()]
                mc = pl.get("market_context")
                if isinstance(mc, dict):
                    market_ctx = mc
                break

        obs_np: dict[str, Any] = self._state.setdefault(
            "obs_negative_pool",
            {
                "verify_traces": [],
                "strike_events": [],
                "stale_retry_attempted": 0,
                "stale_retry_success": 0,
                "stale_retry_latency_ms_total": 0.0,
            },
        )

        sync_payload = {
            "variant_id": vid,
            "real_note_id": real_note_id.strip(),
            "published_at": (published_at or "").strip(),
        }
        sync_env = client.dispatch(
            "sync_manual_result", sync_payload, task_id=f"{self.task_id}-xhs-sync"
        )
        act_n0 = sum(1 for x in trajectory if isinstance(x, dict) and x.get("phase") == "act")
        trajectory.append(
            models.trajectory_step_to_jsonable(
                models.TrajectoryActStep(
                    attempt=act_n0 + 1,
                    variant_id=vid,
                    envelope=sync_env,
                    obs_meta={"xhs_manual_sync": True},
                )
            )
        )
        self._state["trajectory"] = trajectory
        oc_s = sync_env.get("openclaw") if isinstance(sync_env.get("openclaw"), dict) else None
        if sync_env.get("status") != "success" or not oc_s or oc_s.get("status") != "success":
            self._state["last_reason"] = "xhs_sync_openclaw_failed"
            self._state["status"] = "failed"
            self._state["lifecycle_phase"] = "failed"
            self._flush()
            return models.TAVCRunResult(
                task_id=self.task_id,
                goal=goal,
                final_status="failed",
                final_pass=False,
                last_reason=self._state["last_reason"],
                trajectory=trajectory,
                final_envelope=sync_env,
                session_file=self.session_file,
                error=None,
                lifecycle_phase="failed",
                candidate_formula=None,
                negative_sample_pool=neg_pool,
                obs_metrics=None,
                paused=False,
                pause_reason=None,
            )

        rules_s, rr_s = verify.verify_flow_rules("sync_manual_result", oc_s)
        if not rules_s:
            self._state["last_reason"] = f"xhs_sync_rules:{rr_s}"
            self._state["status"] = "failed"
            self._state["lifecycle_phase"] = "failed"
            self._flush()
            return models.TAVCRunResult(
                task_id=self.task_id,
                goal=goal,
                final_status="failed",
                final_pass=False,
                last_reason=self._state["last_reason"],
                trajectory=trajectory,
                final_envelope=sync_env,
                session_file=self.session_file,
                error=None,
                lifecycle_phase="failed",
                candidate_formula=None,
                negative_sample_pool=neg_pool,
                obs_metrics=None,
                paused=False,
                pause_reason=None,
            )

        if isinstance(self._state.get("xhs"), dict):
            self._state["xhs"]["real_note_id"] = real_note_id.strip()
        self._state["lifecycle_phase"] = "monitoring"
        self._flush()

        final_envelope: dict[str, Any] | None = sync_env
        final_pass = False
        last_reason = ""
        intervals = _xhs_poll_intervals()

        for chk, delay in enumerate(intervals):
            logger.info("[%s] XHS monitoring sleep %.1fs then fetch check_index=%d", self.task_id, delay, chk)
            time.sleep(delay)
            fetch_params = {
                "variant_id": vid,
                "note_id": real_note_id.strip(),
                "check_index": chk,
                "metrics_to_track": ["likes", "collects", "comments"],
            }
            current = {"action": "fetch_xhs_metrics", "params": fetch_params, "variant_id": vid}
            envelope = client.dispatch(
                "fetch_xhs_metrics", fetch_params, task_id=f"{self.task_id}-xhsf{chk}"
            )
            final_envelope = envelope
            act_n = sum(1 for x in trajectory if isinstance(x, dict) and x.get("phase") == "act")
            trajectory.append(
                models.trajectory_step_to_jsonable(
                    models.TrajectoryActStep(
                        attempt=act_n + 1,
                        variant_id=vid,
                        envelope=envelope,
                        obs_meta={"xhs_fetch_round": chk},
                    )
                )
            )
            self._state["trajectory"] = trajectory
            self._flush()

            oc = envelope.get("openclaw") if isinstance(envelope.get("openclaw"), dict) else None
            rules_ok, rules_reason = verify.verify_flow_rules("fetch_xhs_metrics", oc)
            mraw = (oc.get("result") or {}) if (oc and isinstance(oc.get("result"), dict)) else {}
            metrics_d: dict[str, Any] = {}
            hard_ok = False
            hard_reason = "skipped"
            soft_ok = False
            soft_reason = "skipped"
            if oc and oc.get("status") == "success":
                metrics_d = metrics.metrics_from_executor("fetch_xhs_metrics", mraw)
                if rules_ok:
                    hard_ok, hard_reason = metrics.hard_verify_metrics("fetch_xhs_metrics", metrics_d)
                    soft_ok, soft_reason = verify.soft_review("fetch_xhs_metrics", goal, oc)
                else:
                    hard_ok, hard_reason = False, "rules_failed_first"
                    soft_ok, soft_reason = True, "soft_skipped_rules_failed"
            else:
                hard_ok, hard_reason = False, "executor_not_success"
                soft_ok, soft_reason = False, "executor_not_success"

            passed = bool(rules_ok and hard_ok and soft_ok)
            hist = list(self._state.get("success_metric_history") or [])
            if metrics_d:
                score, score_ctx = scoring.compute_verify_score(
                    metrics_d, hard_ok=passed, success_history=hist
                )
            else:
                score, score_ctx = 0.0, {}
            scm = "log" if scoring.score_use_log() else "linear"
            review_ok = soft_ok
            review_reason = f"hard:{hard_reason}; soft:{soft_reason}"
            last_reason = (
                f"rules:{rules_reason}; hard:{hard_reason}; soft:{soft_reason}; "
                f"score:{score}; score_mode:{scm}; xhs_check:{chk}"
            )
            fp_cur = negative_pool.params_fingerprint(
                str(current["action"]), dict(current.get("params") or {})
            )
            pool_row = next((x for x in neg_pool if x.get("fp") == fp_cur), None)
            strike_snapshot = int(pool_row["strike_count"]) if pool_row else None
            obs_verify = {"param_fp": fp_cur, "strike_snapshot": strike_snapshot, "stale_retry_act": False}
            obs_np.setdefault("verify_traces", []).append(
                {
                    "attempt": chk + 1,
                    "passed": passed,
                    "action": "fetch_xhs_metrics",
                    **obs_verify,
                    "hard_ok": hard_ok,
                    "soft_ok": soft_ok,
                    "hard_reason": hard_reason,
                }
            )
            trajectory.append(
                models.trajectory_step_to_jsonable(
                    models.TrajectoryVerifyStep(
                        attempt=chk + 1,
                        passed=passed,
                        rules_ok=rules_ok,
                        rules_reason=rules_reason,
                        review_ok=review_ok,
                        review_reason=review_reason,
                        score=score,
                        hard_pass=hard_ok,
                        soft_pass=soft_ok,
                        hard_reason=hard_reason,
                        soft_reason=soft_reason,
                        metrics=metrics_d or None,
                        score_mode=scm,
                        score_context=score_ctx or None,
                        obs_verify=obs_verify,
                    )
                )
            )
            self._state["trajectory"] = trajectory
            self._state["last_reason"] = last_reason
            self._flush()

            if not passed and oc and oc.get("status") == "success" and rules_ok and not hard_ok:
                if hard_reason != "insufficient_sample_defer":
                    rec = negative_pool.negative_pool_record(
                        neg_pool,
                        current,
                        hard_reason,
                        tavc_round=chk + 1,
                        metrics=metrics_d or None,
                    )
                    if rec:
                        fp_r, strike_after = rec
                        obs_np.setdefault("strike_events", []).append(
                            {
                                "attempt": chk + 1,
                                "fp": fp_r,
                                "strike_after": strike_after,
                                "tavc_round": chk + 1,
                            }
                        )

            if passed:
                try:
                    memory.MemoryManager().apply_verify_feedback(
                        retrieved_case_ids,
                        market_ctx,
                        passed=True,
                        tavc_round=chk + 1,
                        pool=None,
                    )
                except Exception:
                    logger.exception("[%s] case_library verify feedback (xhs pass) failed", self.task_id)
                final_pass = True
                self._state["candidate_formula"] = {
                    "variant_id": vid,
                    "action": "fetch_xhs_metrics",
                    "params": dict(fetch_params),
                    "score": score,
                    "metrics": metrics_d,
                }
                sh = self._state.setdefault("success_metric_history", [])
                try:
                    row: dict[str, Any] = {
                        "likes": int(metrics_d.get("likes") or 0),
                        "attempt": chk + 1,
                    }
                    if metrics_d.get("ctr_pct") is not None:
                        row["ctr_pct"] = float(metrics_d["ctr_pct"])
                    sh.append(row)
                except (TypeError, ValueError):
                    pass
                cap = max(scoring.score_dynamic_k(), 48)
                while len(sh) > cap:
                    sh.pop(0)
                break

            if hard_reason != "insufficient_sample_defer" and not passed:
                break

        obs_raw = self._state.get("obs_negative_pool") or {}
        strike_ev = list(obs_raw.get("strike_events") or [])
        obs_summary = negative_pool.finalize_obs_negative_pool(trajectory, strike_ev, obs_raw)
        self._state["obs_negative_pool_summary"] = obs_summary
        try:
            negative_pool.merge_negative_pool_global_aggregate(obs_summary)
        except Exception:
            logger.exception("[%s] merge negative pool aggregate failed (xhs)", self.task_id)

        self._state["final_envelope"] = final_envelope
        self._state["final_pass"] = final_pass
        self._state["final_status"] = "success" if final_pass else "failed"
        self._state["status"] = "completed" if final_pass else "failed"
        self._state["lifecycle_phase"] = "completed" if final_pass else "failed"
        self._state["last_reason"] = last_reason
        self._state["negative_sample_pool"] = neg_pool
        self._flush()

        if final_pass:
            try:
                archived = memory.MemoryManager().archive_session(dict(self._state))
                if archived:
                    logger.info("[%s] case_library archived (xhs): %s", self.task_id, archived)
            except Exception:
                logger.exception("[%s] archive_session failed (xhs)", self.task_id)

        return models.TAVCRunResult(
            task_id=self.task_id,
            goal=goal,
            final_status=self._state["final_status"],
            final_pass=final_pass,
            last_reason=last_reason,
            trajectory=trajectory,
            final_envelope=final_envelope,
            session_file=self.session_file,
            error=None,
            lifecycle_phase=str(self._state.get("lifecycle_phase") or ""),
            candidate_formula=self._state.get("candidate_formula"),
            negative_sample_pool=neg_pool,
            obs_metrics=obs_summary,
            paused=False,
            pause_reason=None,
        )


def spawn_tavc(task_id: str, goal: str, max_attempts: int) -> None:
    TAVCRunner(task_id).run(goal, max_attempts)
