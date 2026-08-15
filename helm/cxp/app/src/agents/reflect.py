"""Reflect agent — self-improvement loop. Rewrites skill files based on failures."""

from __future__ import annotations

import os
from pathlib import Path

from ..agent_shell import AgentShell
from ..packet import CXPPacket

SKILLS_DIR = Path(os.environ.get("CXP_SKILLS_DIR", "/skills"))

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
        # determine which skill file to improve based on context
        skill_file = SKILLS_DIR / "executor_v1.md"
        current = skill_file.read_text() if skill_file.exists() else "(no existing skill)"

        prompt = (
            f"Current skill file:\n{current}\n\n"
            f"Failure report:\n{packet.payload.instructions}\n\n"
            f"Failed artifact context:\n{packet.payload.context[:800]}"
        )
        improved = await self.llm(SYSTEM, prompt)

        # version the old skill before overwriting
        version = self._next_version(skill_file)
        if skill_file.exists():
            skill_file.rename(SKILLS_DIR / f"executor_v{version - 1}.md.bak")

        skill_file.write_text(improved)

        # record the improvement as a semantic fact
        self._memory.add_semantic(
            f"Skill executor updated to v{version}: {packet.payload.instructions[:120]}"
        )
        await self._memory.save()

        return f"Skill file updated to version {version}. Changes committed."

    def _next_version(self, path: Path) -> int:
        baks = list(path.parent.glob("executor_v*.md.bak"))
        return len(baks) + 2
