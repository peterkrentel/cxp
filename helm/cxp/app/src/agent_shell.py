"""Agent shell — base class for all CXP agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod

import nats
from nats.aio.client import Client as NATSClient

from .memory import get_store
from .packet import CXPPacket, PacketStatus, PacketType

log = logging.getLogger(__name__)

NATS_URL     = os.environ.get("NATS_URL",     "nats://localhost:4222")
OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

SUBJECT_PACKETS   = "cxp.packets"    # legacy broadcast (used by submit)
SUBJECT_RESULTS   = "cxp.results"    # completed / failed packets
SUBJECT_DASHBOARD = "cxp.dashboard"  # agent status events
SUBJECT_THINKING  = "cxp.thinking"   # LLM stream + agent reasoning


class AgentShell(ABC):
    """Deterministic wrapper around a non-deterministic LLM worker."""

    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id   = agent_id
        self.capabilities = set(capabilities)
        self._nc: NATSClient | None = None
        self._memory = get_store()

    async def connect(self) -> None:
        self._nc = await nats.connect(NATS_URL)
        log.info("[%s] connected to NATS", self.agent_id)

    async def disconnect(self) -> None:
        if self._nc:
            await self._nc.drain()

    async def run(self) -> None:
        await self.connect()

        async def handle(msg):
            try:
                packet = CXPPacket.model_validate_json(msg.data)
            except Exception as exc:
                log.warning("[%s] bad packet: %s", self.agent_id, exc)
                return

            if packet.status != PacketStatus.PENDING:
                return

            packet.claim(self.agent_id)
            await self._emit_status("working", packet)

            try:
                await self._think(f"▶ TASK: {packet.payload.goal}")
                await self._think(f"  cap={packet.capability}  id={packet.id[:8]}")
                output = await self._execute(packet)
                packet.complete(self.agent_id, output)
                self._memory.record_success(self.agent_id, packet.capability)
                await self._memory.save()
                await self._think(f"✓ DONE: {packet.id[:8]} — {len(output)} chars")
                log.info("[%s] ✓ %s", self.agent_id, packet.id[:8])
            except Exception as exc:
                packet.fail(self.agent_id, str(exc))
                self._memory.record_failure(self.agent_id, packet.capability)
                await self._memory.save()
                await self._think(f"✗ ERROR: {packet.id[:8]} — {exc}")
                log.error("[%s] ✗ %s: %s", self.agent_id, packet.id[:8], exc)

            await self._publish(SUBJECT_RESULTS, packet)
            await self._emit_status("idle")

        # Each capability gets its own capability-routed subject with a shared
        # queue group so multiple replicas of the same agent type compete (not duplicate).
        for cap in self.capabilities:
            subject     = f"cxp.cap.{cap}"
            queue_group = f"cxp-{cap}"
            await self._nc.subscribe(subject, queue=queue_group, cb=handle)
            log.info("[%s] listening on %s (queue=%s)", self.agent_id, subject, queue_group)

        await self._emit_status("idle")
        while True:
            await asyncio.sleep(1)

    @abstractmethod
    async def _execute(self, packet: CXPPacket) -> str:
        """Process the packet and return the output string."""

    async def emit_packet(self, packet: CXPPacket) -> None:
        """Route a new packet to the correct capability subject."""
        subject = f"cxp.cap.{packet.capability}"
        await self._nc.publish(subject, packet.model_dump_json().encode())

    async def _publish(self, subject: str, packet: CXPPacket) -> None:
        await self._nc.publish(subject, packet.model_dump_json().encode())

    async def _emit_status(self, state: str, packet: CXPPacket | None = None) -> None:
        payload = {
            "agent":       self.agent_id,
            "state":       state,
            "packet_id":   packet.id[:8] if packet else None,
            "packet_type": packet.type.value if packet else None,
        }
        await self._nc.publish(SUBJECT_DASHBOARD, json.dumps(payload).encode())

    async def _think(self, text: str) -> None:
        await self._nc.publish(SUBJECT_THINKING,
            json.dumps({"agent": self.agent_id, "text": text}).encode())

    async def llm(self, system: str, user: str) -> str:
        """Call Ollama with streaming; auto-pull model if not present."""
        import httpx

        # connect=10s, first-token=30s, between-tokens=60s
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

        await self._think(f"  ⟳ LLM ({len(user)} chars): {user[:120]}…")

        async def _stream(client: httpx.AsyncClient) -> str:
            chunks: list[str] = []
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL, "stream": True,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        token = json.loads(line).get("message", {}).get("content", "")
                        if token:
                            chunks.append(token)
                            if len("".join(chunks)) % 30 == 0:
                                await self._nc.publish(SUBJECT_THINKING,
                                    json.dumps({"agent": self.agent_id, "text": token, "stream": True}).encode())
                    except Exception:
                        continue
            return "".join(chunks)

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                full = await _stream(client)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise RuntimeError(
                        f"Model '{OLLAMA_MODEL}' not found in Ollama. "
                        f"Available models must be pre-cached in PVC. "
                        f"Auto-pull is disabled to prevent PostStartHook failures."
                    )
                raise

        await self._think(f"  ✓ response ({len(full)} chars): {full[:120]}…")
        return full
