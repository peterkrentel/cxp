"""Typed contracts for capability-specific agent output."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .agent_shell import _strip_trailing_commas, strip_code_fence


class ContractParseError(ValueError):
    """An agent response did not satisfy its capability contract."""


class PlannedTask(BaseModel):
    type: Literal["code", "verify", "reflect"]
    capability: Literal["code", "verify", "reflect"]
    goal: str
    instructions: str
    priority: int = Field(default=2, ge=1, le=5)

    @field_validator("goal", "instructions", mode="before")
    @classmethod
    def coerce_text(cls, value: object) -> str:
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value) if value is not None else ""


class PlanResult(BaseModel):
    subtasks: list[PlannedTask]
    source_count: int = Field(default=0, ge=0)
    dropped_subtasks: list[str] = Field(default_factory=list)


class ArtifactResult(BaseModel):
    content: str
    format: Literal["python", "yaml", "markdown", "text"]


class VerificationResult(BaseModel):
    score: float | None = Field(default=None, ge=0, le=1)
    passed: bool = False
    issues: list[str] = Field(default_factory=list)
    suggestion: str = ""


class AssessmentResult(BaseModel):
    labels: list[str] = Field(default_factory=list)
    verdict: str = ""
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class SkillRevisionCandidate(BaseModel):
    target_role: Literal["planner", "executor", "verifier"]
    content: str
    source_attempt_id: str
    rationale: str
    evidence_class: Literal["contract", "deterministic-validator", "judgment"] = "judgment"


def _infer_fenced_format(text: str) -> Literal["python", "yaml", "markdown", "text"]:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if not first_line.startswith("```"):
        return "text"
    language = first_line[3:].strip().lower()
    return {"py": "python", "python": "python", "yml": "yaml", "yaml": "yaml", "md": "markdown", "markdown": "markdown"}.get(language, "text")


def _normalize_plan_tasks(tasks: object) -> list[dict]:
    if isinstance(tasks, dict) and any(key in tasks for key in ("goal", "type", "capability")):
        # Under json_mode, the model sometimes emits one bare task object
        # instead of a JSON array when the goal only really needs a single
        # sub-task -- confirmed live across 3 separate SMOKE runs
        # (2026-08-23/24). Treat it as a single-subtask plan rather than
        # hard-rejecting a genuinely usable response with zero sub-tasks
        # spawned. Gated on looking task-like (not just any dict) so an
        # unrelated object (e.g. an error message) still gets rejected below.
        tasks = [tasks]
    if not isinstance(tasks, list):
        raise ValueError("plan result must be a JSON array")
    normalized = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each plan subtask must be a JSON object")
        capability = task.get("capability") or task.get("type", "code")
        if capability not in ("code", "verify", "reflect"):
            capability = "code"
        normalized.append({**task, "type": capability, "capability": capability})
    return normalized


def _parse_plan(raw_text: str) -> PlanResult:
    cleaned = _strip_trailing_commas(strip_code_fence(raw_text))
    normalized_tasks = _normalize_plan_tasks(json.loads(cleaned, strict=False))
    valid_subtasks = []
    dropped_subtasks = []
    for index, task in enumerate(normalized_tasks):
        try:
            valid_subtasks.append(PlannedTask.model_validate(task))
        except ValidationError as exc:
            dropped_subtasks.append(f"subtask {index}: {exc}")
            continue
    return PlanResult(
        subtasks=valid_subtasks,
        source_count=len(normalized_tasks),
        dropped_subtasks=dropped_subtasks,
    )


def parse_contract(
    capability: str,
    raw_text: str,
    expected_format: Literal["python", "yaml", "markdown", "text"] | None = None,
) -> PlanResult | ArtifactResult | VerificationResult | AssessmentResult:
    """Parse one capability result and raise an error naming its producer."""
    try:
        if capability == "plan":
            return _parse_plan(raw_text)
        if capability == "code":
            format_name = expected_format or _infer_fenced_format(raw_text)
            return ArtifactResult(
                content=strip_code_fence(raw_text),
                format=format_name,
            )
        if capability == "verify":
            return VerificationResult.model_validate(json.loads(strip_code_fence(raw_text), strict=False))
        if capability == "assess":
            return AssessmentResult.model_validate(json.loads(strip_code_fence(raw_text), strict=False))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ContractParseError(f"{capability} contract validation failed: {exc}") from exc
    raise ContractParseError(f"{capability} has no output contract")