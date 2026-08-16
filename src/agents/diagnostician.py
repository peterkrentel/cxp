"""Diagnostician agent — investigates a swarm halt and either auto-resolves
it (when the failure is a plain network/LLM timeout) or attaches a
human-readable diagnosis to the halt banner (when it's anything else).

Deliberately conservative on WHAT auto-resolves (only a known timeout-class
exception name — bad JSON, bad code, any other exception stays halted for a
human), but deliberately does NOT gate that decision behind an LLM call: an
LLM-authored "is this really transient?" judgment is itself just another
call to the same Ollama instance that may currently be the overloaded thing
being diagnosed. For the timeout case, resource signals (kubectl top,
Ollama's own /api/ps) are gathered as supporting evidence and logged, but
the resolve decision is made from the exception type alone. The LLM is only
invoked for the harder, non-timeout case, where a written diagnosis is
actually worth producing for a human to read.
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
running in Kubernetes. You are given the reason the swarm just halted (already
known to NOT be a plain network/LLM timeout — those are handled separately),
recent verification score history, and live pod resource usage. Produce:

1. A one-paragraph diagnosis of the most likely root cause.
2. A concrete suggested next action for the human who will read this.

Return ONLY a JSON object:
{
  "diagnosis": "one paragraph",
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
        pod_metrics = await self._fetch_pod_metrics()
        ollama_state = await self._fetch_ollama_state()

        if is_timeout_class:
            # No LLM call in this path on purpose — see module docstring.
            # The exception type alone is the resolve signal; resource
            # metrics are gathered as supporting evidence for the record,
            # not as a gate.
            diagnosis = (
                f"Timeout-class failure ({exc_detail}) from {halt.get('agent')}. "
                f"Pod CPU/memory at halt time:\n{pod_metrics}\n"
                f"Ollama instance state:\n{ollama_state}"
            )
            await self._think(f"🩺 timeout-class halt — auto-resuming: {exc_detail}")
            self._memory.add_semantic(f"Auto-resolved halt ({halt.get('reason')}): {diagnosis[:300]}")
            await self.clear_halt()
            await self._memory.save()
            return json.dumps({"diagnosis": diagnosis, "likely_transient": True, "auto_resolved": True})

        # Non-timeout failure (bad JSON, bad code, anything else): worth an
        # actual LLM-authored diagnosis for a human to read. Ollama being
        # reachable is a fair assumption here since the ORIGINAL failure
        # wasn't itself a timeout.
        recent = self._memory.episodic[-20:]
        recent_scores = [e.get("score") for e in recent if e.get("score") is not None]
        prompt = (
            f"Halt reason: {halt.get('reason')}\n"
            f"Agent that failed: {halt.get('agent')}  Task: {halt.get('task_id')}\n"
            f"Raw exception detail: {exc_detail}\n"
            f"Recent verifier scores (last {len(recent_scores)} of 20): {recent_scores}\n"
            f"Pod CPU/memory in cxp namespace (kubectl top pods):\n{pod_metrics}\n"
            f"Ollama instance state (/api/ps, models currently loaded/running):\n{ollama_state}\n"
        )
        result = await self._diagnose(prompt)
        await self._think(f"🩺 diagnosis: {result.get('diagnosis', '')[:200]} — awaiting human")
        await self._attach_diagnosis(result)
        await self._memory.save()
        return json.dumps({**result, "auto_resolved": False})

    async def _diagnose(self, prompt: str) -> dict:
        """Best-effort LLM diagnosis for the non-timeout case. If this call
        itself fails, fall back to a plain "needs human review" verdict
        instead of letting the failure cascade into the generic crash
        handler and become a second, unrelated halt."""
        try:
            raw = strip_code_fence(await self.llm(BASE_SYSTEM, prompt))
            return json.loads(raw, strict=False)
        except json.JSONDecodeError as exc:
            return {
                "diagnosis": f"diagnosis model returned unparseable output: {exc}",
                "suggested_action": "manual review needed — diagnostician's own output was malformed",
            }
        except Exception as exc:
            return {
                "diagnosis": f"diagnostician's own LLM call failed ({exc!r}) while diagnosing a non-timeout failure.",
                "suggested_action": "manual review needed — diagnostician's reasoning step failed",
            }

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
