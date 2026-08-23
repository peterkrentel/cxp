"""Verifier agent — grades executor output and routes next steps."""

from __future__ import annotations

import json
import logging

from ..agent_shell import AgentShell
from ..contracts import parse_contract
from ..candidate_evaluation import build_self_improvement_inputs
from ..packet import CXPPacket, PacketType, Payload, RoutingHints

log = logging.getLogger(__name__)

BASE_SYSTEM = """You are a verification specialist in a distributed AI swarm.
Given an artifact and its original goal, evaluate:
1. Correctness — does it achieve the goal?
2. Completeness — is anything missing?
3. Safety — are there obvious security or reliability issues?

Return ONLY a JSON object:
{
  "score": <float 0.0-1.0>,
  "passed": <true|false>,
  "issues": ["issue1", "issue2"],
  "suggestion": "one sentence on biggest improvement if failed"
}
No prose, no fences.
"""


class VerifierAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("verifier-1", capabilities=["verify"])

    async def _execute(self, packet: CXPPacket) -> str:
        skill = await self.get_skill("verifier", fallback_path="/skills/verifier_v1.md")
        prompt = (
            f"Original goal: {packet.payload.goal}\n\n"
            f"Artifact to verify:\n{packet.payload.context}"
        )
        raw = await self.llm(BASE_SYSTEM + skill, prompt, packet_id=packet.id,
                              task_id=packet.task_id, parent_packet_id=packet.parent_packet_id,
                              json_mode=True)

        validation_status = "valid"
        validation_issues: list[str] = []
        try:
            result = parse_contract("verify", raw).model_dump()
        except Exception as e:
            validation_status = "contract_error"
            validation_issues = [str(e)]
            await self.record_validation_failure("verify response JSON parse", f"{e}\nRaw: {raw[:200]}")
            # Default to fail if we can't parse -- a genuinely unexpected
            # (non-contract) exception here must still degrade gracefully
            # rather than propagate and halt the swarm over one bad response.
            result = {"score": 0.0, "passed": False, "issues": [f"Parse error: {e}"], "suggestion": "Malformed response"}

        await self.record_attempt(
            packet=packet,
            capability="verify",
            raw_response=raw,
            normalized_response=json.dumps(result, separators=(",", ":")),
            validation_status=validation_status,
            validation_issues=validation_issues,
            environment_healthy=True,
            skill_revision=packet.payload.inputs.get("skill_revision"),
            persist=False,
        )

        packet.quality_score = float(result.get("score") if result.get("score") is not None else 0.5)

        # A candidate-comparison run (evaluation_run) or an explicit
        # unvetted candidate skill (candidate_id, reachable via /api/submit
        # with no auth on this prototype) must never feed the regression
        # baseline, stage a new skill candidate, or trigger a real
        # deployment -- one flag guards every one of those side effects,
        # regardless of who set it.
        is_candidate_traffic = bool(
            packet.payload.inputs.get("evaluation_run") or packet.payload.inputs.get("candidate_id")
        )

        if not is_candidate_traffic:
            # Record which skill revision produced this artifact and how it
            # scored — the only way to actually measure whether reflect's
            # rewrites are improving output over time, instead of assuming so.
            self._memory.add_episodic({
                "capability": packet.payload.inputs.get("capability", "code"),
                "skill_revision": packet.payload.inputs.get("skill_revision"),
                "score": packet.quality_score,
                "goal": packet.payload.goal,
            })

        # One save covers both the attempt record above and the episodic
        # entry just queued -- previously two separate memory.json rewrites
        # per verify packet (record_attempt's own internal save, plus this
        # one), on top of a third from _handle_message's own save afterward.
        await self._memory.save()

        if not is_candidate_traffic and not result.get("passed", False):
            # spawn a reflect packet so the system can learn
            reflect = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="reflect",
                priority=3,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal="Self-improve: update skill based on failed verification",
                    instructions=(
                        f"Issues found: {result.get('issues', [])}\n"
                        f"Suggestion: {result.get('suggestion', '')}\n"
                        "Propose a one-paragraph update to the executor skill file to prevent this."
                    ),
                    context=packet.payload.context,
                    inputs=build_self_improvement_inputs(
                        target_role="executor", source_attempt_id=packet.id, evidence_class="judgment",
                    ),
                ),
            )
            reflect.append_trace(self.agent_id, "created", "spawned due to failed verification")
            await self.emit_packet(reflect)

        issues = result.get("issues", [])

        # Spawn assess packet for capability labeling
        assess = CXPPacket(
            origin=self.agent_id,
            type=PacketType.REFLECT,
            capability="assess",
            priority=1,
            task_id=packet.task_id,
            parent_packet_id=packet.id,
            payload=Payload(
                goal=packet.payload.goal,
                instructions="Label this artifact with AI capability tags",
                context=packet.payload.context,
            ),
        )
        log.info(f"Emitting assess packet {assess.id[:8]}")
        await self.emit_packet(assess)

        # Spawn deploy packet if score is high enough -- never for candidate
        # or evaluation traffic (see is_candidate_traffic above): the hourly
        # candidate-comparison job must never trigger a real deployment on
        # its own, and an unvetted candidate's own artifact must never be
        # auto-deployed just because it happened to score well.
        if not is_candidate_traffic and packet.quality_score is not None and packet.quality_score >= 0.85:
            deploy = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="deploy",
                priority=2,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal=packet.payload.goal,
                    instructions=str(packet.quality_score),
                    context=packet.payload.context,
                ),
            )
            log.info(f"Emitting deploy packet {deploy.id[:8]} (score={packet.quality_score})")
            await self.emit_packet(deploy)

        return json.dumps({"score": packet.quality_score, "passed": result.get("passed"), "issues": issues})
