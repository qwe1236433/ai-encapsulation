"""API 与 TAVC 轨迹的 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class DispatchRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None


class TaskRequest(BaseModel):
    goal: str
    max_attempts: int = Field(3, ge=1, le=8)


class XhsSyncRequest(BaseModel):
    """手动发帖完成后绑定 real_note_id；与 experiment_variant_id（会话内 variant）一一对应。"""

    real_note_id: str = Field(..., min_length=1)
    published_at: str | None = None


class TrajectoryThinkStep(BaseModel):
    phase: Literal["think"] = "think"
    plan: dict[str, Any]


class TrajectoryActStep(BaseModel):
    phase: Literal["act"] = "act"
    attempt: int
    variant_id: str = "v1.0"
    envelope: dict[str, Any]
    obs_meta: dict[str, Any] | None = None


class TrajectoryVerifyStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    phase: Literal["verify"] = "verify"
    attempt: int
    passed: bool = Field(validation_alias="pass", serialization_alias="pass")
    rules_ok: bool
    rules_reason: str
    review_ok: bool
    review_reason: str
    score: float = 0.0
    hard_pass: bool = True
    soft_pass: bool = True
    hard_reason: str = ""
    soft_reason: str = ""
    metrics: dict[str, Any] | None = None
    score_mode: str = "log"
    score_context: dict[str, Any] | None = None
    obs_verify: dict[str, Any] | None = None


class TrajectoryCorrectStep(BaseModel):
    phase: Literal["correct"] = "correct"
    attempt: int
    revision: dict[str, Any]


TrajectoryStep: TypeAlias = (
    TrajectoryThinkStep | TrajectoryActStep | TrajectoryVerifyStep | TrajectoryCorrectStep
)


def trajectory_step_to_jsonable(step: TrajectoryStep) -> dict[str, Any]:
    return step.model_dump(mode="json", by_alias=True)


class SessionRecord(BaseModel):
    task_id: str
    status: str
    goal: str | None = None
    max_attempts: int | None = None
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    final_envelope: dict[str, Any] | None = None
    final_pass: bool | None = None
    final_status: str | None = None
    last_reason: str = ""
    error: str | None = None
    updated_at: str = ""


class TAVCRunResult(BaseModel):
    task_id: str
    goal: str
    final_status: str
    final_pass: bool
    last_reason: str
    trajectory: list[dict[str, Any]]
    final_envelope: dict[str, Any] | None = None
    session_file: str
    error: str | None = None
    lifecycle_phase: str | None = None
    candidate_formula: dict[str, Any] | None = None
    negative_sample_pool: list[dict[str, Any]] | None = None
    obs_metrics: dict[str, Any] | None = None
    paused: bool = False
    pause_reason: str | None = None
