"""Web dashboard — browser UI with task submission, thinking stream, and output view."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from datetime import datetime

import nats
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .agent_shell import NATS_URL, SUBJECT_DASHBOARD, SUBJECT_PACKETS, SUBJECT_RESULTS, SUBJECT_THINKING
from .memory import get_store
from .packet import CXPPacket, PacketType, Payload

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


async def subscribe_nats():
    global _nc
    _nc = await nats.connect(NATS_URL)

    async def on_dashboard(msg):
        try:
            data = json.loads(msg.data)
            state["agents"][data.get("agent")] = data
            state["last_activity"] = datetime.now().isoformat()
        except:
            pass

    async def on_result(msg):
        try:
            packet = CXPPacket.model_validate_json(msg.data)
            state["packets"].append({
                "id": packet.id[:8],
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
            if packet.type.value == "code":
                state["stats"]["llm_calls"] += 1
            if packet.type.value == "reflect":
                state["stats"]["reflects"] += 1
            state["last_activity"] = datetime.now().isoformat()
        except:
            pass

    async def on_thinking(msg):
        try:
            data = json.loads(msg.data)
            state["thinking"].append({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "agent": data.get("agent", "?"),
                "text": data.get("text", ""),
                "stream": data.get("stream", False),
            })
        except:
            pass

    await _nc.subscribe(SUBJECT_DASHBOARD, cb=on_dashboard)
    await _nc.subscribe(SUBJECT_RESULTS, cb=on_result)
    await _nc.subscribe(SUBJECT_THINKING, cb=on_thinking)
    while True:
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    asyncio.create_task(subscribe_nats())


@app.post("/api/submit")
async def submit_task(request: Request):
    body = await request.json()
    goal = body.get("goal", "").strip()
    if not goal:
        return JSONResponse({"error": "goal required"}, status_code=400)
    packet = CXPPacket(
        origin="web-ui",
        type=PacketType.PLAN,
        capability="plan",
        priority=5,
        task_id=uuid.uuid4().hex[:8],
        payload=Payload(goal=goal, instructions=goal, context=""),
    )
    if _nc:
        await _nc.publish("cxp.cap.plan", packet.model_dump_json().encode())
    return JSONResponse({"task_id": packet.task_id, "packet_id": packet.id[:8]})


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
</style>
</head>
<body>

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

<div class="row">
  <div class="panel">
    <div class="panel-title">Agents</div>
    <table><thead><tr><th>Agent</th><th>State</th><th>Working On</th></tr></thead><tbody id="agents"></tbody></table>
  </div>
  <div class="panel">
    <div class="panel-title">Reputation</div>
    <table><thead><tr><th>Agent</th><th>Cap</th><th>Score</th><th>✓/✗</th></tr></thead><tbody id="rep"></tbody></table>
  </div>
</div>

<div class="row" style="height:200px">
  <div class="panel" style="flex:1.5;display:flex;flex-direction:column">
    <div class="panel-title">Packets — click row to view output</div>
    <div class="scroll" style="flex:1">
      <table><thead><tr><th>ID</th><th>Type</th><th>Cap</th><th>Status</th><th>Score</th><th>Goal</th></tr></thead>
      <tbody id="pkts"></tbody></table>
    </div>
  </div>
  <div class="panel" style="flex:1;display:flex;flex-direction:column">
    <div class="panel-title">Agent Thinking / LLM Stream</div>
    <div class="scroll" id="thinking" style="flex:1"></div>
  </div>
</div>

<div class="panel">
  <div class="panel-title">Output — <span id="out-goal" class="y"></span></div>
  <div class="scroll" style="max-height:320px">
    <pre id="out-content">Type a task above and press Enter. Output appears here when complete.</pre>
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

  document.getElementById('pkts').innerHTML = data.packets.slice().reverse().map(p => {
    const c = p.status==='done'?'g':p.status==='error'?'r':'y';
    return `<tr data-clickable onclick="showOutput('${p.id}')">
      <td class="d">${p.id}</td><td>${p.type}</td><td>${p.capability}</td>
      <td class="${c}">${p.status}</td><td>${p.score?.toFixed(2)||'-'}</td>
      <td class="d" style="max-width:180px;overflow:hidden;white-space:nowrap">${esc((p.goal||'').slice(0,50))}</td>
    </tr>`;
  }).join('');

  // Auto-show latest completed output if user hasn't clicked
  const done = data.packets.filter(p=>p.type==='code'&&p.status==='done'&&p.output).pop();
  if (done && document.getElementById('out-content').textContent.startsWith('⟳')) showOutput(done.id);

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
    })
