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
                    "type": packet.type.value,
                    "capability": packet.capability,
                    "status": packet.status.value,
                    "score": packet.quality_score,
                    "timestamp": datetime.now().isoformat(),
                }
            )
            state["packets"] = state["packets"][-50:]  # Keep last 50

            # Update stats
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
    """Serve the HTML dashboard."""
    return HTMLResponse(
        """
    <!DOCTYPE html>
    <html>
    <head>
        <title>CXP Swarm</title>
        <style>
            body { font-family: monospace; background: #1a1a1a; color: #0f0; margin: 0; padding: 20px; }
            .container { max-width: 1400px; margin: 0 auto; }
            .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
            .panel { background: #2a2a2a; border: 1px solid #0f0; padding: 15px; }
            h2 { color: #0f0; margin-top: 0; }
            table { width: 100%; border-collapse: collapse; font-size: 12px; }
            th { background: #0f0; color: #000; padding: 8px; text-align: left; }
            td { padding: 6px 8px; border-bottom: 1px solid #333; }
            tr:hover { background: #333; }
            .done { color: #0f0; }
            .error { color: #f00; }
            .working { color: #ff0; }
            .stat { font-size: 24px; color: #0f0; font-weight: bold; }
            .stat-label { color: #888; font-size: 12px; }
        </style>
        <script>
            async function refresh() {
                const res = await fetch('/api/state');
                const data = await res.json();
                
                // Update stats
                document.getElementById('tasks-done').textContent = data.stats.tasks_done;
                document.getElementById('tasks-error').textContent = data.stats.tasks_error;
                document.getElementById('llm-calls').textContent = data.stats.llm_calls;
                document.getElementById('reflects').textContent = data.stats.reflects;
                
                // Update agents
                let agentsHTML = '<tr><th>Agent</th><th>State</th><th>Working On</th></tr>';
                for (const [name, info] of Object.entries(data.agents)) {
                    const color = info.state === 'working' ? 'working' : info.state === 'online' ? 'done' : 'error';
                    agentsHTML += `<tr><td>${name}</td><td class="${color}">${info.state}</td><td>${info.packet_id || '-'}</td></tr>`;
                }
                document.getElementById('agents-table').innerHTML = agentsHTML;
                
                // Update packets
                let packetsHTML = '<tr><th>ID</th><th>Type</th><th>Cap</th><th>Status</th><th>Score</th></tr>';
                for (const p of data.packets.slice().reverse()) {
                    const color = p.status === 'done' ? 'done' : p.status === 'error' ? 'error' : 'working';
                    packetsHTML += `<tr><td>${p.id}</td><td>${p.type}</td><td>${p.capability}</td><td class="${color}">${p.status}</td><td>${p.score?.toFixed(2) || '-'}</td></tr>`;
                }
                document.getElementById('packets-table').innerHTML = packetsHTML;
            }
            
            setInterval(refresh, 2000);
            refresh();
        </script>
    </head>
    <body>
        <div class="container">
            <h1>🤖 CXP Swarm Dashboard</h1>
            
            <div class="grid">
                <div class="panel">
                    <h2>Tasks Completed</h2>
                    <div class="stat" id="tasks-done">-</div>
                    <div class="stat-label">✓ done</div>
                </div>
                <div class="panel">
                    <h2>Tasks Failed</h2>
                    <div class="stat" id="tasks-error" style="color: #f00;">-</div>
                    <div class="stat-label">✗ error</div>
                </div>
                <div class="panel">
                    <h2>LLM Calls</h2>
                    <div class="stat" id="llm-calls">-</div>
                    <div class="stat-label">code generation</div>
                </div>
                <div class="panel">
                    <h2>Skill Updates</h2>
                    <div class="stat" id="reflects">-</div>
                    <div class="stat-label">self-improvements</div>
                </div>
            </div>
            
            <div class="grid">
                <div class="panel">
                    <h2>Agents</h2>
                    <table id="agents-table"></table>
                </div>
                <div class="panel">
                    <h2>Reputation</h2>
                    <p style="color: #888;">Loading...</p>
                </div>
            </div>
            
            <div class="panel">
                <h2>Recent Packets</h2>
                <table id="packets-table"></table>
            </div>
        </div>
    </body>
    </html>
    """
    )


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
