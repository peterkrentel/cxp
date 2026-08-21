"""Agent shell — base class for all CXP agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import nats
from nats.aio.client import Client as NATSClient
from nats.js import api
from nats.js.errors import BadRequestError, NotFoundError

from .memory import get_store
from .packet import CXPPacket, PacketStatus, PacketType, Payload
from .telemetry import get_tracer, record_llm_call

log = logging.getLogger(__name__)

NATS_URL     = os.environ.get("NATS_URL",     "nats://localhost:4222")
OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

SUBJECT_PACKETS   = "cxp.packets"    # legacy broadcast (used by submit)
SUBJECT_RESULTS   = "cxp.results"    # completed / failed packets
SUBJECT_DASHBOARD = "cxp.dashboard"  # agent status events
SUBJECT_THINKING  = "cxp.thinking"   # LLM stream + agent reasoning

KV_SKILLS = "cxp-skills"        # bucket: skill file text, shared across all replicas
KV_STATE  = "cxp-state"         # bucket: swarm-wide control state (e.g. halt flag)
KV_OLLAMA_SLOTS = "cxp-ollama-slots"  # bucket: which Ollama instances have a request
                                      # actually in flight right now, cross-pod
KV_INFLIGHT = "cxp-inflight"    # bucket: packet ids currently being executed --
                                # an idempotency fence so a JetStream redelivery
                                # of a packet another replica is still (or was
                                # already) working on becomes a harmless no-op
                                # ack instead of a second real execution.

# A slot claim older than this is treated as abandoned (its holder crashed or
# got killed without releasing) and gets pruned on the next acquire attempt,
# rather than permanently shrinking that instance's real capacity. Must stay
# above LLM_TOTAL_TIMEOUT (900s, in llm() below) so a legitimately-still-
# running call is never mistaken for an abandoned one -- raised alongside it
# when the heartbeat mechanism let that ceiling go from 240s to 900s.
OLLAMA_SLOT_STALE_SECONDS = 960
OLLAMA_SLOT_POLL_SECONDS = 1.0

# How long an in-flight claim is honored before it's assumed abandoned (holder
# crashed without releasing it) rather than genuinely still running. Must
# stay >= LLM_TOTAL_TIMEOUT (900s) for the same reason as OLLAMA_SLOT_STALE_
# SECONDS above -- a legitimately-still-running call must never be mistaken
# for an abandoned one and get its result silently discarded by a second
# worker that thinks it's now free to take over.
INFLIGHT_STALE_SECONDS = 960

# How long a "done" claim marker is kept around before it's eligible for
# pruning -- long enough to catch a late redelivery (observed live
# 2026-08-17 landing ~960-1080s after the original completion, triggered
# by an ack() that apparently didn't register), short enough that this
# bucket doesn't grow forever across the swarm's lifetime.
DONE_CLAIM_RETENTION_SECONDS = 7200

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


class _NDJSONReassembler:
    """Reassembles Ollama's streamed NDJSON response, tolerant of a single
    JSON object arriving split across more than one transport-level line.

    Found live 2026-08-20: the previous version parsed each line from
    aiter_lines() independently and silently discarded it on any
    json.loads() failure (`except Exception: continue`, no log, no
    trace). Under the real CPU contention this cluster runs Ollama
    under, a JSON object splitting across two lines is plausible -- and
    when it happened, that line's content was just gone, leaving a gap
    in the reconstructed text. Several capability-test failures this
    session ("Unterminated string", "Expecting ',' delimiter" in
    downstream JSON; "unterminated string literal" in generated Python)
    were read as the model being unreliable at structured output -- this
    is what a dropped line in the middle of a string value actually
    looks like, not a model capability ceiling.

    feed() buffers across a parse failure and retries with the next
    line appended, instead of dropping it.
    """

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, line: str) -> str | None:
        """Returns the newly-completed token's content, or None if `line`
        was blank or is still an incomplete fragment being buffered."""
        if not line:
            return None
        candidate = self._pending + line if self._pending else line
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            self._pending = candidate
            return None
        self._pending = ""
        return obj.get("message", {}).get("content", "")

    @property
    def leftover(self) -> str:
        """Unparsed content still buffered when the stream ended -- a
        real error (a genuinely dead connection, not just a benign
        split), surfaced for logging instead of silently vanishing."""
        return self._pending


class AgentShell(ABC):
    """Deterministic wrapper around a non-deterministic LLM worker."""

    # Set True only on agents that must keep working while the swarm is
    # halted (e.g. diagnostician — it exists specifically to investigate a
    # halt, so it can't be subject to the same halt-drops-every-packet rule
    # as everyone else).
    BYPASS_HALT_CHECK: bool = False

    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id   = agent_id
        self.capabilities = set(capabilities)
        self._nc: NATSClient | None = None
        self._memory = get_store()
        self._kv_cache: dict[str, object] = {}
        # packet_id -> in-flight NATS msg, for every packet this replica is
        # genuinely still processing right now (added on claim, removed the
        # moment _mark_packet_done() runs -- see _handle_message). Lets a
        # SIGTERM handler in run() hand every one of them back immediately
        # via _release_for_shutdown() instead of leaving the next claimant
        # to wait out INFLIGHT_STALE_SECONDS, a timer sized for a crash with
        # zero warning, not a routine rolling restart.
        self._active_messages: dict[str, object] = {}

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

    async def record_validation_failure(self, context: str, detail: str) -> None:
        """Persist a non-fatal LLM-output validation failure (a skipped
        malformed sub-task, a degraded-gracefully JSON parse error) to
        durable memory instead of only the pod's ephemeral stdout log --
        these used to just vanish on the next restart with no trace they
        ever happened, making it impossible to see how often a given
        failure shape actually recurs."""
        log.error("[%s] validation failure (%s): %s", self.agent_id, context, detail)
        self._memory.add_semantic(f"[{self.agent_id}] {context}: {detail[:300]}")
        await self._memory.save()

    @staticmethod
    def _ollama_slot_key(url: str) -> str:
        return url.replace("http://", "").replace("https://", "").replace(":", "_").replace("/", "_")

    async def acquire_ollama_slot(self, url: str, max_slots: int) -> str:
        """Block until a slot is actually free on this Ollama instance,
        checked against real claims from every agent pod sharing it --
        not a fixed timer guessing how long a queue wait might take.
        Returns a claim id; pass it to release_ollama_slot when done."""
        key = self._ollama_slot_key(url)
        claim_id = uuid.uuid4().hex
        kv = await self._kv(KV_OLLAMA_SLOTS)
        while True:
            now = time.time()
            try:
                entry = await kv.get(key)
                claims = json.loads(entry.value.decode())
                revision = entry.revision
            except Exception:
                claims, revision = [], None
            # Prune claims held far longer than any real generation could
            # take -- protects against a crashed/killed holder leaking a
            # slot forever instead of just this one request waiting on it.
            live = [c for c in claims if now - c["ts"] < OLLAMA_SLOT_STALE_SECONDS]
            if len(live) < max_slots:
                new_claims = live + [{"id": claim_id, "ts": now}]
                try:
                    if revision is not None:
                        await kv.update(key, json.dumps(new_claims).encode(), last=revision)
                    else:
                        await kv.create(key, json.dumps(new_claims).encode())
                    return claim_id
                except Exception:
                    pass  # lost the race to another claimant -- retry immediately
            await asyncio.sleep(OLLAMA_SLOT_POLL_SECONDS)

    async def release_ollama_slot(self, url: str, claim_id: str) -> None:
        key = self._ollama_slot_key(url)
        kv = await self._kv(KV_OLLAMA_SLOTS)
        for _ in range(5):  # bounded CAS retry against concurrent releases, not a wait
            try:
                entry = await kv.get(key)
                claims = json.loads(entry.value.decode())
                remaining = [c for c in claims if c["id"] != claim_id]
                await kv.update(key, json.dumps(remaining).encode(), last=entry.revision)
                return
            except Exception:
                continue

    async def _claim_packet(self, packet_id: str) -> bool:
        """Atomically claim a packet id for execution. Returns False if
        another delivery of the same packet already holds a live claim, OR
        already finished it -- the caller must then just ack and skip, not
        execute again.

        This exists because JetStream's delivery guarantee is at-least-once,
        not exactly-once: a heartbeat ping delayed even once past ack_wait
        triggers a real redelivery to another consumer while the first copy
        keeps running (or has already finished) regardless -- both then
        publish a "done" result and both run any real side effects (skill
        writes, deploys) a second time.

        Found live 2026-08-17: a *second*, more fundamental instance of this
        even after the fail-open fix below. Several duplicate completions
        landed ~960-1080s apart (right around INFLIGHT_STALE_SECONDS=960)
        despite the original delivery completing and calling
        _mark_packet_done() successfully. Root cause: the claim used to be
        released (deleted) only *after* msg.ack() succeeded -- if ack()
        itself ever throws or silently fails to register (a real
        possibility this fence can't rule out), the release/mark-done step
        never runs at all, the claim's timestamp is never refreshed, and a
        later JetStream redelivery (triggered by the failed ack) finds a
        now-stale claim and correctly-by-the-rules reclaims it as
        abandoned -- reprocessing the entire task from scratch. Fixed by
        marking completion *before* the risky publish/ack steps (see
        _mark_packet_done, called right after _execute() returns in
        handle()) and by distinguishing "genuinely still running" from
        "already finished" via a status field: a finished claim is never
        eligible for the stale-abandoned-crash reclaim path.
        """
        kv = await self._kv(KV_INFLIGHT)
        now = time.time()
        try:
            await kv.create(packet_id,
                             json.dumps({"agent": self.agent_id, "ts": now, "status": "in_progress"}).encode())
            return True
        except Exception:
            pass  # key already exists -- someone holds (or held) this claim

        # The create() above failing PROVES a claim already exists -- not
        # being able to read it doesn't mean it's gone. This used to fail
        # open (return True, "safe to execute") on any transient KV read
        # error, which is backwards and reopened the exact duplicate-
        # execution bug this fence exists to close. It disproportionately
        # hit the assess capability -- by far the highest-throughput agent,
        # so the most likely to hit a transient JetStream read blip under
        # real concurrent load. Retry a few times for an ordinary blip; if
        # it still can't be read, fail CLOSED (assume live, reject) rather
        # than open (assume abandoned, re-execute) -- occasionally dropping
        # a packet on a persistent KV outage is a far smaller cost than
        # silently re-running real side effects a second time.
        claim = None
        entry = None
        for attempt in range(3):
            try:
                entry = await kv.get(packet_id)
                claim = json.loads(entry.value.decode())
                break
            except Exception:
                if attempt == 2:
                    log.error("[%s] could not read existing claim for %s after retries -- "
                              "failing closed (treating as still live)", self.agent_id, packet_id[:8])
                    return False
                await asyncio.sleep(0.2)

        if claim.get("status") == "done":
            # Already finished -- never eligible for reclaim on staleness
            # grounds. A redelivery arriving any time later (that's the
            # whole failure mode this fix closes) must still be rejected,
            # not mistaken for an abandoned in-progress claim. Only an
            # extremely old "done" marker (long past any plausible
            # redelivery lag) is pruned, so this bucket doesn't grow
            # forever across the swarm's lifetime.
            if now - claim.get("ts", 0) < DONE_CLAIM_RETENTION_SECONDS:
                return False
        elif now - claim.get("ts", 0) < INFLIGHT_STALE_SECONDS:
            return False

        try:
            await kv.update(packet_id,
                             json.dumps({"agent": self.agent_id, "ts": now, "status": "in_progress"}).encode(),
                             last=entry.revision)
            return True
        except Exception:
            return False  # lost the race to whoever else just reclaimed it

    async def _refresh_packet_claim(self, packet_id: str) -> None:
        """Bump a live claim's timestamp so it never goes stale while its
        holder is genuinely still alive and heartbeating.

        Found live 2026-08-17: the claim's staleness clock was a single
        fixed timestamp set once at claim time, completely disconnected
        from whether the holder was actually still working -- unlike
        JetStream's own ack_wait, which the heartbeat already resets every
        90s via msg.in_progress(). Under real Ollama-slot contention (both
        concurrent slots full most of the night, made worse by adding a
        second verifier replica competing for the same two slots), one
        verify call legitimately took 34 minutes -- almost entirely spent
        waiting for a free slot before the LLM call even started -- which
        blew past INFLIGHT_STALE_SECONDS (960s) even though the holder
        never crashed. A second delivery then correctly-by-the-old-rules
        reclaimed it as abandoned and re-executed the whole task, producing
        a real duplicate with a different (non-deterministic) verifier
        score. Refreshing the claim's timestamp on every heartbeat -- the
        same cadence already proven reliable for JetStream's own ack_wait
        -- means the claim only goes stale if heartbeats actually stop
        (a genuine crash), regardless of how long legitimate processing
        (including semaphore wait, which has no upper bound) takes.
        """
        kv = await self._kv(KV_INFLIGHT)
        for _ in range(3):
            try:
                entry = await kv.get(packet_id)
                claim = json.loads(entry.value.decode())
            except Exception:
                return
            if claim.get("status") != "in_progress":
                return  # already marked done elsewhere -- nothing to refresh
            claim["ts"] = time.time()
            try:
                await kv.update(packet_id, json.dumps(claim).encode(), last=entry.revision)
                return
            except Exception:
                continue  # lost a race against another writer -- retry

    async def _mark_packet_done(self, packet_id: str) -> None:
        """Mark a claim as finished rather than deleting it. Deleting made
        a completed packet indistinguishable from "never claimed" -- any
        later redelivery of the same message (confirmed live 2026-08-17,
        arriving well after the original had already finished) was then
        treated as brand new work and fully re-executed. A "done" marker
        is instead kept around for DONE_CLAIM_RETENTION_SECONDS so a late
        redelivery is correctly recognized and rejected. Called right after
        _execute() returns, deliberately *before* the publish/emit_status/
        ack sequence -- so even if ack() itself throws or never registers,
        the fact that this packet is done is already durably recorded."""
        kv = await self._kv(KV_INFLIGHT)
        now = time.time()
        for _ in range(5):  # bounded CAS retry against concurrent writers
            try:
                entry = await kv.get(packet_id)
                await kv.update(packet_id,
                                 json.dumps({"agent": self.agent_id, "ts": now, "status": "done"}).encode(),
                                 last=entry.revision)
                return
            except Exception:
                continue
        log.error("[%s] could not mark packet %s done after retries", self.agent_id, packet_id[:8])

    async def disconnect(self) -> None:
        if self._nc:
            await self._nc.drain()

    async def _handle_message(self, msg) -> None:
        """Process one delivered packet message end-to-end. Broken out of
        run() as its own method (rather than a closure) specifically so it
        can be unit-tested with a fake `msg` (just needs async ack()/
        in_progress()) and mocked _execute(), without a live NATS server.

        Every exit path acks — redelivery here exists only to rescue a
        packet whose delivery attempt never finished (pod died mid-handling
        before this line ran), not to retry business-logic failures. Those
        are already the halt gate's job, and a failed packet redelivering
        forever would just spam the same error into the halt reason.
        """
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
        if halt and not self.BYPASS_HALT_CHECK:
            log.warning("[%s] swarm halted (%s) — dropping packet %s",
                        self.agent_id, halt.get("reason"), packet.id[:8])
            await msg.ack()
            return

        if not await self._claim_packet(packet.id):
            log.warning("[%s] packet %s already claimed by another delivery — "
                        "acking as a duplicate redelivery, not re-executing",
                        self.agent_id, packet.id[:8])
            await msg.ack()
            return

        packet.claim(self.agent_id)
        self._active_messages[packet.id] = msg
        await self._emit_status("working", packet)

        # Tell JetStream "still alive" every 90s (well under ack_wait's
        # 300s) for as long as _execute() genuinely keeps running --
        # decouples "how long can a legitimate LLM call take" from
        # "when does JetStream assume this pod died and redelivers to
        # someone else" entirely. A real generation can now take as
        # long as it actually needs; only a pod that's genuinely
        # crashed (heartbeats stop with it) still triggers redelivery,
        # exactly the resilience ack_wait was built for.
        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(90)
                try:
                    await msg.in_progress()
                    # Refresh the inflight claim's timestamp on the same
                    # cadence -- otherwise its staleness clock is disconnected
                    # from whether this holder is actually still alive. See
                    # _refresh_packet_claim's docstring for the live incident
                    # this fixes.
                    await self._refresh_packet_claim(packet.id)
                    log.info("[%s] heartbeat sent for %s", self.agent_id, packet.id[:8])
                except Exception as exc:
                    log.error("[%s] heartbeat FAILED for %s: %r", self.agent_id, packet.id[:8], exc)

        heartbeat_task = asyncio.create_task(_heartbeat())
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
            await self._emit_diagnose_request(packet, detail)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        # Marked done here -- immediately once _execute() has genuinely
        # finished (success or failure), before the publish/emit_status/ack
        # sequence below, not after. An earlier version of this fix marked
        # completion only *after* msg.ack() succeeded, which reopened the
        # exact race this fence exists to close: if ack() itself ever
        # throws or silently fails to register, that release/mark-done
        # step never runs, the claim's timestamp never gets refreshed, and
        # a later JetStream redelivery (triggered by the failed ack) finds
        # a now-stale claim and correctly-by-the-rules reclaims it as an
        # abandoned crash -- reprocessing the entire task from scratch.
        # Confirmed live 2026-08-17: several packets completed twice,
        # ~960-1080s apart (right around INFLIGHT_STALE_SECONDS), even
        # after the first fence fix. Marking done up front means the fact
        # that this packet is finished is durably recorded no matter what
        # happens to publish/ack afterward.
        await self._mark_packet_done(packet.id)
        # Popped here, not after ack() -- _release_for_shutdown() only ever
        # touches packets still in this dict, and a packet is only ever
        # genuinely still "in progress" up to this exact line.
        self._active_messages.pop(packet.id, None)
        await self._publish(SUBJECT_RESULTS, packet)
        await self._emit_status("idle")
        await msg.ack()

    async def _release_for_shutdown(self) -> None:
        """Called from a SIGTERM handler in run() -- a graceful pod
        termination (e.g. mid-rollout during `make deploy`) gets a real
        termination grace period to hand back any in-flight claim, unlike
        a hard crash with zero warning. For every packet this replica is
        still genuinely processing: nak the message so JetStream
        redelivers it right away (instead of waiting out ack_wait), and
        delete its KV_INFLIGHT claim so the redelivered copy is claimable
        immediately rather than read as still-in-progress.

        Found live 2026-08-20: without this, recovery from a claim-holder
        killed by a routine rolling restart had to wait out the full
        INFLIGHT_STALE_SECONDS (960s) -- a timer deliberately sized for a
        crash with no warning at all, not a planned shutdown. The test
        harness's own per-test timeout (900s) is shorter than that, so
        the gap reliably surfaced as a false TIMEOUT on whichever test
        happened to be in flight during a deploy.

        Safe even if this process's own _execute() finishes anyway before
        SIGKILL and tries to complete normally afterward -- the
        idempotency fence already tolerates two replicas briefly
        processing the same packet (see _claim_packet's and
        _mark_packet_done's own docstrings)."""
        kv = await self._kv(KV_INFLIGHT)
        for packet_id, msg in list(self._active_messages.items()):
            try:
                await msg.nak()
            except Exception as exc:
                log.error("[%s] nak failed for %s during shutdown: %r", self.agent_id, packet_id[:8], exc)
            self._active_messages.pop(packet_id, None)
            try:
                await kv.delete(packet_id)
            except Exception as exc:
                log.error("[%s] claim release failed for %s during shutdown: %r", self.agent_id, packet_id[:8], exc)

    async def run(self) -> None:
        await self.connect()

        # Kubernetes sends SIGTERM before SIGKILL on a routine pod
        # termination (rolling restart, scale-down) and waits out a grace
        # period -- real time to hand back any in-flight claim cleanly via
        # _release_for_shutdown(), rather than leaving the next claimant to
        # wait out INFLIGHT_STALE_SECONDS as if this were an unannounced
        # crash. shutdown_event decouples "signal received" from "actually
        # released" so the handler itself stays a plain, fast callback.
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)

        async def _shutdown_watcher() -> None:
            await shutdown_event.wait()
            log.info("[%s] SIGTERM received -- releasing in-flight claims", self.agent_id)
            await self._release_for_shutdown()

        asyncio.create_task(_shutdown_watcher())

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
            await js.subscribe(subject, durable=durable, queue=durable, cb=self._handle_message,
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

    async def _emit_diagnose_request(self, failed_packet: CXPPacket, detail: str) -> None:
        """Fired on every halt, regardless of which agent crashed — hands the
        raw exception detail to the diagnostician so it can investigate
        while the swarm sits halted, instead of a human being the only
        thing that ever looks at why."""
        diagnose = CXPPacket(
            origin=self.agent_id,
            type=PacketType.DIAGNOSE,
            capability="diagnose",
            priority=5,
            task_id=failed_packet.task_id,
            parent_packet_id=failed_packet.id,
            payload=Payload(
                goal="Diagnose swarm halt and propose resolution",
                instructions=detail,
                context=f"agent={self.agent_id} capability={failed_packet.capability} packet_id={failed_packet.id}",
            ),
        )
        await self.emit_packet(diagnose)

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

    async def llm(self, system: str, user: str, packet_id: str | None = None) -> str:
        """Call Ollama with streaming. Auto-pull is disabled (see below) — a
        missing model raises RuntimeError rather than silently pulling.

        packet_id is optional (some callers don't have one in scope) and
        only used to tag the OTel span below for later lookup -- it has
        no effect on the call itself."""
        import httpx

        tracer = get_tracer(__name__)
        call_start = time.time()

        # connect=10s, write=10s, NO per-chunk read limit (see below) --
        # LLM_TOTAL_TIMEOUT is the one and only "how long is too long"
        # threshold for the whole call.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        #
        # Used to also have a 60s PER-CHUNK read timeout here, stacked on
        # top of the total. Found live (2026-08-17): this actively kills
        # requests Ollama is still genuinely working on. Ollama's own
        # server log showed a real verifier request complete successfully
        # in 59.999922528s -- a hair under the 60s per-chunk limit -- and
        # the client gave up microseconds before the response arrived. The
        # per-chunk timeout was meant to catch a truly-dead connection, but
        # LLM_TOTAL_TIMEOUT already does that job, with real margin chosen
        # from observed data -- no reason to guess at a second, tighter
        # threshold on top of it. A genuinely dead connection (zero bytes
        # for the entire budget) still gets caught below; a slow-but-
        # working one no longer gets killed partway through for no reason.
        #
        # Used to be kept below ack_wait (300s) specifically so this failed
        # and halted before JetStream would redeliver the same message to
        # another attempt -- found live (2026-08-17) that this coupling
        # itself was the wrong fix: real requests kept landing within a
        # couple seconds of 240s and succeeding anyway (observed up to
        # 239s), meaning legitimate work under real CPU-only load routinely
        # needs several minutes, and no timeout number was going to feel
        # right. The real fix is handle()'s heartbeat loop above (msg.
        # in_progress() every 90s), which decouples "how long can a
        # legitimate call take" from ack_wait's redelivery entirely -- a
        # pod that's actually still working keeps the packet alive no
        # matter how long it takes; only a genuinely crashed pod (heartbeat
        # stops with it) still triggers redelivery. This is now a true
        # "something is definitely wrong" backstop, not a guess at typical
        # duration -- picked generously since there's no longer a reason to
        # keep it tight.
        LLM_TOTAL_TIMEOUT = 900.0

        max_slots = int(os.environ.get("OLLAMA_MAX_PARALLEL", "2"))
        claim_id = await self.acquire_ollama_slot(OLLAMA_URL, max_slots)

        await self._think(f"  ⟳ LLM ({len(user)} chars): {user[:120]}…")

        # Declared here, not inside _stream() -- so a timeout (below) still
        # leaves whatever content had actually been generated so far
        # readable for the telemetry span. asyncio.wait_for() cancels the
        # _stream() coroutine on timeout; a list local to that closure
        # would be unreachable afterward, but mutating (not rebinding) a
        # list from the enclosing scope survives the cancellation fine.
        chunks: list[str] = []

        async def _stream(client: httpx.AsyncClient) -> str:
            total_len = 0
            # Publishes the actual text generated since the last update, not
            # just whichever single token happened to cross the 30-char
            # mark -- the old `total_len % 30 == 0` check only gated WHEN to
            # publish, but then sent `token` alone (often a single
            # character on this small/quantized model), making the live
            # thinking-stream show meaningless fragments like "s", "g", "4"
            # instead of readable progress.
            last_published_len = 0
            reassembler = _NDJSONReassembler()
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL, "stream": True,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    token = reassembler.feed(line)
                    if token:
                        chunks.append(token)
                        total_len += len(token)
                        if total_len - last_published_len >= 30:
                            new_text = "".join(chunks)[last_published_len:total_len]
                            last_published_len = total_len
                            await self._nc.publish(SUBJECT_THINKING,
                                json.dumps({"agent": self.agent_id, "text": new_text, "stream": True}).encode())
            if reassembler.leftover:
                log.error("[%s] LLM stream ended with unparsed content, discarding %d chars: %r",
                          self.agent_id, len(reassembler.leftover), reassembler.leftover[:200])
            return "".join(chunks)

        with tracer.start_as_current_span("llm.call") as span:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    try:
                        full = await asyncio.wait_for(_stream(client), timeout=LLM_TOTAL_TIMEOUT)
                        record_llm_call(span, agent_id=self.agent_id, packet_id=packet_id,
                                         system=system, user=user, timed_out=False,
                                         response=full, duration_seconds=time.time() - call_start)
                        return full
                    except asyncio.TimeoutError:
                        # chunks (outer scope, see above) still holds
                        # whatever content had actually been generated up
                        # to the moment the budget ran out -- captured here
                        # instead of discarded, so it's possible to tell
                        # "generating something reasonable that just needed
                        # more time" apart from "stuck looping / garbage,"
                        # neither of which was distinguishable before this.
                        record_llm_call(span, agent_id=self.agent_id, packet_id=packet_id,
                                         system=system, user=user, timed_out=True,
                                         response="".join(chunks), duration_seconds=time.time() - call_start)
                        raise TimeoutError(f"LLM call exceeded total budget of {LLM_TOTAL_TIMEOUT}s")
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            raise RuntimeError(
                                f"Model '{OLLAMA_MODEL}' not found in Ollama. "
                                f"Available models must be pre-cached in PVC. "
                                f"Auto-pull is disabled to prevent PostStartHook failures."
                            )
                        raise
            finally:
                # Released on every exit path, success or failure, so a request
                # that errors doesn't leak its slot -- OLLAMA_SLOT_STALE_SECONDS
                # is the backstop for a pod that gets killed outright and never
                # reaches this finally block at all.
                await self.release_ollama_slot(OLLAMA_URL, claim_id)

        await self._think(f"  ✓ response ({len(full)} chars): {full[:120]}…")
        return full
