"""Assessor agent — reads completed artifacts and labels them with AI capability tags."""

from __future__ import annotations

import json

from ..agent_shell import AgentShell
from ..contracts import parse_contract
from ..packet import CXPPacket, PacketType, Payload

SYSTEM = """You are a capability assessor for an AI agent swarm.
Given a task goal and the generated artifact, identify which AI capabilities are demonstrated.

Return ONLY a JSON object:
{
  "labels": [<zero or more labels from the list below, as real values -- never the words "LABEL1"/"LABEL2">],
  "verdict": "<one sentence describing what THIS artifact actually does>",
  "strengths": ["<what it did well>"],
  "gaps": ["<what is missing or weak>"]
}

Available labels:
CODE_GENERATION, ERROR_HANDLING, STRUCTURED_OUTPUT, SECURITY_AWARENESS,
DECOMPOSITION, INFRA_AS_CODE, TESTING, DOCUMENTATION, SELF_IMPROVEMENT

No prose, no fences.
"""

# Confirmed live 2026-08-30 (issue #87): this model repeatedly echoes the
# prompt's own example values verbatim instead of substituting real ones --
# both the old "LABEL1"/"LABEL2" placeholder tokens, and sometimes a real
# but contextually wrong label copied from a previous run's actual answer.
# sanitize_labels() can only guarantee the former never survives (a
# deterministic filter against the real taxonomy) -- it cannot verify a real
# label is actually correct for this artifact, that remains a model-quality
# limit no static check can close.
VALID_LABELS = frozenset({
    "CODE_GENERATION", "ERROR_HANDLING", "STRUCTURED_OUTPUT", "SECURITY_AWARENESS",
    "DECOMPOSITION", "INFRA_AS_CODE", "TESTING", "DOCUMENTATION", "SELF_IMPROVEMENT",
})


def sanitize_labels(labels: list[str] | None) -> list[str]:
    """Drop anything that isn't a real taxonomy member (e.g. the old
    'LABEL1'/'LABEL2' placeholder echo). Order-preserving."""
    if not labels:
        return []
    return [label for label in labels if label in VALID_LABELS]


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
        raw = await self.llm(SYSTEM, prompt, packet_id=packet.id,
                              task_id=packet.task_id, parent_packet_id=packet.parent_packet_id,
                              json_mode=True)

        validation_status = "valid"
        validation_issues: list[str] = []
        try:
            result = parse_contract("assess", raw).model_dump()
        except Exception as e:
            # Catches more than ContractParseError on purpose -- a genuinely
            # unexpected exception here must still degrade gracefully rather
            # than propagate and halt the swarm over one bad response.
            validation_status = "contract_error"
            validation_issues = [str(e)]
            result = {"labels": [], "verdict": raw[:200], "strengths": [], "gaps": []}

        result["labels"] = sanitize_labels(result.get("labels"))

        await self.record_attempt(
            packet=packet,
            capability="assess",
            raw_response=raw,
            normalized_response=json.dumps(result, separators=(",", ":")),
            validation_status=validation_status,
            validation_issues=validation_issues,
            environment_healthy=True,
        )

        # Store assessment as semantic memory
        self._memory.add_semantic(
            f"Assessment [{', '.join(result.get('labels', []))}]: {result.get('verdict', '')}"
        )
        await self._memory.save()

        return json.dumps(result, indent=2)
