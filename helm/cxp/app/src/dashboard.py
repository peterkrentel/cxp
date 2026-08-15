"""Terminal dashboard — live view of the swarm using Rich."""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime

import nats
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .agent_shell import NATS_URL, SUBJECT_DASHBOARD, SUBJECT_PACKETS, SUBJECT_RESULTS
from .memory import get_store
from .packet import CXPPacket

MAX_LOG = 30


class Dashboard:
    def __init__(self) -> None:
        self.console = Console()
        self.agent_states: dict[str, dict] = {}
        self.recent_packets: deque[CXPPacket] = deque(maxlen=20)
        self.log_lines: deque[str] = deque(maxlen=MAX_LOG)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # NATS listeners                                                       #
    # ------------------------------------------------------------------ #

    async def _on_dashboard(self, msg) -> None:
        try:
            data = json.loads(msg.data)
        except Exception:
            return
        async with self._lock:
            agent = data.get("agent", "?")
            self.agent_states[agent] = data
            state = data.get("state", "?")
            pid = data.get("packet_id") or ""
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_lines.append(f"[dim]{ts}[/] [cyan]{agent}[/] → {state} {pid}")

    async def _on_result(self, msg) -> None:
        try:
            packet = CXPPacket.model_validate_json(msg.data)
        except Exception:
            return
        async with self._lock:
            self.recent_packets.append(packet)
            icon = "✓" if packet.status.value == "done" else "✗"
            ts = datetime.now().strftime("%H:%M:%S")
            score = f" score={packet.quality_score:.2f}" if packet.quality_score is not None else ""
            self.log_lines.append(
                f"[dim]{ts}[/] {icon} [{packet.type.value}] {packet.id[:8]} "
                f"cap={packet.capability}{score}"
            )

    # ------------------------------------------------------------------ #
    # Render                                                               #
    # ------------------------------------------------------------------ #

    def _render_agents(self) -> Panel:
        t = Table(show_header=True, header_style="bold magenta", expand=True)
        t.add_column("Agent", style="cyan")
        t.add_column("State")
        t.add_column("Working On")
        for agent, data in sorted(self.agent_states.items()):
            state = data.get("state", "?")
            color = {"online": "green", "idle": "green", "working": "yellow", "offline": "red"}.get(state, "white")
            pid = data.get("packet_id") or ""
            ptype = data.get("packet_type") or ""
            t.add_row(agent, Text(state, style=color), f"{ptype} {pid}")
        return Panel(t, title="[bold]Agents[/]", border_style="blue")

    def _render_reputation(self) -> Panel:
        store = get_store()
        t = Table(show_header=True, header_style="bold magenta", expand=True)
        t.add_column("Agent", style="cyan")
        t.add_column("Capability")
        t.add_column("Score", justify="right")
        t.add_column("✓/✗", justify="right")
        for rep in sorted(store.all_reputations(), key=lambda r: -r.score):
            color = "green" if rep.score >= 0.8 else "yellow" if rep.score >= 0.5 else "red"
            t.add_row(
                rep.agent_id,
                rep.capability,
                Text(f"{rep.score:.0%}", style=color),
                f"{rep.successes}/{rep.failures}",
            )
        return Panel(t, title="[bold]Reputation[/]", border_style="magenta")

    def _render_packets(self) -> Panel:
        t = Table(show_header=True, header_style="bold", expand=True)
        t.add_column("ID", style="dim")
        t.add_column("Type")
        t.add_column("Cap")
        t.add_column("Status")
        t.add_column("Score", justify="right")
        for p in reversed(list(self.recent_packets)):
            status_color = {"done": "green", "error": "red", "in_progress": "yellow"}.get(p.status.value, "white")
            score_str = f"{p.quality_score:.2f}" if p.quality_score is not None else "-"
            t.add_row(
                p.id[:8],
                p.type.value,
                p.capability,
                Text(p.status.value, style=status_color),
                score_str,
            )
        return Panel(t, title="[bold]Recent Packets[/]", border_style="green")

    def _render_log(self) -> Panel:
        lines = "\n".join(self.log_lines)
        return Panel(lines or "[dim]Waiting for activity…[/]", title="[bold]Live Log[/]", border_style="yellow")

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=12),
            Layout(name="middle", size=12),
            Layout(name="bottom"),
        )
        layout["top"].split_row(Layout(name="agents"), Layout(name="reputation"))
        layout["agents"].update(self._render_agents())
        layout["reputation"].update(self._render_reputation())
        layout["middle"].update(self._render_packets())
        layout["bottom"].update(self._render_log())
        return layout

    # ------------------------------------------------------------------ #
    # Entry point                                                          #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        nc = await nats.connect(NATS_URL)
        await nc.subscribe(SUBJECT_DASHBOARD, cb=self._on_dashboard)
        await nc.subscribe(SUBJECT_RESULTS, cb=self._on_result)

        with Live(self._build_layout(), console=self.console, refresh_per_second=4, screen=True) as live:
            while True:
                await asyncio.sleep(0.25)
                async with self._lock:
                    live.update(self._build_layout())
