"""Executor agent — carries out code / artifact generation tasks."""

from __future__ import annotations

from ..agent_shell import AgentShell
from ..contracts import parse_contract
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
        candidate_id = packet.payload.inputs.get("candidate_id")
        candidate = await self.get_skill_candidate(candidate_id) if candidate_id else None
        if candidate and candidate.get("target_role") == "executor":
            skill = candidate.get("content", "")
            skill_revision = candidate_id
        else:
            # Fetched per-task (not at import time) so active revisions are
            # visible to every replica without a pod restart.
            skill, skill_revision = await self.get_skill_with_revision(
                "executor", fallback_path="/skills/executor_v1.md"
            )
        prompt = (
            f"Instructions: {packet.payload.instructions}\n\n"
            f"Goal: {packet.payload.goal}\n\n"
            f"Context:\n{packet.payload.context}"
        )
        raw_output = await self.llm(BASE_SYSTEM + skill, prompt, packet_id=packet.id,
                                     task_id=packet.task_id, parent_packet_id=packet.parent_packet_id)
        artifact = parse_contract("code", raw_output)
        output = artifact.content
        await self.record_attempt(
            packet=packet,
            capability="code",
            raw_response=raw_output,
            normalized_response=output,
            validation_status="valid",
            environment_healthy=True,
            skill_revision=skill_revision,
        )

        # spawn a verify packet automatically — skill_revision rides along in
        # `inputs` so verifier can log which skill version produced this
        # artifact, letting improvement over reflect updates be measured
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
                inputs={**packet.payload.inputs, "skill_revision": skill_revision, "capability": "code"},
            ),
            routing_hints=RoutingHints(next_type=PacketType.REFLECT),
        )
        verify.append_trace(self.agent_id, "created", "auto-spawned after execution")
        await self.emit_packet(verify)

        return output
