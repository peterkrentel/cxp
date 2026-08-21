"""Verifier agent — grades executor output and routes next steps."""

from __future__ import annotations

import json
import logging

from ..agent_shell import AgentShell, strip_code_fence
from ..packet import CXPPacket, PacketType, Payload, RoutingHints

log = logging.getLogger(__name__)

BASE_SYSTEM = """You are a verification specialist in a distributed AI swarm.
Given an artifact and its original goal, evaluate:
1. Correctness — does it achieve the goal?
2. Completeness — is anything missing?
3. Safety — are there obvious security or reliability issues?

Return ONLY a JSON object:
{
  "score": <float 0.0-1.0>,
  "passed": <true|false>,
  "issues": ["issue1", "issue2"],
  "suggestion": "one sentence on biggest improvement if failed"
}
No prose, no fences.
"""


class VerifierAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("verifier-1", capabilities=["verify"])

    async def _execute(self, packet: CXPPacket) -> str:
        skill = await self.get_skill("verifier", fallback_path="/skills/verifier_v1.md")
        prompt = (
            f"Original goal: {packet.payload.goal}\n\n"
            f"Artifact to verify:\n{packet.payload.context}"
        )
        raw = await self.llm(BASE_SYSTEM + skill, prompt, packet_id=packet.id)
        raw = strip_code_fence(raw)

        # Better error handling for JSON parsing. strict=False: tolerate a
        # literal unescaped control character in a string value, which small
        # local models occasionally emit.
        try:
            result = json.loads(raw, strict=False)
        except json.JSONDecodeError as e:
            await self.record_validation_failure("verify response JSON parse", f"{e}\nRaw: {raw[:200]}")
            # Default to fail if we can't parse
            result = {"score": 0.0, "passed": False, "issues": [f"Parse error: {e}"], "suggestion": "Malformed response"}

        packet.quality_score = float(result.get("score") if result.get("score") is not None else 0.5)

        # Record which skill revision produced this artifact and how it
        # scored — the only way to actually measure whether reflect's
        # rewrites are improving output over time, instead of assuming so.
        self._memory.add_episodic({
            "capability": packet.payload.inputs.get("capability", "code"),
            "skill_revision": packet.payload.inputs.get("skill_revision"),
            "score": packet.quality_score,
            "goal": packet.payload.goal,
        })
        await self._memory.save()

        if not result.get("passed", False):
            # spawn a reflect packet so the system can learn
            reflect = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="reflect",
                priority=3,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal="Self-improve: update skill based on failed verification",
                    instructions=(
                        f"Issues found: {result.get('issues', [])}\n"
                        f"Suggestion: {result.get('suggestion', '')}\n"
                        "Propose a one-paragraph update to the executor skill file to prevent this."
                    ),
                    context=packet.payload.context,
                ),
            )
            reflect.append_trace(self.agent_id, "created", "spawned due to failed verification")
            await self.emit_packet(reflect)

        issues = result.get("issues", [])

        # Spawn assess packet for capability labeling
        assess = CXPPacket(
            origin=self.agent_id,
            type=PacketType.REFLECT,
            capability="assess",
            priority=1,
            task_id=packet.task_id,
            parent_packet_id=packet.id,
            payload=Payload(
                goal=packet.payload.goal,
                instructions="Label this artifact with AI capability tags",
                context=packet.payload.context,
            ),
        )
        log.info(f"Emitting assess packet {assess.id[:8]}")
        await self.emit_packet(assess)

        # Spawn deploy packet if score is high enough
        if packet.quality_score is not None and packet.quality_score >= 0.85:
            deploy = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="deploy",
                priority=2,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal=packet.payload.goal,
                    instructions=str(packet.quality_score),
                    context=packet.payload.context,
                ),
            )
            log.info(f"Emitting deploy packet {deploy.id[:8]} (score={packet.quality_score})")
            await self.emit_packet(deploy)

        return json.dumps({"score": packet.quality_score, "passed": result.get("passed"), "issues": issues})
