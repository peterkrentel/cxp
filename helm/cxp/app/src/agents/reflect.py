"""Reflect agent — self-improvement loop. Rewrites skill files based on failures."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..agent_shell import AgentShell
from ..packet import CXPPacket

log = logging.getLogger(__name__)

SKILLS_DIR = Path(os.environ.get("CXP_SKILLS_DIR", "/skills"))
SKILL_TARGET = "executor"  # the only skill reflect currently maintains

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
        # KV is the source of truth (visible to every replica); the local
        # ConfigMap-seeded file only matters before the first ever KV write.
        current = await self.get_skill(SKILL_TARGET, fallback_path=str(SKILLS_DIR / f"{SKILL_TARGET}_v1.md"))
        if not current:
            current = "(no existing skill)"
        log.info("[reflect] target=%s has_current=%s instructions=%s",
                  SKILL_TARGET, bool(current), packet.payload.instructions[:80])

        prompt = (
            f"Current skill file:\n{current}\n\n"
            f"Failure report:\n{packet.payload.instructions}\n\n"
            f"Failed artifact context:\n{packet.payload.context[:800]}"
        )
        improved = await self.llm(SYSTEM, prompt)

        # JetStream KV versions every put() with an atomic revision number —
        # no separate .bak/glob bookkeeping needed, and no race between
        # concurrent reflect runs computing the same "next version".
        revision = await self.put_skill(SKILL_TARGET, improved)

        # record the improvement as a semantic fact
        self._memory.add_semantic(
            f"Skill {SKILL_TARGET} updated to rev{revision}: {packet.payload.instructions[:120]}"
        )
        await self._memory.save()

        return f"Skill '{SKILL_TARGET}' updated to revision {revision}. Live for all replicas."
