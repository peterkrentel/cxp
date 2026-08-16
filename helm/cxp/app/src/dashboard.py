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

from .agent_shell import KV_STATE, NATS_URL, SUBJECT_DASHBOARD, SUBJECT_PACKETS, SUBJECT_RESULTS
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
        self.task_stats = {"submitted": 0, "done": 0, "error": 0, "pending": 0}
        self.last_activity = datetime.now()
        self.llm_calls = 0
        self.reflect_rewrites = 0
        # Last completed output for viewing
        self.last_output: str = ""
        self.last_goal: str = ""
        self.halt: dict | None = None
        self._kv_cache: dict[str, object] = {}

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
            self.last_activity = datetime.now()

            if packet.task_id:
                if packet.status.value == "done":
                    self.task_stats["done"] += 1
                elif packet.status.value == "error":
                    self.task_stats["error"] += 1

            if packet.type.value == "code":
                self.llm_calls += 1
                if packet.status.value == "done" and packet.payload and packet.payload.output:
                    self.last_output = packet.payload.output
                    self.last_goal = packet.payload.goal or ""

            # capability, not type — PacketType has no ASSESS/DEPLOY value, so
            # verifier labels deploy/assess/reflect packets all as type=REFLECT.
            if packet.capability == "reflect" and packet.status.value == "done":
                self.reflect_rewrites += 1

            icon = "✓" if packet.status.value == "done" else "✗" if packet.status.value == "error" else "⟳"
            ts = datetime.now().strftime("%H:%M:%S")
            score = f" score={packet.quality_score:.2f}" if packet.quality_score is not None else ""
            goal_snippet = f" — {(packet.payload.goal or '')[:50]}" if packet.payload and packet.payload.goal else ""
            self.log_lines.append(
                f"[dim]{ts}[/] {icon} [bold]{packet.type.value}[/] {packet.id[:8]} "
                f"cap={packet.capability}{score}[dim]{goal_snippet}[/]"
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

    def _render_status(self) -> Panel:
        """Show swarm health and activity metrics."""
        t = Table.grid(padding=(0, 2))
        
        # Activity gauge
        since_activity = (datetime.now() - self.last_activity).total_seconds()
        activity_color = "green" if since_activity < 5 else "yellow" if since_activity < 15 else "red"
        activity_text = f"[{activity_color}]{'🟢' if since_activity < 5 else '🟡' if since_activity < 15 else '⚫'}[/] Last activity: {since_activity:.0f}s ago"
        
        # Task progress
        total = self.task_stats["done"] + self.task_stats["error"] + self.task_stats["pending"]
        progress_pct = (self.task_stats["done"] / total * 100) if total > 0 else 0
        progress_bar = "█" * int(progress_pct / 5) + "░" * (20 - int(progress_pct / 5))
        
        t.add_row("Tasks", f"[green]{self.task_stats['done']}✓[/] [red]{self.task_stats['error']}✗[/] [yellow]{self.task_stats['pending']}⟳[/]")
        t.add_row("Progress", f"{progress_bar} {progress_pct:.0f}%")
        t.add_row("LLM Calls", f"[cyan]{self.llm_calls}[/]")
        t.add_row("Skill Updates", f"[magenta]{self.reflect_rewrites}[/]")
        t.add_row("Status", activity_text)
        if self.halt:
            t.add_row("HALTED", f"[bold red]{self.halt.get('reason', 'unknown error')}[/]")

        return Panel(t, title="[bold]Swarm Health[/]", border_style="red" if self.halt else "cyan")

    def _render_log(self) -> Panel:
        lines = "\n".join(self.log_lines)
        return Panel(lines or "[dim]Waiting for activity…[/]", title="[bold]Live Log[/]", border_style="yellow")

    def _render_output(self) -> Panel:
        """Show the latest generated artifact."""
        if self.last_output:
            content = f"[bold yellow]Goal:[/] {self.last_goal}\n\n[green]{self.last_output[:800]}[/]"
            if len(self.last_output) > 800:
                content += f"\n[dim]... ({len(self.last_output)} chars total)[/]"
        else:
            content = "[dim]Waiting for first completed task…[/]"
        return Panel(content, title="[bold]Latest Output[/]", border_style="green")

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=8),
            Layout(name="status", size=7),
            Layout(name="middle", size=10),
            Layout(name="output", size=14),
            Layout(name="bottom"),
        )
        layout["top"].split_row(Layout(name="agents"), Layout(name="reputation"))
        layout["agents"].update(self._render_agents())
        layout["reputation"].update(self._render_reputation())
        layout["status"].update(self._render_status())
        layout["middle"].update(self._render_packets())
        layout["output"].update(self._render_output())
        layout["bottom"].update(self._render_log())
        return layout

    # ------------------------------------------------------------------ #
    # Entry point                                                          #
    # ------------------------------------------------------------------ #

    async def _poll_halt(self, nc) -> None:
        from nats.js.errors import NotFoundError
        while True:
            try:
                js = nc.jetstream()
                kv = await js.key_value(KV_STATE)
                entry = await kv.get("halt")
                data = json.loads(entry.value.decode())
                self.halt = data if data.get("halted") else None
            except NotFoundError:
                self.halt = None
            except Exception:
                pass
            await asyncio.sleep(2)

    async def run(self) -> None:
        nc = await nats.connect(NATS_URL)
        await nc.subscribe(SUBJECT_DASHBOARD, cb=self._on_dashboard)
        await nc.subscribe(SUBJECT_RESULTS, cb=self._on_result)
        asyncio.create_task(self._poll_halt(nc))

        with Live(self._build_layout(), console=self.console, refresh_per_second=4, screen=True) as live:
            while True:
                await asyncio.sleep(0.25)
                async with self._lock:
                    live.update(self._build_layout())
