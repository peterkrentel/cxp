# Platform Schema and Evidence Loop Plan

> **Status: implemented, pending deployment.** Automatic held-out evaluation is intentionally executor-only in this first release; planner and verifier candidates are retained for review until they have their own isolated evaluation paths.

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

- [x] Write failing tests for every valid/invalid contract and for old packets without `schema_version`.
- [x] Add the default version and minimal models/dispatcher.
- [x] Move planner, verifier, and assessor parsing behind contract parsers without changing routing.
- [x] Run `make sync`; add `contracts.py` explicitly to `app-code.yaml` because ConfigMap source files are enumerated.
- [x] Run the focused tests, then existing planner/stream parsing tests.

**Acceptance:** contract errors identify the producer; current packets remain routable.

### Task 2: Attempt evidence

**Files:** `src/memory.py`, `src/agent_shell.py`, producing agents, Helm mirrors, `tests/unit/test_attempt_memory.py`

- [x] Write failing tests for success, contract failure, and platform failure records, including bounded concurrent merge behavior.
- [x] Implement `MemoryStore.add_attempt()` using the existing flock and delta-merge pattern.
- [x] Record raw/normalized output after a call and contract parse; telemetry export must not be required.
- [x] Mark timeout/halt/API interruption evidence as platform-unhealthy.
- [x] Run focused tests, then `pytest tests/unit`.

**Acceptance:** a restart cannot erase the evidence needed to distinguish an output failure from a platform failure.

### Task 3: Responsible feedback

**Files:** verifier/reflect agents, contracts/evidence modules, Helm mirrors, `tests/unit/test_feedback_routing.py`

- [x] Write failing tests for planner JSON, executor artifact formatting, verifier schema, and infrastructure failures.
- [x] Add `target_role` and `source_attempt_id` to candidate records/reflect packets.
- [x] Replace the hard-coded executor target with validated candidate targeting.
- [x] Exclude `environment_healthy=False` and judgment-only candidates from automatic evaluation and promotion; retain them as reviewable proposals.
- [x] Run focused tests, then the full unit suite.

**Acceptance:** a planner failure cannot rewrite executor skill text, and an outage cannot create a candidate.

### Task 4: Candidate evaluation and human promotion

**Files:** new `tests/evaluate_candidate.py`, `tests/run_tests.py`, `tests/check_plateau.py`, web dashboard, Helm mirrors, `tests/unit/test_candidate_evaluation.py`

- [x] Write failing tests for insufficient evidence, platform interruption, regression, and successful recommendation.
- [x] Compare executor candidates and active skill on held-out deterministic validators.
- [x] Publish a NATS KV report and retain it in result history: baseline, candidate, excluded failures, recommendation.
- [x] Show candidate/recommendation state in the dashboard; human approval remains the sole promoter, with the applied revision recorded on promotion.
- [x] Run unit tests; the existing sequential CronJob evaluates at most one healthy, unevaluated executor candidate after the ordinary suite.

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
