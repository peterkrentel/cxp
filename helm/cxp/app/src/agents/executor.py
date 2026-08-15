"""Executor agent — carries out code / artifact generation tasks."""

from __future__ import annotations

import os

from ..agent_shell import AgentShell
from ..packet import CXPPacket, PacketType, Payload, RoutingHints

SKILL = open("/skills/executor_v1.md").read() if os.path.exists("/skills/executor_v1.md") else ""

SYSTEM = """You are a specialist execution worker in a distributed AI swarm.
You receive a focused task and produce the actual artifact — code, YAML, config, shell script, etc.
Be precise and complete. Output only the artifact with no surrounding explanation unless
the instructions specifically ask for prose.
""" + SKILL


class ExecutorAgent(AgentShell):
    def __init__(self, agent_id: str = "executor-1", capabilities: list[str] | None = None) -> None:
        super().__init__(agent_id, capabilities=capabilities or ["code", "k8s-manifest", "python-code", "any"])

    async def _execute(self, packet: CXPPacket) -> str:
        prompt = (
            f"Instructions: {packet.payload.instructions}\n\n"
            f"Goal: {packet.payload.goal}\n\n"
            f"Context:\n{packet.payload.context}"
        )
        output = await self.llm(SYSTEM, prompt)

        # spawn a verify packet automatically
        verify = CXPPacket(
            origin=self.agent_id,
            type=PacketType.VERIFY,
            capability="verify",
            priority=packet.priority,
            task_id=packet.task_id,
            parent_packet_id=packet.id,
            payload=Payload(
                goal=f"Verify: {packet.payload.goal}",
                instructions="Check correctness, completeness, and safety of the artifact below.",
                context=output,
            ),
            routing_hints=RoutingHints(next_type=PacketType.REFLECT),
        )
        verify.append_trace(self.agent_id, "created", "auto-spawned after execution")
        await self.emit_packet(verify)

        return output
