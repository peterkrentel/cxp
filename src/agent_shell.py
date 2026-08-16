"""Agent shell — base class for all CXP agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import nats
from nats.aio.client import Client as NATSClient
from nats.js import api
from nats.js.errors import BadRequestError, NotFoundError

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

KV_SKILLS = "cxp-skills"  # bucket: skill file text, shared across all replicas
KV_STATE  = "cxp-state"   # bucket: swarm-wide control state (e.g. halt flag)

# Work-item packets (cxp.cap.*) get a durable JetStream stream so a packet
# published while no replica happens to be subscribed (mid-rollout, pod
# restart) isn't silently lost — it's redelivered once a consumer is back.
# Results/dashboard/thinking stay plain pub/sub: they're fan-out status/log
# events for live viewers, not single-consumer work items.
STREAM_PACKETS = "CXP_PACKETS"
STREAM_PACKET_SUBJECTS = ["cxp.cap.>"]


def strip_code_fence(text: str) -> str:
    """Extract code from the first ```lang ... ``` fence, ignoring trailing prose.

    Line-based on purpose: str.lstrip/rstrip take a *character set*, not a
    literal prefix, so "```json".lstrip works by accident and mis-strips any
    text that happens to start with one of those characters after the fence.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "```":
            end = i
            break
    return "\n".join(lines[1:end]).strip() if end is not None else text


class AgentShell(ABC):
    """Deterministic wrapper around a non-deterministic LLM worker."""

    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id   = agent_id
        self.capabilities = set(capabilities)
        self._nc: NATSClient | None = None
        self._memory = get_store()
        self._kv_cache: dict[str, object] = {}

    async def connect(self) -> None:
        self._nc = await nats.connect(NATS_URL)
        js = self._nc.jetstream()
        try:
            await js.add_stream(name=STREAM_PACKETS, subjects=STREAM_PACKET_SUBJECTS)
        except BadRequestError:
            pass  # another replica already created it with the same config
        log.info("[%s] connected to NATS", self.agent_id)

    async def _kv(self, bucket: str):
        """Get-or-create a JetStream KV bucket, cached per agent instance."""
        if bucket not in self._kv_cache:
            js = self._nc.jetstream()
            try:
                self._kv_cache[bucket] = await js.key_value(bucket)
            except NotFoundError:
                try:
                    self._kv_cache[bucket] = await js.create_key_value(bucket=bucket)
                except BadRequestError:
                    # lost a create race against another replica — it exists now
                    self._kv_cache[bucket] = await js.key_value(bucket)
        return self._kv_cache[bucket]

    async def get_skill(self, name: str, fallback_path: str | None = None) -> str:
        """Read the current skill text for `name`, shared across all replicas.

        Falls back to the ConfigMap-seeded local file only if the KV bucket
        has no entry yet (i.e. reflect hasn't written an update since deploy).
        """
        content, _ = await self.get_skill_with_revision(name, fallback_path)
        return content

    async def get_skill_with_revision(self, name: str, fallback_path: str | None = None) -> tuple[str, int | None]:
        """Like get_skill, but also returns the KV revision (None if reading
        from the fallback file) — lets callers tag output with which skill
        version produced it, so improvement over time can actually be measured."""
        try:
            kv = await self._kv(KV_SKILLS)
            entry = await kv.get(name)
            return entry.value.decode(), entry.revision
        except Exception:
            if fallback_path and os.path.exists(fallback_path):
                return open(fallback_path).read(), None
            return "", None

    async def put_skill(self, name: str, content: str) -> int:
        """Write an updated skill file, visible to every replica on next read."""
        kv = await self._kv(KV_SKILLS)
        return await kv.put(name, content.encode())

    async def is_halted(self) -> dict | None:
        """Return the halt record if the swarm is currently paused, else None."""
        try:
            kv = await self._kv(KV_STATE)
            entry = await kv.get("halt")
            data = json.loads(entry.value.decode())
            return data if data.get("halted") else None
        except Exception:
            return None

    async def set_halt(self, reason: str, task_id: str = "") -> None:
        """Pause swarm-wide packet intake after an unhandled agent error."""
        kv = await self._kv(KV_STATE)
        payload = json.dumps({
            "halted": True,
            "reason": reason,
            "agent": self.agent_id,
            "task_id": task_id,
            "since": datetime.now(timezone.utc).isoformat(),
        })
        await kv.put("halt", payload.encode())

    async def clear_halt(self) -> None:
        kv = await self._kv(KV_STATE)
        await kv.put("halt", json.dumps({"halted": False}).encode())

    async def disconnect(self) -> None:
        if self._nc:
            await self._nc.drain()

    async def run(self) -> None:
        await self.connect()

        async def handle(msg):
            # Every exit path below acks — redelivery here exists only to
            # rescue a packet whose delivery attempt never finished (pod
            # died mid-handling before this line ran), not to retry
            # business-logic failures. Those are already the halt gate's
            # job, and a failed packet redelivering forever would just spam
            # the same error into the halt reason.
            try:
                packet = CXPPacket.model_validate_json(msg.data)
            except Exception as exc:
                log.warning("[%s] bad packet: %s", self.agent_id, exc)
                await msg.ack()
                return

            if packet.status != PacketStatus.PENDING:
                await msg.ack()
                return

            halt = await self.is_halted()
            if halt:
                log.warning("[%s] swarm halted (%s) — dropping packet %s",
                            self.agent_id, halt.get("reason"), packet.id[:8])
                await msg.ack()
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
                # str(exc) is empty for some exception types (e.g. httpx's
                # timeout errors) — fall back to repr() so the halt reason
                # and logs always name at least the exception class.
                detail = str(exc) or repr(exc)
                packet.fail(self.agent_id, detail)
                self._memory.record_failure(self.agent_id, packet.capability)
                await self._memory.save()
                reason = f"{self.agent_id} failed on {packet.capability} ({packet.id[:8]}): {detail}"
                await self.set_halt(reason, task_id=packet.task_id)
                await self._think(f"✗ ERROR: {packet.id[:8]} — {detail} — swarm halted, awaiting human")
                log.error("[%s] ✗ %s: %s — swarm halted", self.agent_id, packet.id[:8], detail, exc_info=True)

            await self._publish(SUBJECT_RESULTS, packet)
            await self._emit_status("idle")
            await msg.ack()

        # Each capability gets its own capability-routed subject with a durable
        # JetStream consumer — `durable` + `queue` gives the same "replicas
        # compete, not duplicate" behavior as a core queue group, but with
        # redelivery: a packet published while no replica is up (mid-rollout,
        # restart) sits in the stream instead of vanishing, and is handed to
        # whichever replica comes back first.
        for cap in self.capabilities:
            subject = f"cxp.cap.{cap}"
            durable = f"cxp-{cap}"
            js = self._nc.jetstream()
            # ack_wait must exceed the slowest legitimate _execute() call, or
            # JetStream redelivers a message to another replica while the
            # first is still (slowly) processing it — not lost, but
            # processed twice. Default is 30s; LLM calls under contention
            # have taken 3-4 minutes, so this needs real headroom.
            config = api.ConsumerConfig(ack_wait=300)
            await js.subscribe(subject, durable=durable, queue=durable, cb=handle,
                                manual_ack=True, config=config)
            log.info("[%s] listening on %s (durable=%s, ack_wait=300s)", self.agent_id, subject, durable)

        await self._emit_status("idle")
        while True:
            await asyncio.sleep(1)

    @abstractmethod
    async def _execute(self, packet: CXPPacket) -> str:
        """Process the packet and return the output string."""

    async def emit_packet(self, packet: CXPPacket) -> None:
        """Route a new packet to the correct capability subject, via
        JetStream so the publish is confirmed stored before returning."""
        subject = f"cxp.cap.{packet.capability}"
        js = self._nc.jetstream()
        await js.publish(subject, packet.model_dump_json().encode())

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
        """Call Ollama with streaming. Auto-pull is disabled (see below) — a
        missing model raises RuntimeError rather than silently pulling."""
        import httpx

        # connect=10s, first-token=30s, between-tokens=60s
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

        await self._think(f"  ⟳ LLM ({len(user)} chars): {user[:120]}…")

        async def _stream(client: httpx.AsyncClient) -> str:
            chunks: list[str] = []
            total_len = 0
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
                            total_len += len(token)
                            if total_len % 30 == 0:
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
