"""Assessor agent — reads completed artifacts and labels them with AI capability tags."""

from __future__ import annotations

import json

from ..agent_shell import AgentShell, strip_code_fence
from ..packet import CXPPacket, PacketType, Payload

SYSTEM = """You are a capability assessor for an AI agent swarm.
Given a task goal and the generated artifact, identify which AI capabilities are demonstrated.

Return ONLY a JSON object:
{
  "labels": ["LABEL1", "LABEL2"],
  "verdict": "one sentence summary",
  "strengths": ["what it did well"],
  "gaps": ["what is missing or weak"]
}

Available labels:
CODE_GENERATION, ERROR_HANDLING, STRUCTURED_OUTPUT, SECURITY_AWARENESS,
DECOMPOSITION, INFRA_AS_CODE, TESTING, DOCUMENTATION, SELF_IMPROVEMENT

No prose, no fences.
"""


class AssessorAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("assessor-1", capabilities=["assess"])

    async def _execute(self, packet: CXPPacket) -> str:
        goal = packet.payload.goal or ""
        artifact = packet.payload.context or ""

        prompt = (
            f"Task goal: {goal}\n\n"
            f"Generated artifact:\n{artifact[:2000]}"
        )
        raw = await self.llm(SYSTEM, prompt)
        raw = strip_code_fence(raw)

        try:
            result = json.loads(raw)
        except Exception:
            result = {"labels": [], "verdict": raw[:200], "strengths": [], "gaps": []}

        # Store assessment as semantic memory
        self._memory.add_semantic(
            f"Assessment [{', '.join(result.get('labels', []))}]: {result.get('verdict', '')}"
        )
        await self._memory.save()

        return json.dumps(result, indent=2)
