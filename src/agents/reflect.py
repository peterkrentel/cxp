"""Reflect agent — self-improvement loop. Rewrites skill files based on failures."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..agent_shell import AgentShell
from ..contracts import SkillRevisionCandidate
from ..candidate_evaluation import resolve_source_attempt
from ..packet import CXPPacket

log = logging.getLogger(__name__)

SKILLS_DIR = Path(os.environ.get("CXP_SKILLS_DIR", "/skills"))
DEFAULT_SKILL_TARGET = "executor"
LEARNABLE_SKILL_TARGETS = {"planner", "executor", "verifier"}

SYSTEM = """You are the self-improvement agent in a distributed AI swarm.
You receive a failure report and the current skill file for an agent.
Your job is to propose an improved skill file that would prevent the reported failure.
Output ONLY the new skill file content — plain text, no JSON, no fences.
Keep it concise (under 300 words). Focus on concrete, actionable guidance.
"""


class ReflectAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("reflect-1", capabilities=["reflect"])

    async def _execute(self, packet: CXPPacket) -> str:
        target_role = packet.payload.inputs.get("target_role", DEFAULT_SKILL_TARGET)
        if target_role not in LEARNABLE_SKILL_TARGETS:
            target_role = DEFAULT_SKILL_TARGET
        source_attempt_id = packet.payload.inputs.get("source_attempt_id") or packet.parent_packet_id or packet.id
        evidence_class = packet.payload.inputs.get("evidence_class", "judgment")
        if evidence_class not in {"contract", "deterministic-validator", "judgment"}:
            evidence_class = "judgment"
        # Match by attempt_id, falling back to task_id -- the same lookup
        # src/candidate_evaluation.py's own health gate uses, since callers
        # (e.g. run_tests.py's improvement_inputs_for_result()) pass a task_id
        # here, not the attempt's own immutable id.
        attempts_by_id = {attempt.get("attempt_id"): attempt for attempt in self._memory.attempts}
        source_attempt = resolve_source_attempt(attempts_by_id, source_attempt_id)
        if source_attempt is not None and not source_attempt.get("environment_healthy", True):
            return f"No candidate created: source attempt {source_attempt_id} was platform-unhealthy."

        # KV is the source of truth for the active baseline; the local
        # ConfigMap-seeded file only matters before the first ever KV write.
        current = await self.get_skill(target_role, fallback_path=str(SKILLS_DIR / f"{target_role}_v1.md"))
        if not current:
            current = "(no existing skill)"
        log.info("[reflect] target=%s has_current=%s instructions=%s",
                  target_role, bool(current), packet.payload.instructions[:80])

        prompt = (
            f"Current skill file:\n{current}\n\n"
            f"Failure report:\n{packet.payload.instructions}\n\n"
            f"Failed artifact context:\n{packet.payload.context[:800]}"
        )
        improved = await self.llm(SYSTEM, prompt, packet_id=packet.id)

        candidate = SkillRevisionCandidate(
            target_role=target_role,
            content=improved,
            source_attempt_id=source_attempt_id,
            rationale=packet.payload.instructions[:500],
            evidence_class=evidence_class,
        )
        revision = await self.put_skill_candidate(packet.id, candidate.model_dump())

        # A candidate is evidence for later evaluation, not a live update.
        self._memory.add_semantic(
            f"Skill candidate {target_role} rev{revision}: {packet.payload.instructions[:120]}"
        )
        await self._memory.save()

        return f"Skill candidate '{target_role}' stored as revision {revision}. Awaiting evaluation and promotion."
