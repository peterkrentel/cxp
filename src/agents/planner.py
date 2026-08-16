"""Planner agent — decomposes a task into CXP sub-packets."""

from __future__ import annotations

import json

from ..agent_shell import AgentShell, strip_code_fence
from ..packet import CXPPacket, PacketType, Payload, RoutingHints


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
        sub_tasks: list[dict] = json.loads(raw, strict=False)

        for task in sub_tasks:
            child = CXPPacket(
                origin=self.agent_id,
                type=PacketType(task.get("type", "code")),
                capability=task.get("capability", "any"),
                priority=int(task.get("priority", 2)),
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal=task.get("goal", ""),
                    instructions=task.get("instructions", ""),
                    context=packet.payload.output or packet.payload.context,
                ),
                routing_hints=RoutingHints(next_type=PacketType.VERIFY),
            )
            child.append_trace(self.agent_id, "created", "spawned by planner")
            await self.emit_packet(child)

        return f"Spawned {len(sub_tasks)} sub-packets for task {packet.task_id[:8]}"
