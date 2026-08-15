"""Planner agent — decomposes a task into CXP sub-packets."""

from __future__ import annotations

import json

from ..agent_shell import AgentShell
from ..packet import CXPPacket, PacketType, Payload, RoutingHints


SKILL = open("/skills/planner_v1.md").read() if __import__("os").path.exists("/skills/planner_v1.md") else ""

SYSTEM = """You are a task planner in a distributed AI swarm.
Given a high-level goal, decompose it into 2-5 focused sub-tasks.
Return ONLY a JSON array of objects with these fields:
  type        - one of: code, verify, reflect
  capability  - short slug e.g. "k8s-manifest", "python-code", "security-review"
  goal        - one sentence describing what the sub-task achieves
  instructions - concrete instructions for the worker agent
  priority    - integer 1-5 (5 = urgent)

No prose, no markdown fences, just the raw JSON array.
""" + SKILL


class PlannerAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("planner-1", capabilities=["plan"])

    async def _execute(self, packet: CXPPacket) -> str:
        raw = await self.llm(SYSTEM, f"Goal: {packet.payload.goal}\nContext: {packet.payload.context}")

        # strip accidental markdown fences
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        sub_tasks: list[dict] = json.loads(raw)

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
