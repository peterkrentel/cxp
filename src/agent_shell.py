"""Agent shell — base class for all CXP agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator

import nats
from nats.aio.client import Client as NATSClient

from .memory import get_store
from .packet import CXPPacket, PacketStatus, PacketType

log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

# Subject constants
SUBJECT_PACKETS = "cxp.packets"          # broadcast new packets
SUBJECT_RESULTS = "cxp.results"          # completed / failed packets
SUBJECT_DASHBOARD = "cxp.dashboard"      # status events for the dashboard


class AgentShell(ABC):
    """Deterministic wrapper around a non-deterministic LLM worker."""

    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id = agent_id
        self.capabilities = set(capabilities)
        self._nc: NATSClient | None = None
        self._memory = get_store()

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                 #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        self._nc = await nats.connect(NATS_URL)
        log.info("[%s] connected to NATS at %s", self.agent_id, NATS_URL)

    async def disconnect(self) -> None:
        if self._nc:
            await self._nc.drain()

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        await self.connect()
        sub = await self._nc.subscribe(SUBJECT_PACKETS, queue=self.agent_id)
        log.info("[%s] listening for packets (caps: %s)", self.agent_id, self.capabilities)
        await self._emit_status("online")

        async for msg in sub.messages:
            try:
                packet = CXPPacket.model_validate_json(msg.data)
            except Exception as exc:
                log.warning("[%s] bad packet: %s", self.agent_id, exc)
                continue

            if not self._should_handle(packet):
                continue

            packet.claim(self.agent_id)
            await self._emit_status("working", packet)

            try:
                output = await self._execute(packet)
                packet.complete(self.agent_id, output)
                self._memory.record_success(self.agent_id, packet.capability)
                await self._memory.save()
                log.info("[%s] ✓ packet %s", self.agent_id, packet.id[:8])
            except Exception as exc:
                packet.fail(self.agent_id, str(exc))
                self._memory.record_failure(self.agent_id, packet.capability)
                await self._memory.save()
                log.error("[%s] ✗ packet %s: %s", self.agent_id, packet.id[:8], exc)

            await self._publish(SUBJECT_RESULTS, packet)
            await self._emit_status("idle")

    # ------------------------------------------------------------------ #
    # Override this in subclasses                                          #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def _execute(self, packet: CXPPacket) -> str:
        """Process the packet and return the output string."""

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _should_handle(self, packet: CXPPacket) -> bool:
        if packet.status != PacketStatus.PENDING:
            return False
        if packet.target not in ("any", self.agent_id):
            return False
        if packet.capability not in self.capabilities and "any" not in self.capabilities:
            return False
        return True

    async def _publish(self, subject: str, packet: CXPPacket) -> None:
        await self._nc.publish(subject, packet.model_dump_json().encode())

    async def emit_packet(self, packet: CXPPacket) -> None:
        """Publish a new packet to the swarm."""
        await self._publish(SUBJECT_PACKETS, packet)

    async def _emit_status(self, state: str, packet: CXPPacket | None = None) -> None:
        payload = {
            "agent": self.agent_id,
            "state": state,
            "packet_id": packet.id[:8] if packet else None,
            "packet_type": packet.type if packet else None,
        }
        await self._nc.publish(SUBJECT_DASHBOARD, json.dumps(payload).encode())

    async def llm(self, system: str, user: str) -> str:
        """Call the local Ollama LLM and return the response text."""
        import httpx

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
