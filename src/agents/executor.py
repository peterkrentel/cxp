"""Executor agent — carries out code / artifact generation tasks."""

from __future__ import annotations

from ..agent_shell import AgentShell
from ..packet import CXPPacket, PacketType, Payload, RoutingHints

BASE_SYSTEM = """You are a specialist execution worker in a distributed AI swarm.
You receive a focused task and produce the actual artifact — code, YAML, config, shell script, etc.
Be precise and complete. Output only the artifact with no surrounding explanation unless
the instructions specifically ask for prose.
"""


class ExecutorAgent(AgentShell):
    def __init__(self, agent_id: str = "executor-1", capabilities: list[str] | None = None) -> None:
        # Only subscribe to "code" capability; reject old names like k8s-manifest, python-code
        super().__init__(agent_id, capabilities=capabilities or ["code"])

    async def _execute(self, packet: CXPPacket) -> str:
        # fetched per-task (not at import time) so a reflect update is picked
        # up on the very next task, from every replica, without a pod restart
        skill = await self.get_skill("executor", fallback_path="/skills/executor_v1.md")
        prompt = (
            f"Instructions: {packet.payload.instructions}\n\n"
            f"Goal: {packet.payload.goal}\n\n"
            f"Context:\n{packet.payload.context}"
        )
        output = await self.llm(BASE_SYSTEM + skill, prompt)

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
