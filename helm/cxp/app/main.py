"""Entry points: run an agent role or the dashboard."""

from __future__ import annotations

import asyncio
import logging
import sys


def main() -> None:
    # Without this, log.info()/log.warning()/log.error() calls throughout
    # agent_shell.py and the agents are silently dropped -- Python's root
    # logger has no handler unless something calls basicConfig, so nothing
    # ever reached `kubectl logs`. Found live 2026-08-17 chasing a heartbeat
    # bug: a 2-minute wait for "heartbeat sent"/"heartbeat FAILED" log lines
    # found neither, which looked like evidence about the heartbeat itself
    # but was really just this gap -- the calls were firing the whole time.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    role = sys.argv[1] if len(sys.argv) > 1 else "dashboard"

    if role == "planner":
        from src.agents.planner import PlannerAgent
        asyncio.run(PlannerAgent().run())

    elif role == "executor":
        from src.agents.executor import ExecutorAgent
        asyncio.run(ExecutorAgent().run())

    elif role == "verifier":
        from src.agents.verifier import VerifierAgent
        asyncio.run(VerifierAgent().run())

    elif role == "reflect":
        from src.agents.reflect import ReflectAgent
        asyncio.run(ReflectAgent().run())

    elif role == "assessor":
        from src.agents.assessor import AssessorAgent
        asyncio.run(AssessorAgent().run())

    elif role == "deployer":
        from src.agents.deployer import DeployerAgent
        asyncio.run(DeployerAgent().run())

    elif role == "diagnostician":
        from src.agents.diagnostician import DiagnosticianAgent
        asyncio.run(DiagnosticianAgent().run())

    elif role == "dashboard":
        from src.dashboard import Dashboard
        asyncio.run(Dashboard().run())

    elif role == "web":
        # HTTP web dashboard
        import uvicorn
        from src.web_dashboard import app
        uvicorn.run(app, host="0.0.0.0", port=8080)

    elif role == "idle":
        # keeps the pod alive so you can exec in and run the dashboard manually
        import time
        while True:
            time.sleep(3600)

    elif role == "submit":
        # submit a task from the CLI: python main.py submit "your goal here"
        goal = " ".join(sys.argv[2:]) or "scaffold a Redis StatefulSet for Kubernetes with persistence"
        asyncio.run(_submit_task(goal))

    else:
        print(f"Unknown role: {role}")
        print("Usage: python main.py [planner|executor|verifier|reflect|dashboard|submit <goal>]")
        sys.exit(1)


async def _submit_task(goal: str) -> None:
    import nats
    from src.agent_shell import NATS_URL
    from src.packet import CXPPacket, PacketType, Payload

    nc = await nats.connect(NATS_URL)
    packet = CXPPacket(
        origin="human",
        type=PacketType.PLAN,
        capability="plan",
        priority=3,
        payload=Payload(goal=goal),
    )
    packet.append_trace("human", "created", "submitted via CLI")
    # Route directly to the plan capability subject, via JetStream so the
    # publish is confirmed durably stored before this process exits
    await nc.jetstream().publish("cxp.cap.plan", packet.model_dump_json().encode())
    await nc.drain()
    print(f"Task submitted: {packet.task_id[:8]}  goal='{goal}'")


if __name__ == "__main__":
    main()
