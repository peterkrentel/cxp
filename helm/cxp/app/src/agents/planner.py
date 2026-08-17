"""Planner agent — decomposes a task into CXP sub-packets."""

from __future__ import annotations

import json
import logging

from ..agent_shell import AgentShell, strip_code_fence
from ..packet import CXPPacket, PacketType, Payload, RoutingHints

log = logging.getLogger(__name__)


def _coerce_str(value: object) -> str:
    """Small models occasionally return a list of strings where a single
    string field was asked for (e.g. instructions as several bullet points)
    -- join rather than let Payload's str-typed field reject the whole
    sub-task with a pydantic ValidationError."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


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
        raw = await self.llm(BASE_SYSTEM + skill, f"Goal: {packet.payload.goal}\nContext: {packet.payload.context}")

        raw = strip_code_fence(raw)
        # strict=False: small local models sometimes emit a literal unescaped
        # control character (e.g. a raw newline) inside a string value —
        # strict JSON rejects that outright, strict=False tolerates it.
        # Doesn't fix every malformation this model produces (missing
        # delimiters, truncated output), just this specific recurring class.
        # A malformed decomposition used to crash _execute() uncaught, which
        # halts the ENTIRE swarm over one goal's LLM hiccup -- verifier
        # already degrades gracefully on the same failure (see its own
        # JSONDecodeError handling); planner should too, rather than being
        # the one agent whose bad output blocks everyone else's work.
        try:
            sub_tasks: list[dict] = json.loads(raw, strict=False)
        except json.JSONDecodeError as e:
            await self.record_validation_failure("decomposition JSON parse", f"{e}\nRaw: {raw[:200]}")
            return f"Failed to decompose task {packet.task_id[:8]}: malformed JSON from model ({e}). No sub-tasks spawned."

        for task in sub_tasks:
            type_str = task.get("type", "code")
            # capability routes to cxp.cap.<capability>, and only code/verify/
            # reflect have a subscribed consumer. task.get(..., "any") used to
            # be the fallback here — "any" has no consumer, so a sub-task
            # missing this field was published successfully and then silently
            # lost forever (no error, no halt, just gone). Falling back to
            # type_str keeps the packet routable; the model is already told
            # capability must match type, so this is the same value it should
            # have provided anyway.
            capability = task.get("capability") or type_str
            if capability not in ("code", "verify", "reflect"):
                capability = "code"
            # PacketType(type_str) below also raises if the model invents a
            # type outside PacketType's enum -- wrapped in the same try so
            # neither that nor any other unexpected shape from this one
            # sub-task can crash the whole decomposition.
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
                    ),
                    routing_hints=RoutingHints(next_type=PacketType.VERIFY),
                )
            except Exception as e:
                await self.record_validation_failure("malformed sub-task", f"{e}\nTask: {task}")
                continue
            child.append_trace(self.agent_id, "created", "spawned by planner")
            await self.emit_packet(child)

        return f"Spawned {len(sub_tasks)} sub-packets for task {packet.task_id[:8]}"
