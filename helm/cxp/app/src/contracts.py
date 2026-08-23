"""Typed contracts for capability-specific agent output."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


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
    score: float = Field(ge=0, le=1)
    passed: bool
    issues: list[str]
    suggestion: str


class AssessmentResult(BaseModel):
    labels: list[str]
    verdict: str
    strengths: list[str]
    gaps: list[str]


class SkillRevisionCandidate(BaseModel):
    target_role: Literal["planner", "executor", "verifier"]
    content: str
    source_attempt_id: str
    rationale: str
    evidence_class: Literal["contract", "deterministic-validator", "judgment"] = "judgment"


_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


def _unwrap_outer_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "```":
            return "\n".join(lines[1:index]).strip()
    return text


def _infer_fenced_format(text: str) -> Literal["python", "yaml", "markdown", "text"]:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if not first_line.startswith("```"):
        return "text"
    language = first_line[3:].strip().lower()
    return {"py": "python", "python": "python", "yml": "yaml", "yaml": "yaml", "md": "markdown", "markdown": "markdown"}.get(language, "text")


def _normalize_plan_tasks(tasks: object) -> list[dict]:
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
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", _unwrap_outer_fence(raw_text))
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
                content=_unwrap_outer_fence(raw_text),
                format=format_name,
            )
        if capability == "verify":
            return VerificationResult.model_validate(json.loads(_unwrap_outer_fence(raw_text), strict=False))
        if capability == "assess":
            return AssessmentResult.model_validate(json.loads(_unwrap_outer_fence(raw_text), strict=False))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ContractParseError(f"{capability} contract validation failed: {exc}") from exc
    raise ContractParseError(f"{capability} has no output contract")