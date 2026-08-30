"""Diagnostician agent — investigates every swarm halt and attaches a
human-readable diagnosis to the halt record. It never clears a halt itself.

Earlier version of this agent auto-cleared a narrow class of plain
network/LLM timeouts without a human. Rolled back deliberately (2026-08-17):
the user wanted awareness, not unilateral resolution -- "once I see the
pattern then you should too... but I'm not sure resolution [is right]."
Diagnostician still does the hard part (gather evidence, recognize a
recurring pattern, write a real diagnosis) but leaves every halt, including
a well-understood recurring one, for a human to actually clear. The real
fix for the specific queue-collision timeouts found live tonight is
`acquire_ollama_slot()`/`release_ollama_slot()` in agent_shell.py -- an
actual root-cause fix, not an auto-resolve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..agent_shell import OLLAMA_URL, TRANSIENT_EXCEPTIONS, AgentShell, strip_code_fence
from ..packet import CXPPacket

log = logging.getLogger(__name__)

OLLAMA_SMALL_URL = os.environ.get("OLLAMA_SMALL_URL", OLLAMA_URL)

# Recognized and surfaced in the diagnosis text, not acted on unilaterally --
# "this keeps happening" is exactly the kind of pattern a human should see,
# not one that gets quietly absorbed forever.
RECURRENCE_WINDOW_SECONDS = 900  # 15 minutes
KV_RECURRENCE_KEY = "diagnostician_timeout_history"

BASE_SYSTEM = """You are a systems diagnostician for a small self-improving AI agent swarm
running in Kubernetes. You are given the reason the swarm just halted, recent
verification score history, and live pod resource usage. Produce:

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
        ollama_request_log = await self._fetch_ollama_request_log()

        if is_timeout_class:
            # No LLM call in this path on purpose: an LLM-authored "is this
            # really transient?" judgment is itself just another call to the
            # same Ollama instance that may currently be the overloaded thing
            # being diagnosed. The diagnosis here is built from hard evidence
            # (pod metrics, Ollama state, recurrence count) instead.
            recent_count = await self._recent_timeout_count()
            recurrence_note = (
                f" This is the {recent_count + 1}th timeout-class halt in the last "
                f"{RECURRENCE_WINDOW_SECONDS // 60} minutes — a recurring pattern, not a one-off."
                if recent_count > 0 else ""
            )
            diagnosis = (
                f"Timeout-class failure ({exc_detail}) from {halt.get('agent')}.{recurrence_note} "
                f"Pod CPU/memory at halt time:\n{pod_metrics}\n"
                f"Ollama instance state:\n{ollama_state}\n"
                f"Ollama's own recent request log (real duration + status per call -- "
                f"check whether the failing request actually succeeded server-side just "
                f"past the client's timeout, like a real incident found live 2026-08-17):\n"
                f"{ollama_request_log}"
            )
            suggested_action = (
                "Recurring resource contention — consider the quantization/model-swap or GPU-serving "
                "roadmap items rather than just resuming each time."
                if recent_count >= 2
                else "Likely transient LLM/network slowness. Safe to Resume once ready."
            )
            await self._think(f"🩺 diagnosis: timeout-class halt{recurrence_note} — awaiting human")
            await self._record_timeout()
            await self._attach_diagnosis({"diagnosis": diagnosis, "suggested_action": suggested_action})
            await self._memory.save()
            return json.dumps({"diagnosis": diagnosis, "suggested_action": suggested_action})

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
            f"Ollama's own recent request log (real duration + status per call):\n{ollama_request_log}\n"
        )
        result = await self._diagnose(prompt, packet)
        await self._think(f"🩺 diagnosis: {result.get('diagnosis', '')[:200]} — awaiting human")
        await self._attach_diagnosis(result)
        await self._memory.save()
        return json.dumps(result)

    async def _recent_timeout_count(self) -> int:
        """How many timeout-class halts have happened in the last
        RECURRENCE_WINDOW_SECONDS -- surfaced in the diagnosis text, never
        used to take an action on its own."""
        kv = await self._kv("cxp-state")
        try:
            entry = await kv.get(KV_RECURRENCE_KEY)
            history = json.loads(entry.value.decode())
        except Exception:
            history = []
        now = time.time()
        return len([ts for ts in history if now - ts < RECURRENCE_WINDOW_SECONDS])

    async def _record_timeout(self) -> None:
        kv = await self._kv("cxp-state")
        now = time.time()
        try:
            entry = await kv.get(KV_RECURRENCE_KEY)
            history = json.loads(entry.value.decode())
        except Exception:
            history = []
        history = [ts for ts in history if now - ts < RECURRENCE_WINDOW_SECONDS] + [now]
        await kv.put(KV_RECURRENCE_KEY, json.dumps(history).encode())

    async def _diagnose(self, prompt: str, packet: CXPPacket) -> dict:
        """Best-effort LLM diagnosis for the non-timeout case. If this call
        itself fails, fall back to a plain "needs human review" verdict
        instead of letting the failure cascade into the generic crash
        handler and become a second, unrelated halt."""
        try:
            raw = strip_code_fence(await self.llm(BASE_SYSTEM, prompt, packet_id=packet.id,
                                                   task_id=packet.task_id, parent_packet_id=packet.parent_packet_id,
                                                   json_mode=True))
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

    async def _fetch_ollama_request_log(self, tail: int = 60) -> str:
        """Ollama's own access log records the *actual* duration and status
        of every request it served -- the exact signal that revealed a real
        request tonight (2026-08-17) actually succeeded server-side in
        59.999922528s, a hair under a 60s client timeout that killed it
        anyway. This automates the manual `kubectl logs` archaeology that
        took a live investigation to piece together the first two times."""
        lines = []
        # Selecting by pod label, not `deploy/name` -- the latter needs
        # kubectl to read the Deployment resource first to resolve which
        # pods it owns, which would need an RBAC grant this agent
        # deliberately doesn't have (read-only on pods/pod-metrics only).
        for label, pod_label in (("main", "app.kubernetes.io/name=ollama"),
                                  ("small", "app.kubernetes.io/name=ollama-small")):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "kubectl", "logs", "-n", "cxp", "-l", pod_label, "--tail", str(tail),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                chat_lines = [line for line in stdout.decode().splitlines() if "/api/chat" in line]
                body = "\n".join(chat_lines[-10:]) or "(no recent /api/chat requests in log tail)"
                lines.append(f"{label}:\n{body}")
            except Exception as exc:
                lines.append(f"{label}: unavailable — {exc!r}")
        return "\n".join(lines)

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
