"""Planner agent — decomposes a task into CXP sub-packets."""

from __future__ import annotations

import json
import logging

from opentelemetry.trace import Status, StatusCode

from ..agent_shell import AgentShell
from ..candidate_evaluation import build_self_improvement_inputs
from ..contracts import ContractParseError, PlanResult, PlannedTask, parse_contract
from ..packet import CXPPacket, PacketType, Payload, RoutingHints
from ..telemetry import get_tracer

log = logging.getLogger(__name__)


def _coerce_str(value: object) -> str:
    """Small models occasionally return a list of strings where a single
    string field was asked for (e.g. instructions as several bullet points)
    -- join rather than let Payload's str-typed field reject the whole
    sub-task with a pydantic ValidationError."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


def _planner_contract_issue(plan) -> str | None:
    if len(plan.subtasks) >= 2 and any(task.capability == "verify" for task in plan.subtasks):
        return None
    return (
        f"Planner output invalid: at least 2 sub-tasks are required and at least one must be verify. "
        f"Received {len(plan.subtasks)} sub-task(s): "
        f"{[task.capability for task in plan.subtasks]}"
    )


def _fallback_plan(goal: str, issue: str) -> PlanResult:
    return PlanResult(subtasks=[
        PlannedTask(
            type="code",
            capability="code",
            goal=goal,
            instructions=f"Implement the requested goal. Planner fallback reason: {issue}",
            priority=3,
        ),
        PlannedTask(
            type="verify",
            capability="verify",
            goal=f"Verify implementation for: {goal}",
            instructions="Run an appropriate validation of the code task output and report issues clearly.",
            priority=3,
        ),
    ], source_count=2)


BASE_SYSTEM = """You are a task planner in a distributed AI swarm.
Given a high-level goal, decompose it into 2-5 focused sub-tasks.
Return ONLY a JSON array of objects with these fields:
  type        - one of: code, verify, reflect
  capability  - one of: code, verify, reflect (MUST match type, cannot invent names)
  goal        - one sentence describing what the sub-task achieves
  instructions - concrete instructions for the worker agent
  priority    - integer 1-5 (5 = urgent)

No prose, no markdown fences, just the raw JSON array.
"""


class PlannerAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("planner-1", capabilities=["plan"])

    async def _execute(self, packet: CXPPacket) -> str:
        # fetched per-task (not at import time) so a reflect update is picked
        # up on the very next task, from every replica, without a pod restart
        skill = await self.get_skill("planner", fallback_path="/skills/planner_v1.md")
        raw = await self.llm(BASE_SYSTEM + skill, f"Goal: {packet.payload.goal}\nContext: {packet.payload.context}",
                              packet_id=packet.id, task_id=packet.task_id, parent_packet_id=packet.parent_packet_id,
                              json_mode=True)

        # Own span (not just llm.call, which already ended by the time
        # we're here) so a still-broken parse after the cleanup above
        # shows up as a real ERROR-status span -- llm.call has no way to
        # know its own response failed downstream, so without this the
        # "Error Spans" panel on the OTel dashboard reads 0 forever, even
        # during a run full of malformed decompositions.
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("decomposition.parse") as span:
            span.set_attribute("packet.id", packet.id)
            try:
                plan = parse_contract("plan", raw)
            except ContractParseError as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute("json.parse_error", str(e))
                span.set_attribute("llm.response", raw)
                await self.record_attempt(
                    packet=packet,
                    capability="plan",
                    raw_response=raw,
                    validation_status="contract_error",
                    validation_issues=[str(e)],
                    outcome="contract_error",
                    environment_healthy=True,
                )
                reflect = CXPPacket(
                    origin=self.agent_id,
                    type=PacketType.REFLECT,
                    capability="reflect",
                    priority=3,
                    task_id=packet.task_id,
                    parent_packet_id=packet.id,
                    payload=Payload(
                        goal="Self-improve: repair planner structured output",
                        instructions=f"Plan contract error: {e}",
                        context=raw,
                        inputs=build_self_improvement_inputs(
                            target_role="planner", source_attempt_id=packet.id, evidence_class="contract",
                        ),
                    ),
                )
                await self.emit_packet(reflect)
                await self.record_validation_failure("decomposition JSON parse", f"{e}\nRaw: {raw[:200]}")
                return f"Failed to decompose task {packet.task_id[:8]}: malformed JSON from model ({e}). No sub-tasks spawned."

        for detail in plan.dropped_subtasks:
            await self.record_validation_failure("malformed sub-task", detail)

        contract_issue = _planner_contract_issue(plan)
        if contract_issue:
            raw = await self.llm(
                BASE_SYSTEM + skill,
                f"Goal: {packet.payload.goal}\nContext: {packet.payload.context}\n"
                f"Previous planner output violated the contract: {contract_issue}\n"
                "Return a corrected JSON array with 2-5 sub-tasks, including at least one verify sub-task.",
                packet_id=packet.id,
                task_id=packet.task_id,
                parent_packet_id=packet.parent_packet_id,
                json_mode=True,
            )
            try:
                plan = parse_contract("plan", raw)
            except ContractParseError as e:
                contract_issue = f"Planner retry produced malformed JSON: {e}"
            else:
                for detail in plan.dropped_subtasks:
                    await self.record_validation_failure("malformed sub-task", detail)
                contract_issue = _planner_contract_issue(plan)

        if contract_issue:
            await self.record_validation_failure("planner contract", contract_issue)
            plan = _fallback_plan(packet.payload.goal, contract_issue)

        await self.record_attempt(
            packet=packet,
            capability="plan",
            raw_response=raw,
            normalized_response=plan.model_dump_json(),
            validation_status="valid",
            environment_healthy=True,
        )

        if not plan.subtasks and plan.source_count > 0:
            # A structurally valid parse where every subtask still failed
            # PlannedTask validation is the same missed-learning-signal shape
            # as a hard contract error -- only the JSONDecodeError branch
            # above used to request planner feedback, so this case silently
            # produced zero self-improvement signal.
            detail = f"All {plan.source_count} sub-task(s) failed validation: {'; '.join(plan.dropped_subtasks)}"
            reflect = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="reflect",
                priority=3,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal="Self-improve: repair planner structured output",
                    instructions=f"Plan contract error: {detail}",
                    context=raw,
                    inputs=build_self_improvement_inputs(
                        target_role="planner", source_attempt_id=packet.id, evidence_class="contract",
                    ),
                ),
            )
            await self.emit_packet(reflect)

        for task_model in plan.subtasks:
            task = task_model.model_dump()
            raw_type = task.get("type", "code")
            # capability routes to cxp.cap.<capability>, and only code/verify/
            # reflect have a subscribed consumer. task.get(..., "any") used to
            # be the fallback here — "any" has no consumer, so a sub-task
            # missing this field was published successfully and then silently
            # lost forever (no error, no halt, just gone). Falling back to
            # raw_type keeps the packet routable when capability is absent
            # entirely.
            capability = task.get("capability") or raw_type
            if capability not in ("code", "verify", "reflect"):
                capability = "code"
            # type is derived from the now-validated capability, not trusted
            # as the model's own separate raw "type" field -- found live
            # 2026-08-20: the model emitted capability="code" (correct, and
            # what real routing used -- the executor produced real code,
            # verified at a genuine 0.9) but type="verify" for that same
            # sub-task. Nothing caught the mismatch, since "verify" is a
            # perfectly valid PacketType member -- the try/except below
            # only guards against an invented type outside the enum
            # entirely, not a valid-but-inconsistent one. wait_for_results()
            # keys off packet *type* == "code" to know a task produced an
            # artifact; a type="verify" packet is invisible to that check
            # forever, so a genuinely completed task times out. capability
            # is already clamped to a known-safe 3-value set two lines up;
            # type should always agree with it, not be independently
            # model-supplied.
            type_str = capability
            # The try/except below still guards other unexpected shapes
            # from this one sub-task (e.g. other malformed fields) from
            # crashing the whole decomposition, even though type_str can no
            # longer itself be an invalid PacketType value.
            try:
                child = CXPPacket(
                    origin=self.agent_id,
                    type=PacketType(type_str),
                    capability=capability,
                    priority=int(task.get("priority", 2)),
                    task_id=packet.task_id,
                    parent_packet_id=packet.id,
                    payload=Payload(
                        goal=_coerce_str(task.get("goal", "")),
                        instructions=_coerce_str(task.get("instructions", "")),
                        context=packet.payload.output or packet.payload.context,
                        inputs=dict(packet.payload.inputs),
                    ),
                    routing_hints=RoutingHints(next_type=PacketType.VERIFY),
                )
            except Exception as e:
                await self.record_validation_failure("malformed sub-task", f"{e}\nTask: {task}")
                continue
            child.append_trace(self.agent_id, "created", "spawned by planner")
            await self.emit_packet(child)

        return f"Spawned {len(plan.subtasks)} sub-packets for task {packet.task_id[:8]}"
