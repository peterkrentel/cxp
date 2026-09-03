"""Web dashboard — browser UI with task submission, thinking stream, and output view."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timedelta

import nats
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .agent_shell import (
  KV_CANDIDATE_EVALUATIONS,
  KV_SKILL_CANDIDATES,
  KV_SKILLS,
  KV_STATE,
  NATS_URL,
  SUBJECT_DASHBOARD,
  SUBJECT_PACKETS,
  SUBJECT_RESULTS,
  SUBJECT_THINKING,
  get_or_create_kv,
)
from .memory import get_store
from .packet import CXPPacket, PacketType, Payload

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="CXP")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

state = {
    "agents": {},
    "packets": [],
    "thinking": deque(maxlen=200),
    "stats": {"tasks_done": 0, "tasks_error": 0, "llm_calls": 0, "reflects": 0},
    "last_activity": datetime.now().isoformat(),
}

_nc = None
_kv_cache: dict[str, object] = {}


async def _kv(bucket: str):
    if bucket not in _kv_cache:
        _kv_cache[bucket] = await get_or_create_kv(_nc.jetstream(), bucket)
    return _kv_cache[bucket]


async def get_halt() -> dict | None:
    if not _nc:
        return None
    try:
        kv = await _kv(KV_STATE)
        entry = await kv.get("halt")
        data = json.loads(entry.value.decode())
        return data if data.get("halted") else None
    except Exception:
        return None


async def get_tier_status() -> dict | None:
    """Per-tier streak progress and the currently active tier, published to
    KV_STATE by tests/check_plateau.py (the only component with git access
    to the tests/results history this is computed from) -- this pod has no
    git credentials of its own to read that history directly."""
    if not _nc:
        return None
    try:
        kv = await _kv(KV_STATE)
        entry = await kv.get("tier-status")
        return json.loads(entry.value.decode())
    except Exception:
        return None


async def get_candidate_evaluation(candidate_id: str) -> dict | None:
    if not _nc:
        return None
    try:
        kv = await _kv(KV_CANDIDATE_EVALUATIONS)
        entry = await kv.get(candidate_id)
        return json.loads(entry.value.decode())
    except Exception:
        return None


async def get_candidate_evaluations() -> list[dict]:
    if not _nc:
        return []
    try:
        kv = await _kv(KV_CANDIDATE_EVALUATIONS)
        reports = []
        for candidate_id in sorted(await kv.keys()):
            report = json.loads((await kv.get(candidate_id)).value.decode())
            reports.append({"candidate_id": candidate_id, **report})
        return reports
    except Exception:
        return []


async def promote_candidate(candidate_id: str) -> dict:
    """Apply a human-approved, positively evaluated candidate skill."""
    if not _nc:
        raise ValueError("dashboard is not connected to NATS")
    candidates = await _kv(KV_SKILL_CANDIDATES)
    reports = await _kv(KV_CANDIDATE_EVALUATIONS)
    try:
        candidate = json.loads((await candidates.get(candidate_id)).value.decode())
        report = json.loads((await reports.get(candidate_id)).value.decode())
    except Exception as exc:
        raise ValueError(f"candidate or evaluation report not found: {candidate_id}") from exc
    if report.get("recommendation") != "recommend_promotion":
        raise ValueError(f"candidate {candidate_id} is not recommended for promotion")
    target_role = candidate.get("target_role")
    content = candidate.get("content")
    if target_role not in {"planner", "executor", "verifier"} or not isinstance(content, str):
        raise ValueError(f"candidate {candidate_id} has invalid promotion data")
    active_skills = await _kv(KV_SKILLS)
    revision = await active_skills.put(target_role, content.encode())
    report["promotion"] = {
      "revision": revision,
      "target_role": target_role,
      "timestamp": datetime.now().isoformat(),
    }
    await reports.put(candidate_id, json.dumps(report).encode())
    return {"candidate_id": candidate_id, "target_role": target_role, "revision": revision}


# Every question asked by hand tonight while chasing a live duplicate-
# processing bug -- "is anything actually processing right now", "did that
# fix actually stop the duplicates" -- required a kubectl/nats CLI round
# trip nobody but an operator with cluster access could run. Surfacing the
# same two signals here means a browser tab answers both without asking.
CAPABILITIES = ["plan", "code", "verify", "reflect", "assess", "deploy", "diagnose"]
STREAM_PACKETS = "CXP_PACKETS"


async def get_stream_health() -> list[dict]:
    if not _nc:
        return []
    js = _nc.jetstream()
    out = []
    for cap in CAPABILITIES:
        durable = f"cxp-{cap}"
        try:
            info = await js.consumer_info(STREAM_PACKETS, durable)
            out.append({
                "capability": cap,
                "outstanding_acks": info.num_ack_pending,
                "redelivered": info.num_redelivered,
                "pending": info.num_pending,
            })
        except Exception as exc:
            out.append({"capability": cap, "error": str(exc)})
    return out


DUPLICATE_RECENCY_MINUTES = 20


def get_duplicate_packets() -> list[dict]:
    """Any packet id completed more than once in the current in-memory
    buffer, where the most recent completion is still recent -- the direct
    symptom of the 2026-08-17 duplicate-processing bug. Recency-gated
    rather than "ever duplicated in this buffer" so a resolved incident
    stops being flagged once it's aged out, instead of lingering
    indefinitely until this pod happens to restart or ~100 more packets
    push the raw entries out of state["packets"]'s capped buffer. Found
    confusing live 2026-08-17: the same pre-fix duplicates kept showing
    for many minutes after the fix that stopped them was already deployed
    and separately confirmed to be holding."""
    by_id: dict[str, list[dict]] = {}
    for p in state["packets"]:
        by_id.setdefault(p["id"], []).append(p)
    cutoff = datetime.now() - timedelta(minutes=DUPLICATE_RECENCY_MINUTES)
    result = []
    for pid, entries in by_id.items():
        if len(entries) <= 1:
            continue
        timestamps = [e["timestamp"] for e in entries]
        most_recent = max(datetime.fromisoformat(ts) for ts in timestamps)
        if most_recent < cutoff:
            continue
        result.append({"id": pid, "capability": entries[0].get("capability"), "timestamps": timestamps})
    return result


async def subscribe_nats():
    global _nc
    try:
        _nc = await nats.connect(NATS_URL)
        log.info(f"✓ Connected to NATS: {NATS_URL}")
    except Exception as e:
        log.error(f"✗ Failed to connect to NATS: {e}")
        raise

    async def on_dashboard(msg):
        try:
            data = json.loads(msg.data)
            state["agents"][data.get("agent")] = data
            state["last_activity"] = datetime.now().isoformat()
        except Exception as e:
            log.warning(f"Dashboard packet error: {e}")

    async def on_result(msg):
        try:
            packet = CXPPacket.model_validate_json(msg.data)
            log.info(f"✓ Result packet: {packet.id[:8]} (cap={packet.capability}, status={packet.status})")
            state["packets"].append({
                "id": packet.id[:8],
                "task_id": packet.task_id,
                "type": packet.type.value,
                "capability": packet.capability,
                "status": packet.status.value,
                "score": packet.quality_score,
                "goal": packet.payload.goal if packet.payload else "",
                "output": packet.payload.output if packet.payload else "",
                "timestamp": datetime.now().isoformat(),
            })
            state["packets"] = state["packets"][-100:]
            if packet.status.value == "done":
                state["stats"]["tasks_done"] += 1
            elif packet.status.value == "error":
                state["stats"]["tasks_error"] += 1
            # capability, not type — PacketType has no ASSESS/DEPLOY value, so
            # verifier labels deploy/assess/reflect packets all as type=REFLECT;
            # counting by type here previously counted deploys/assessments too.
            if packet.capability == "reflect" and packet.status.value == "done":
                state["stats"]["reflects"] += 1
            state["last_activity"] = datetime.now().isoformat()
        except Exception as e:
            log.warning(f"Result packet error: {e}")

    async def on_thinking(msg):
        try:
            data = json.loads(msg.data)
            text = data.get("text", "")
            state["thinking"].append({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "agent": data.get("agent", "?"),
                "text": text,
                "stream": data.get("stream", False),
            })
            if "⟳ LLM" in text:
                state["stats"]["llm_calls"] += 1
        except Exception as e:
            log.warning(f"Thinking packet error: {e}")

    await _nc.subscribe(SUBJECT_DASHBOARD, cb=on_dashboard)
    await _nc.subscribe(SUBJECT_RESULTS, cb=on_result)
    await _nc.subscribe(SUBJECT_THINKING, cb=on_thinking)
    log.info(f"✓ Subscribed to {SUBJECT_DASHBOARD}, {SUBJECT_RESULTS}, {SUBJECT_THINKING}")
    while True:
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    log.info("Starting web dashboard...")
    task = asyncio.create_task(subscribe_nats())
    # Wait a bit for the connection to establish, but don't block forever
    try:
        await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
    except:
        pass
    log.info("Startup event completed (subscribe_nats running in background)")


# Fields that only the trusted in-cluster test-runner CronJob may set --
# candidate_id lets a caller run any staged (unvetted) skill candidate
# against a real task, and evaluation_run hides a real production score
# from the episodic-memory regression baseline. /api/submit has no other
# authentication, so both are stripped from any caller that can't present
# the shared token below.
INTERNAL_ONLY_INPUT_KEYS = {"candidate_id", "evaluation_run"}


def _has_valid_internal_token(header_value: str | None) -> bool:
    expected = os.environ.get("CXP_INTERNAL_TOKEN")
    if not expected or not header_value:
        return False
    return hmac.compare_digest(header_value, expected)


def sanitize_untrusted_inputs(inputs: dict, internal_token_header: str | None) -> dict:
    if _has_valid_internal_token(internal_token_header):
        return inputs
    return {k: v for k, v in inputs.items() if k not in INTERNAL_ONLY_INPUT_KEYS}


def build_submission_packet(body: dict) -> CXPPacket:
    goal = body.get("goal", "").strip()
    inputs = body.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    capability = body.get("capability", "plan")
    type_map = {"plan": PacketType.PLAN, "code": PacketType.CODE,
                "verify": PacketType.VERIFY, "reflect": PacketType.REFLECT,
                "assess": PacketType.ASSESS, "deploy": PacketType.DEPLOY}
    return CXPPacket(
        origin="web-ui",
        type=type_map.get(capability, PacketType.PLAN),
        capability=capability,
        priority=5,
        task_id=uuid.uuid4().hex[:8],
        payload=Payload(goal=goal, instructions=goal, context="", inputs=inputs),
    )


@app.post("/api/submit")
async def submit_task(request: Request):
    halt = await get_halt()
    if halt:
        return JSONResponse({
            "error": f"swarm halted: {halt.get('reason', 'unknown error')} — clear it before submitting new work",
            "halt": halt,
        }, status_code=409)

    body = await request.json()
    if isinstance(body.get("inputs"), dict):
        body["inputs"] = sanitize_untrusted_inputs(
            body["inputs"], request.headers.get("x-cxp-internal-token")
        )
    try:
        packet = build_submission_packet(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not packet.payload.goal:
        return JSONResponse({"error": "goal required"}, status_code=400)
    if _nc:
        await _nc.jetstream().publish(f"cxp.cap.{packet.capability}", packet.model_dump_json().encode())
    return JSONResponse({"task_id": packet.task_id, "packet_id": packet.id[:8]})


@app.post("/api/halt/clear")
async def clear_halt():
    kv = await _kv(KV_STATE)
    await kv.put("halt", json.dumps({"halted": False}).encode())
    return JSONResponse({"ok": True})


@app.post("/api/candidates/{candidate_id}/promote")
async def promote_candidate_route(candidate_id: str):
    try:
        return JSONResponse(await promote_candidate(candidate_id))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.get("/")
async def root():
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
<title>CXP Swarm</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Courier New', monospace; background: #0d0d0d; color: #00ff00; font-size: 12px; padding: 8px; }
.row { display: flex; gap: 8px; margin-bottom: 8px; }
.panel { border: 1px solid #1a6600; padding: 6px; flex: 1; min-width: 0; }
.panel-title { color: #fff; text-align: center; border-bottom: 1px solid #1a6600; padding-bottom: 4px; margin-bottom: 6px; font-size: 11px; letter-spacing: 1px; }
table { width: 100%; border-collapse: collapse; }
th { color: #ff00ff; text-align: left; padding: 2px 6px; font-size: 11px; }
td { padding: 2px 6px; border-bottom: 1px solid #111; }
.g{color:#00ff00}.y{color:#ffff00}.r{color:#ff4444}.c{color:#00ffff}.d{color:#555}
.stat-row { display: flex; gap: 8px; margin-bottom: 8px; }
.stat-box { border: 1px solid #1a6600; padding: 4px 10px; flex: 1; text-align: center; }
.stat-val { font-size: 20px; font-weight: bold; }
.scroll { overflow-y: auto; }
.log-line { white-space: pre-wrap; padding: 1px 4px; border-bottom: 1px solid #0a0a0a; }
pre { white-space: pre-wrap; word-break: break-word; color: #ccc; font-size: 11px; padding: 4px; }
input[type=text] { background: #111; color: #0f0; border: 1px solid #1a6600; padding: 6px 10px; font-family: monospace; font-size: 12px; width: calc(100% - 90px); outline: none; }
button { background: #1a6600; color: #0f0; border: 1px solid #0f0; padding: 6px 14px; font-family: monospace; font-size: 12px; cursor: pointer; }
button:hover { background: #0f0; color: #000; }
.submit-row { display: flex; gap: 8px; align-items: center; }
#submit-status { color: #888; font-size: 11px; min-width: 120px; }
tr[data-clickable]:hover { background: #1a1a0a; cursor: pointer; }
.resizable { resize: vertical; overflow: auto; min-height: 80px; }
.drag-handle { height: 12px; background: linear-gradient(to bottom, #1a6600 0%, #00ff00 50%, #1a6600 100%); cursor: ns-resize; margin: 4px 0; display: flex; align-items: center; justify-content: center; user-select: none; }
.drag-handle::after { content: '⋮'; color: #0f0; font-size: 14px; font-weight: bold; }
.drag-handle:hover { background: linear-gradient(to bottom, #00ff00 0%, #00ff00 50%, #00ff00 100%); }
#halt-banner { display:none; border:1px solid #ff4444; background:#2a0a0a; color:#ff6666; padding:8px 10px; margin-bottom:8px; align-items:center; justify-content:space-between; }
#halt-banner button { background:#ff4444; color:#000; border:1px solid #ff6666; }
#halt-banner button:hover { background:#ff8888; }
</style>
</head>
<body>

<div class="panel" id="halt-banner">
  <div>
    <div>⛔ SWARM HALTED — <span id="halt-reason"></span></div>
    <div id="halt-diagnosis" style="display:none; font-size:0.9em; opacity:0.85; margin-top:4px">🩺 <span id="halt-diagnosis-text"></span> — <i id="halt-suggested-action"></i></div>
  </div>
  <button onclick="clearHalt()">Resume ▶</button>
</div>

<div class="panel" style="margin-bottom:8px">
  <div class="panel-title">Submit Task</div>
  <div class="submit-row">
    <input type="text" id="goal-input" placeholder="describe what you want the swarm to build or solve…" />
    <button onclick="submitTask()">▶ Run</button>
    <span id="submit-status"></span>
  </div>
</div>

<div class="stat-row">
  <div class="stat-box"><div class="stat-val g" id="s-done">-</div><div class="d">Done</div></div>
  <div class="stat-box"><div class="stat-val r" id="s-error">-</div><div class="d">Errors</div></div>
  <div class="stat-box"><div class="stat-val c" id="s-llm">-</div><div class="d">LLM Calls</div></div>
  <div class="stat-box"><div class="stat-val" style="color:#f0f" id="s-reflect">-</div><div class="d">Skill Updates</div></div>
  <div class="stat-box"><div class="stat-val y" id="s-idle">-</div><div class="d">Last Activity</div></div>
</div>

<div class="panel" id="dup-banner" style="display:none; border-color:#ff4444; background:#2a0a0a; color:#ff6666; margin-bottom:8px">
  <div>⚠ DUPLICATE PACKET COMPLETIONS DETECTED — a packet finished more than once within the last 20 minutes. This is the exact symptom of the 2026-08-17 redelivery bug; if this appears after a fix has been deployed, that fix isn't holding. Clears on its own once nothing new is added for 20 minutes.</div>
  <table style="margin-top:4px"><thead><tr><th>Packet</th><th>Cap</th><th>Times seen</th></tr></thead><tbody id="dup-rows"></tbody></table>
</div>

<div class="row">
  <div class="panel">
    <div class="panel-title">Agents</div>
    <table><thead><tr><th>Agent</th><th>State</th><th>Working On</th></tr></thead><tbody id="agents"></tbody></table>
  </div>
  <div class="panel">
    <div class="panel-title">Reputation</div>
    <table><thead><tr><th>Agent</th><th>Cap</th><th>Score</th><th>✓/✗</th></tr></thead><tbody id="rep"></tbody></table>
  </div>
  <div class="panel">
    <div class="panel-title">Queue Health — is anything stuck?</div>
    <table><thead><tr><th>Cap</th><th>Pending</th><th>In-flight</th><th>Redelivered</th></tr></thead><tbody id="health"></tbody></table>
  </div>
  <div class="panel">
    <div class="panel-title">Tier Progress — capability difficulty ladder</div>
    <table><thead><tr><th>Tier</th><th>Streak</th><th>Active</th></tr></thead><tbody id="tier-rows"></tbody></table>
  </div>
</div>

<div class="panel" style="margin-bottom:8px">
  <div class="panel-title">Candidate Evaluations</div>
  <table><thead><tr><th>Candidate</th><th>Role</th><th>Baseline</th><th>Candidate</th><th>Recommendation</th><th></th></tr></thead><tbody id="candidate-rows"></tbody></table>
</div>

<div class="row" style="height:200px" id="mid-row">
  <div class="panel resizable" style="flex:1.5;display:flex;flex-direction:column" id="pkt-panel">
    <div class="panel-title">Packets — newest first, ⟳ live rows pinned at top, click a finished row for output</div>
    <div class="scroll" style="flex:1">
      <table><thead><tr><th>ID</th><th>Type</th><th>Cap</th><th>Status</th><th>Score</th><th>Goal</th></tr></thead>
      <tbody id="pkts"></tbody></table>
    </div>
  </div>
  <div class="panel resizable" style="flex:1;display:flex;flex-direction:column" id="think-panel">
    <div class="panel-title">Agent Thinking / LLM Stream</div>
    <div class="scroll" id="thinking" style="flex:1"></div>
  </div>
</div>

<div class="drag-handle" id="output-drag"></div>
<div class="panel" id="output-wrap">
  <div class="panel-title">Output — <span id="out-goal" class="y"></span></div>
  <div class="scroll resizable" style="max-height:320px" id="out-wrap">
    <pre id="out-content">Click a completed packet above to see its output.</pre>
  </div>
</div>

<script>
let seenThinking = new Set();
let packets = [];

async function submitTask() {
  const goal = document.getElementById('goal-input').value.trim();
  if (!goal) return;
  document.getElementById('submit-status').textContent = '⟳ submitting…';
  try {
    const res = await fetch('/api/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({goal})});
    const data = await res.json();
    document.getElementById('submit-status').textContent = data.error ? '✗ '+data.error : '✓ task='+data.task_id;
    document.getElementById('goal-input').value = '';
    document.getElementById('out-goal').textContent = goal;
    document.getElementById('out-content').textContent = '⟳ waiting for agents…';
  } catch(e) { document.getElementById('submit-status').textContent = '✗ '+e.message; }
}
document.getElementById('goal-input').addEventListener('keydown', e => { if(e.key==='Enter') submitTask(); });

async function clearHalt() {
  await fetch('/api/halt/clear', {method:'POST'});
  refresh();
}

async function promoteCandidate(candidateId) {
  const response = await fetch(`/api/candidates/${encodeURIComponent(candidateId)}/promote`, {method:'POST'});
  const result = await response.json();
  document.getElementById('submit-status').textContent = result.error
    ? '✗ ' + result.error
    : `✓ promoted ${result.target_role} rev=${result.revision}`;
  refresh();
}

function showOutput(id) {
  const p = packets.find(x=>x.id===id);
  if (!p) return;
  document.getElementById('out-goal').textContent = p.goal || '';
  document.getElementById('out-content').textContent = p.output || '(no output yet — task may still be running)';
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function refresh() {
  const data = await fetch('/api/state').then(r=>r.json());
  packets = data.packets;

  const banner = document.getElementById('halt-banner');
  if (data.halt) {
    banner.style.display = 'flex';
    document.getElementById('halt-reason').textContent = data.halt.reason || 'unknown error';
    const diag = document.getElementById('halt-diagnosis');
    if (data.halt.diagnosis) {
      diag.style.display = 'block';
      document.getElementById('halt-diagnosis-text').textContent = data.halt.diagnosis;
      document.getElementById('halt-suggested-action').textContent = data.halt.suggested_action || '';
    } else {
      diag.style.display = 'none';
    }
  } else {
    banner.style.display = 'none';
  }

  document.getElementById('s-done').textContent = data.stats.tasks_done;
  document.getElementById('s-error').textContent = data.stats.tasks_error;
  document.getElementById('s-llm').textContent = data.stats.llm_calls;
  document.getElementById('s-reflect').textContent = data.stats.reflects;
  const secs = Math.floor((Date.now()-new Date(data.last_activity))/1000);
  document.getElementById('s-idle').textContent = secs+'s';

  document.getElementById('agents').innerHTML = Object.entries(data.agents).map(([n,a]) => {
    const c = a.state==='working'?'y':a.state==='idle'||a.state==='online'?'g':'r';
    return `<tr><td class="c">${n}</td><td class="${c}">${a.state}</td><td class="d">${esc(a.packet_type||'')} ${(a.packet_id||'').slice(0,8)}</td></tr>`;
  }).join('');

  document.getElementById('rep').innerHTML = (data.reputation||[]).map(r => {
    const c = r.score>=0.8?'g':r.score>=0.5?'y':'r';
    return `<tr><td class="c">${r.agent}</td><td>${r.capability}</td><td class="${c}">${(r.score*100).toFixed(0)}%</td><td class="d">${r.successes}/${r.failures}</td></tr>`;
  }).join('');

  // "Nothing outstanding at all" for a capability with no active agent for
  // it is normal, not stuck -- redelivered>0 just means JetStream resent a
  // message at some point (could be an old restart), not an active problem
  // on its own. The duplicate-completions banner below is the real signal.
  document.getElementById('health').innerHTML = (data.stream_health||[]).map(h => {
    if (h.error) return `<tr><td class="c">${h.capability}</td><td class="d" colspan="3">unavailable</td></tr>`;
    const redC = h.redelivered > 0 ? 'y' : 'g';
    return `<tr><td class="c">${h.capability}</td><td>${h.pending}</td><td>${h.outstanding_acks}</td><td class="${redC}">${h.redelivered}</td></tr>`;
  }).join('');

  const dupBanner = document.getElementById('dup-banner');
  if ((data.duplicate_packets||[]).length > 0) {
    dupBanner.style.display = 'block';
    document.getElementById('dup-rows').innerHTML = data.duplicate_packets.map(d =>
      `<tr><td class="d">${d.id}</td><td>${d.capability}</td><td class="r">${d.timestamps.length}</td></tr>`
    ).join('');
  } else {
    dupBanner.style.display = 'none';
  }

  // Published by tests/check_plateau.py after each hourly CronJob run (the
  // only component with git access to the tests/results history this is
  // computed from) -- absent until the first run after this feature deploys.
  const ts = data.tier_status;
  document.getElementById('tier-rows').innerHTML = !ts
    ? `<tr><td class="d" colspan="3">no data yet — waiting on next scheduled run</td></tr>`
    : Object.keys(ts.streaks).map(tier => {
        const active = Number(tier) === ts.active_tier;
        return `<tr><td class="c">Tier ${tier}</td><td>${ts.streaks[tier]} / ${ts.streak_target}</td><td class="${active ? 'g' : 'd'}">${active ? '● active' : ''}</td></tr>`;
      }).join('');

  const evaluations = data.candidate_evaluations || [];
  document.getElementById('candidate-rows').innerHTML = evaluations.length === 0
    ? `<tr><td class="d" colspan="6">no evaluated candidates</td></tr>`
    : evaluations.map(e => {
        const promotable = e.recommendation === 'recommend_promotion';
        const recClass = promotable ? 'g' : e.recommendation?.startsWith('reject') ? 'r' : 'y';
        const action = promotable
          ? `<button onclick="promoteCandidate('${esc(e.candidate_id)}')">Promote</button>`
          : '';
        const baseline = e.baseline_pass_rate == null ? '-' : `${(e.baseline_pass_rate * 100).toFixed(0)}%`;
        const candidate = e.candidate_pass_rate == null ? '-' : `${(e.candidate_pass_rate * 100).toFixed(0)}%`;
        return `<tr><td class="d">${esc(e.candidate_id)}</td><td>${esc(e.target_role || '-')}</td><td>${baseline}</td><td>${candidate}</td><td class="${recClass}">${esc(e.recommendation || '-')}</td><td>${action}</td></tr>`;
      }).join('');

  // Packets table only ever holds FINISHED packets (on_result fires on
  // done/error) — nothing in-flight ever appears there on its own. Show
  // what's currently active by synthesizing a row per agent that's
  // "working" right now, from the Agents panel's live state, pinned above
  // the historical (newest-first) list below.
  const liveRows = Object.entries(data.agents)
    .filter(([n, a]) => a.state === 'working' && a.packet_id)
    .map(([n, a]) => `<tr style="background:#1a1a0a">
      <td class="d">${esc(a.packet_id)}</td><td>${esc(a.packet_type || '')}</td><td>-</td>
      <td class="y">⟳ live</td><td>-</td>
      <td class="d" style="max-width:180px;overflow:hidden;white-space:nowrap">${esc(n)} working…</td>
    </tr>`).join('');

  document.getElementById('pkts').innerHTML = liveRows + data.packets.slice().reverse().map(p => {
    const c = p.status==='done'?'g':p.status==='error'?'r':'y';
    return `<tr data-clickable onclick="showOutput('${p.id}')">
      <td class="d">${p.id}</td><td>${p.type}</td><td>${p.capability}</td>
      <td class="${c}">${p.status}</td><td>${p.score?.toFixed(2)||'-'}</td>
      <td class="d" style="max-width:180px;overflow:hidden;white-space:nowrap">${esc((p.goal||'').slice(0,50))}</td>
    </tr>`;
  }).join('');

  // Auto-show latest completed output (any type with output)
  const done = data.packets.filter(p=>p.status==='done'&&p.output).pop();
  if (done && (document.getElementById('out-content').textContent.startsWith('Click')||document.getElementById('out-content').textContent.startsWith('⟳'))) showOutput(done.id);

  // Thinking stream
  const el = document.getElementById('thinking');
  const atBottom = el.scrollTop+el.clientHeight >= el.scrollHeight-10;
  (data.thinking||[]).forEach(t => {
    const key = t.ts+t.agent+t.text.slice(0,40);
    if (seenThinking.has(key)) return;
    seenThinking.add(key);
    const div = document.createElement('div');
    div.className = 'log-line';
    const col = t.stream?'#444':t.text.includes('✓')||t.text.includes('▶')?'#0f0':t.text.includes('✗')?'#f44':'#888';
    div.innerHTML = `<span class="d">${t.ts}</span> <span class="c">${t.agent}</span> <span style="color:${col}">${esc(t.text)}</span>`;
    el.appendChild(div);
  });
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// Drag to resize output panel
(function(){
  const handle = document.getElementById('output-drag');
  if (!handle) return;
  let dragging = false, startY, startOutH, startMidH;
  const outWrap = document.getElementById('out-wrap');
  const mid = document.getElementById('mid-row');
  
  handle.addEventListener('mousedown', e => {
    dragging = true;
    startY = e.clientY;
    startOutH = outWrap.offsetHeight;
    startMidH = mid.offsetHeight;
    handle.style.background = 'linear-gradient(to bottom, #00ff00 0%, #ffff00 50%, #00ff00 100%)';
    e.preventDefault();
  });
  
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const delta = startY - e.clientY;
    const newOutH = Math.max(80, startOutH + delta);
    const newMidH = Math.max(100, startMidH - delta);
    outWrap.style.maxHeight = newOutH + 'px';
    mid.style.height = newMidH + 'px';
  });
  
  document.addEventListener('mouseup', () => {
    if (dragging) {
      dragging = false;
      handle.style.background = 'linear-gradient(to bottom, #1a6600 0%, #00ff00 50%, #1a6600 100%)';
    }
  });
})();

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>""")


@app.get("/api/state")
async def get_state():
    store = get_store()
    reputation = [
        {"agent": r.agent_id, "capability": r.capability,
         "score": r.score, "successes": r.successes, "failures": r.failures}
        for r in store.all_reputations()
    ]
    return JSONResponse({
        "agents": state["agents"],
        "packets": state["packets"],
        "thinking": list(state["thinking"])[-100:],
        "stats": state["stats"],
        "reputation": reputation,
        "last_activity": state["last_activity"],
        "halt": await get_halt(),
        "stream_health": await get_stream_health(),
        "duplicate_packets": get_duplicate_packets(),
        "tier_status": await get_tier_status(),
        "candidate_evaluations": await get_candidate_evaluations(),
    })
