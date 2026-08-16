"""Diagnostician agent — investigates a swarm halt and either auto-resolves
it (when the evidence points to transient infra noise) or attaches a
human-readable diagnosis to the halt banner (when it doesn't).

Deliberately conservative: only a halt whose exception type is a plain
network/LLM timeout, AND whose own LLM-produced judgment agrees it looks
transient, gets auto-cleared. Anything else (bad JSON, bad code, any other
exception) is left halted for a human — this preserves the original "stop on
real errors, don't guess" intent while removing the toil of clicking Resume
on the same recurring timeout over and over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from ..agent_shell import AgentShell, OLLAMA_URL, strip_code_fence
from ..packet import CXPPacket

log = logging.getLogger(__name__)

OLLAMA_SMALL_URL = os.environ.get("OLLAMA_SMALL_URL", OLLAMA_URL)

# Exception class names that indicate transient network/LLM slowness rather
# than a code or logic defect — matched against the raw exception detail
# string already captured by AgentShell's halt reason.
TRANSIENT_EXCEPTIONS = ("ReadTimeout", "ConnectTimeout", "ConnectError", "PoolTimeout", "TimeoutError")

BASE_SYSTEM = """You are a systems diagnostician for a small self-improving AI agent swarm
running in Kubernetes. You are given the reason the swarm just halted, recent
verification score history, and live pod resource usage. Decide:

1. A one-paragraph diagnosis of the most likely root cause.
2. Whether this looks like transient infrastructure load (busy CPU, slow LLM
   response) with no evidence of an actual code/logic defect, versus
   something that looks like a real bug a human should look at.
3. A concrete suggested next action.

Return ONLY a JSON object:
{
  "diagnosis": "one paragraph",
  "likely_transient": <true|false>,
  "suggested_action": "one sentence"
}
No prose, no fences.
"""


class DiagnosticianAgent(AgentShell):
    # This agent's entire job is to investigate WHILE the swarm is halted —
    # it must not be subject to the halt-drops-every-packet rule everyone
    # else follows.
    BYPASS_HALT_CHECK = True

    def __init__(self) -> None:
        super().__init__("diagnostician-1", capabilities=["diagnose"])

    async def _execute(self, packet: CXPPacket) -> str:
        halt = await self.is_halted()
        if not halt:
            # Halt already cleared (e.g. by a human) before this packet was
            # picked up — nothing to do.
            return "no active halt, nothing to diagnose"

        exc_detail = packet.payload.instructions
        is_timeout_class = any(name in exc_detail for name in TRANSIENT_EXCEPTIONS)

        recent = self._memory.episodic[-20:]
        recent_scores = [e.get("score") for e in recent if e.get("score") is not None]
        pod_metrics = await self._fetch_pod_metrics()
        ollama_state = await self._fetch_ollama_state()

        prompt = (
            f"Halt reason: {halt.get('reason')}\n"
            f"Agent that failed: {halt.get('agent')}  Task: {halt.get('task_id')}\n"
            f"Raw exception detail: {exc_detail}\n"
            f"Recent verifier scores (last {len(recent_scores)} of 20): {recent_scores}\n"
            f"Pod CPU/memory in cxp namespace (kubectl top pods):\n{pod_metrics}\n"
            f"Ollama instance state (/api/ps, models currently loaded/running):\n{ollama_state}\n"
        )
        raw = strip_code_fence(await self.llm(BASE_SYSTEM, prompt))
        try:
            result = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            result = {
                "diagnosis": raw[:300] or "diagnosis LLM call returned unparseable output",
                "likely_transient": False,
                "suggested_action": "manual review needed — diagnostician's own output was malformed",
            }

        auto_resolve = is_timeout_class and bool(result.get("likely_transient"))

        if auto_resolve:
            await self._think(
                f"🩺 diagnosed as transient ({exc_detail}) — auto-resuming: {result.get('diagnosis', '')[:200]}"
            )
            self._memory.add_semantic(
                f"Auto-resolved halt ({halt.get('reason')}): {result.get('diagnosis', '')[:200]}"
            )
            await self.clear_halt()
        else:
            await self._think(f"🩺 diagnosis: {result.get('diagnosis', '')[:200]} — awaiting human")
            await self._attach_diagnosis(result)

        await self._memory.save()
        return json.dumps({**result, "auto_resolved": auto_resolve})

    async def _attach_diagnosis(self, result: dict) -> None:
        """Write the diagnosis onto the still-active halt KV entry so the
        dashboard's halt banner can show it. No-op if halt was cleared by a
        human in the few seconds this took to run."""
        kv = await self._kv("cxp-state")
        entry = await kv.get("halt")
        current = json.loads(entry.value.decode())
        if not current.get("halted"):
            return
        current["diagnosis"] = result.get("diagnosis")
        current["suggested_action"] = result.get("suggested_action")
        await kv.put("halt", json.dumps(current).encode())

    async def _fetch_pod_metrics(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "top", "pods", "-n", "cxp", "--no-headers",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            return stdout.decode().strip() or f"no metrics returned ({stderr.decode().strip()[:200]})"
        except Exception as exc:
            return f"metrics unavailable: {exc!r}"

    async def _fetch_ollama_state(self) -> str:
        import httpx

        lines = []
        for label, url in (("main", OLLAMA_URL), ("small", OLLAMA_SMALL_URL)):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{url}/api/ps")
                    resp.raise_for_status()
                    lines.append(f"{label} ({url}): {resp.json()}")
            except Exception as exc:
                lines.append(f"{label} ({url}): unavailable — {exc!r}")
        return "\n".join(lines)
