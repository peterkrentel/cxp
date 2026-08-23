"""CXP — Context Exchange Protocol packet definition."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


class PacketType(str, Enum):
    PLAN = "plan"
    CODE = "code"
    VERIFY = "verify"
    REFLECT = "reflect"
    DIAGNOSE = "diagnose"
    MEMORY = "memory"
    ROUTE = "route"


class PacketStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ERROR = "error"


class TraceEntry(BaseModel):
    agent: str
    action: str  # created | claimed | completed | errored | reflected
    timestamp: str = Field(default_factory=_now)
    notes: str = ""


class Payload(BaseModel):
    goal: str
    context: str = ""
    instructions: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    output: str = ""          # agent writes result here
    error_detail: str = ""


class RoutingHints(BaseModel):
    next_type: PacketType | None = None
    preferred_capability: str = "any"


class CXPPacket(BaseModel):
    id: str = Field(default_factory=_uid)
    schema_version: str = "1.0"
    created_at: str = Field(default_factory=_now)
    origin: str = "human"
    target: str = "any"        # agent-id or "any"
    type: PacketType
    capability: str = "any"    # what capability is required
    priority: int = 1          # higher = more urgent
    ttl: int = 5               # max hops before expiry
    task_id: str = Field(default_factory=_uid)
    parent_packet_id: str = ""
    status: PacketStatus = PacketStatus.PENDING
    payload: Payload
    routing_hints: RoutingHints = Field(default_factory=RoutingHints)
    trace: list[TraceEntry] = Field(default_factory=list)
    # self-reflection score set by verifier / reflect agents
    quality_score: float | None = None

    def append_trace(self, agent: str, action: str, notes: str = "") -> None:
        self.trace.append(TraceEntry(agent=agent, action=action, notes=notes))

    def claim(self, agent_id: str) -> None:
        self.status = PacketStatus.IN_PROGRESS
        self.append_trace(agent_id, "claimed")

    def complete(self, agent_id: str, output: str, notes: str = "") -> None:
        self.payload.output = output
        self.status = PacketStatus.DONE
        self.append_trace(agent_id, "completed", notes)

    def fail(self, agent_id: str, reason: str) -> None:
        self.payload.error_detail = reason
        self.status = PacketStatus.ERROR
        self.ttl -= 1
        self.append_trace(agent_id, "errored", reason)

    def hop(self) -> bool:
        """Decrement TTL and return False if expired."""
        self.ttl -= 1
        return self.ttl > 0
