# Platform Schema and Evidence Loop Plan

> **Status: design only.** This document does not authorize implementation. It defines the next learning-system layer for CXP on its current small local models and local kind cluster.

**Goal:** Evolve CXP from direct prompt rewriting into controlled improvement: typed capability contracts, durable evidence, responsible-role feedback, and candidate skill revisions evaluated before promotion.

**Architecture:** Keep `CXPPacket` as the transport envelope. Add a backward-compatible `schema_version` and validate each agent's result at the boundary where it is produced. Planner, verifier, and assessor produce structured JSON contracts; executor keeps returning raw code/YAML/Markdown, which the platform wraps and normalizes rather than requiring a second model-generated JSON wrapper. Store bounded attempt evidence in the existing shared memory PVC. Candidate skills live separately from active `cxp-skills` entries and are evaluated against held-out deterministic tests before a human promotes them.

**Non-goals:** This is not model-weight fine-tuning, autonomous promotion, a new queue, or removal of the existing halt gate. Prompt/skill revisions remain the adaptation mechanism until a separate, curated fine-tuning dataset exists.

## Specification

### Versioned contracts

Add `schema_version: str = "1.0"` to `CXPPacket`; old packets must remain valid through the default. Add `src/contracts.py` with:

```python
class PlanResult(BaseModel):
    subtasks: list[PlannedTask]

class ArtifactResult(BaseModel):
    content: str
    format: Literal["python", "yaml", "markdown", "text"]

class VerificationResult(BaseModel):
    score: float = Field(ge=0, le=1)
    passed: bool
    issues: list[str]
    suggestion: str

class AssessmentResult(BaseModel):
    labels: list[str]
    verdict: str
    strengths: list[str]
    gaps: list[str]

class SkillRevisionCandidate(BaseModel):
    target_role: Literal["planner", "executor", "verifier"]
    content: str
    source_attempt_id: str
    rationale: str
```

One `parse_contract(capability, raw_text, expected_format=None)` dispatcher owns normalization. It removes one outer Markdown fence for executor Python/YAML output and retains both raw and normalized text. Existing planner trailing-comma repair moves into the plan parser; parsing rules must not remain scattered across agents.

### Durable attempt evidence

Add a capped `attempts` collection to `MemoryStore`, merged under the existing file lock. Each record contains:

```text
attempt_id, packet_id, task_id, role, capability, schema_version,
skill_revision, prompt_hash, raw_response, normalized_response,
validation_status, validation_issues, outcome, environment_healthy, timestamp
```

Timeouts, swarm halts, unavailable web API, and rollout interruptions set `environment_healthy=False`. They are preserved for operations, but never become learning examples.

### Candidate and promotion policy

- Contract failures target the producing role: malformed plan JSON -> planner; fenced YAML -> executor; invalid verifier JSON -> verifier.
- Reflect writes `SkillRevisionCandidate` entries to a separate `cxp-skill-candidates` KV bucket. It never directly replaces an active skill.
- Planner, executor, and verifier are candidates because they already have seeded/live skills. Assessor failures are recorded only until it has both a skill and deterministic evaluation path.
- A candidate is compared with the active skill on a fixed held-out deterministic subset. The source failure is never its only evidence.
- Initial promotion remains human-approved. Promotion requires platform-healthy evaluation, improvement, and no deterministic Tier 0 regression.

## Test-First Implementation Steps

### Task 1: Contracts and compatibility

**Files:** `src/packet.py`, new `src/contracts.py`, `helm/cxp/templates/app-code.yaml`, Helm mirror, `tests/unit/test_contracts.py`

- [ ] Write failing tests for every valid/invalid contract and for old packets without `schema_version`.
- [ ] Add the default version and minimal models/dispatcher.
- [ ] Move planner, verifier, and assessor parsing behind contract parsers without changing routing.
- [ ] Run `make sync`; add `contracts.py` explicitly to `app-code.yaml` because ConfigMap source files are enumerated.
- [ ] Run the focused tests, then existing planner/stream parsing tests.

**Acceptance:** contract errors identify the producer; current packets remain routable.

### Task 2: Attempt evidence

**Files:** `src/memory.py`, `src/agent_shell.py`, producing agents, Helm mirrors, `tests/unit/test_attempt_memory.py`

- [ ] Write failing tests for success, contract failure, and platform failure records, including bounded concurrent merge behavior.
- [ ] Implement `MemoryStore.add_attempt()` using the existing flock and delta-merge pattern.
- [ ] Record raw/normalized output after a call and contract parse; telemetry export must not be required.
- [ ] Mark timeout/halt/API interruption evidence as platform-unhealthy.
- [ ] Run focused tests, then `pytest tests/unit`.

**Acceptance:** a restart cannot erase the evidence needed to distinguish an output failure from a platform failure.

### Task 3: Responsible feedback

**Files:** verifier/reflect agents, contracts/evidence modules, Helm mirrors, `tests/unit/test_feedback_routing.py`

- [ ] Write failing tests for planner JSON, executor artifact formatting, verifier schema, and infrastructure failures.
- [ ] Add `target_role` and `source_attempt_id` to candidate records/reflect packets.
- [ ] Replace the hard-coded executor target with validated candidate targeting.
- [ ] Refuse candidates from `environment_healthy=False` or judgment-only evidence.
- [ ] Run focused tests, then the full unit suite.

**Acceptance:** a planner failure cannot rewrite executor skill text, and an outage cannot create a candidate.

### Task 4: Candidate evaluation and human promotion

**Files:** new `tests/evaluate_candidate.py`, `tests/run_tests.py`, `tests/check_plateau.py`, web dashboard, Helm mirrors, `tests/unit/test_candidate_evaluation.py`

- [ ] Write failing tests for insufficient evidence, platform interruption, regression, and successful recommendation.
- [ ] Compare candidate and active skills on held-out deterministic validators.
- [ ] Publish a NATS KV report and retain it in result history: baseline, candidate, excluded failures, recommendation.
- [ ] Show active/candidate/recommendation state in the dashboard; human approval remains the sole promoter.
- [ ] Run unit tests; then let the existing sequential CronJob collect the first in-cluster evaluation after platform health is green.

**Acceptance:** each active-skill change is traceable to evidence, evaluation, and a human promotion event.

## Test Strategy

1. Install the declared `requirements.txt` and `requirements-dev.txt` dependencies, then write and run a focused failing test before every implementation edit.
2. Run the focused test after each smallest change, followed by `PYENV_VERSION=3.11.0 python -m pytest tests/unit -q`.
3. Preserve the existing sequential CronJob for end-to-end validation. Tag each result as `platform`, `contract`, `deterministic-validator`, or `judgment`; only contract and deterministic-validator evidence may inform candidates.
4. Keep at least one deterministic test per capability held out from candidate generation and require it in promotion evaluation.

## Future Betterment Questions

- When candidate reports stabilize, should repeated deterministic wins auto-promote with a rollback window, or remain human-approved?
- Should a future larger evaluator model assess security/documentation while deterministic validators stay the promotion floor?
- What data-quality and privacy review should gate creation of a genuine fine-tuning dataset?
