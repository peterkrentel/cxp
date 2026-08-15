"""Web dashboard — HTTP API to view swarm state in browser."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import nats
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .agent_shell import NATS_URL, SUBJECT_DASHBOARD, SUBJECT_RESULTS
from .memory import get_store
from .packet import CXPPacket

app = FastAPI(title="CXP Swarm Dashboard")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# State
state = {
    "agents": {},
    "packets": [],
    "stats": {"tasks_done": 0, "tasks_error": 0, "llm_calls": 0, "reflects": 0},
    "last_activity": datetime.now().isoformat(),
}


async def subscribe_nats():
    """Background task to subscribe to NATS and update state."""
    nc = await nats.connect(NATS_URL)

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
            state["packets"].append(
                {
                    "id": packet.id[:8],
                    "full_id": packet.id,
                    "type": packet.type.value,
                    "capability": packet.capability,
                    "status": packet.status.value,
                    "score": packet.quality_score,
                    "goal": packet.payload.goal if packet.payload else "",
                    "output": packet.payload.output if packet.payload else "",
                    "instructions": packet.payload.instructions if packet.payload else "",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            state["packets"] = state["packets"][-50:]

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

    await nc.subscribe(SUBJECT_DASHBOARD, cb=on_dashboard)
    await nc.subscribe(SUBJECT_RESULTS, cb=on_result)

    # Keep connection alive
    while True:
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    asyncio.create_task(subscribe_nats())


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
.panel { border: 1px solid #1a6600; padding: 6px; flex: 1; }
.panel-title { color: #fff; text-align: center; border-bottom: 1px solid #1a6600; padding-bottom: 4px; margin-bottom: 6px; font-size: 11px; }
table { width: 100%; border-collapse: collapse; }
th { color: #ff00ff; text-align: left; padding: 2px 6px; font-size: 11px; }
td { padding: 2px 6px; }
.green { color: #00ff00; } .yellow { color: #ffff00; } .red { color: #ff4444; } .cyan { color: #00ffff; } .dim { color: #555; } .white { color: #ccc; }
.log { height: 140px; overflow-y: auto; }
.log div { padding: 1px 0; white-space: pre; }
.output { background: #0a0a0a; border: 1px solid #1a6600; padding: 8px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; color: #ccc; font-size: 11px; }
.stat-row { display: flex; gap: 8px; margin-bottom: 8px; }
.stat-box { border: 1px solid #1a6600; padding: 6px 12px; flex: 1; text-align: center; }
.stat-val { font-size: 22px; font-weight: bold; }
</style>
</head>
<body>
<div class="stat-row">
  <div class="stat-box"><div class="stat-val green" id="s-done">-</div><div class="dim">Tasks Done</div></div>
  <div class="stat-box"><div class="stat-val red" id="s-error">-</div><div class="dim">Failed</div></div>
  <div class="stat-box"><div class="stat-val cyan" id="s-llm">-</div><div class="dim">LLM Calls</div></div>
  <div class="stat-box"><div class="stat-val" style="color:#ff00ff" id="s-reflect">-</div><div class="dim">Skill Updates</div></div>
  <div class="stat-box"><div class="stat-val yellow" id="s-idle">-</div><div class="dim">Last Activity</div></div>
</div>

<div class="row">
  <div class="panel" style="flex:1">
    <div class="panel-title">Agents</div>
    <table><thead><tr><th>Agent</th><th>State</th><th>Working On</th></tr></thead><tbody id="agents"></tbody></table>
  </div>
  <div class="panel" style="flex:1">
    <div class="panel-title">Reputation</div>
    <table><thead><tr><th>Agent</th><th>Capability</th><th>Score</th><th>✓/✗</th></tr></thead><tbody id="rep"></tbody></table>
  </div>
</div>

<div class="panel" style="margin-bottom:8px">
  <div class="panel-title">Recent Packets</div>
  <table><thead><tr><th>ID</th><th>Type</th><th>Cap</th><th>Status</th><th>Score</th><th>Goal</th></tr></thead><tbody id="pkts"></tbody></table>
</div>

<div class="panel" style="margin-bottom:8px">
  <div class="panel-title">Latest Output — <span id="out-goal" class="yellow"></span></div>
  <div class="output" id="out-content">Waiting for first completed task…</div>
</div>

<div class="panel">
  <div class="panel-title">Live Log</div>
  <div class="log" id="log"></div>
</div>

<script>
let logLines = [];

async function refresh() {
  const data = await fetch('/api/state').then(r=>r.json());

  document.getElementById('s-done').textContent = data.stats.tasks_done;
  document.getElementById('s-error').textContent = data.stats.tasks_error;
  document.getElementById('s-llm').textContent = data.stats.llm_calls;
  document.getElementById('s-reflect').textContent = data.stats.reflects;
  const secs = Math.floor((Date.now() - new Date(data.last_activity)) / 1000);
  document.getElementById('s-idle').textContent = secs + 's ago';

  // Agents
  document.getElementById('agents').innerHTML = Object.entries(data.agents).map(([name, a]) => {
    const c = a.state==='working'?'yellow':a.state==='online'||a.state==='idle'?'green':'red';
    return `<tr><td class="cyan">${name}</td><td class="${c}">${a.state}</td><td class="dim">${a.packet_type||''} ${a.packet_id||''}</td></tr>`;
  }).join('');

  // Reputation
  document.getElementById('rep').innerHTML = (data.reputation||[]).map(r => {
    const c = r.score>=0.8?'green':r.score>=0.5?'yellow':'red';
    return `<tr><td class="cyan">${r.agent}</td><td>${r.capability}</td><td class="${c}">${(r.score*100).toFixed(0)}%</td><td class="dim">${r.successes}/${r.failures}</td></tr>`;
  }).join('');

  // Packets
  document.getElementById('pkts').innerHTML = data.packets.slice().reverse().map(p => {
    const c = p.status==='done'?'green':p.status==='error'?'red':'yellow';
    const goal = (p.goal||'').substring(0,60);
    return `<tr><td class="dim">${p.id}</td><td>${p.type}</td><td>${p.capability}</td><td class="${c}">${p.status}</td><td>${p.score?.toFixed(2)||'-'}</td><td class="dim">${goal}</td></tr>`;
  }).join('');

  // Latest output
  const done = data.packets.filter(p=>p.type==='code'&&p.status==='done'&&p.output).pop();
  if (done) {
    document.getElementById('out-goal').textContent = done.goal||'';
    document.getElementById('out-content').textContent = done.output;
  }

  // Live log - append new packets as log lines
  const newest = data.packets.slice(-5).reverse();
  newest.forEach(p => {
    const key = p.id + p.status;
    if (!logLines.includes(key)) {
      logLines.push(key);
      const ts = new Date().toTimeString().slice(0,8);
      const icon = p.status==='done'?'✓':p.status==='error'?'✗':'⟳';
      const score = p.score ? ` score=${p.score.toFixed(2)}` : '';
      const el = document.getElementById('log');
      const line = document.createElement('div');
      line.innerHTML = `<span class="dim">${ts}</span> ${icon} [${p.type}] <span class="dim">${p.id}</span> cap=${p.capability}${score}`;
      el.appendChild(line);
      el.scrollTop = el.scrollHeight;
    }
  });
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>""")


@app.get("/api/state")
async def get_state():
    """Return current swarm state."""
    store = get_store()
    reputation = []
    for rep in store.all_reputations():
        reputation.append(
            {
                "agent": rep.agent_id,
                "capability": rep.capability,
                "score": rep.score,
                "successes": rep.successes,
                "failures": rep.failures,
            }
        )

    return JSONResponse(
        {
            "agents": state["agents"],
            "packets": state["packets"],
            "stats": state["stats"],
            "reputation": reputation,
            "last_activity": state["last_activity"],
        }
    )
