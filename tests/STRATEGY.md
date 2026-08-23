# Testing Strategy

Where the test suite is today, and what happens next — written down so it isn't a mystery next time someone (including future-you) looks at this folder.

Two separate suites live here, at two different levels:

- **`tests/unit/`** — fast, deterministic pytest tests of agent-level control-plane logic (the ollama semaphore, the packet idempotency fence, diagnostician's timeout classification, planner's decomposition edge cases), run on every PR via CI's `unit-tests` job. No cluster, no LLM, no network — a fake JetStream KV (`tests/unit/conftest.py`) stands in for the real one. Added 2026-08-17 after a live duplicate-packet-processing bug shipped with no test coverage at all; run with `pytest tests/unit`.
- **Everything below this line** is the *other* suite — `run_tests.py`, an end-to-end suite against a live swarm with a real (small, non-deterministic) LLM, run hourly by the in-cluster CronJob, not on every PR.

## Where we are: SMOKE, a difficulty ladder, and a regression check

1. **SMOKE.** One trivial task ("print hello world"), always run first regardless of which difficulty tier below is active. A failure here means the pipeline itself is broken — a different, more urgent signal than "bad at capability X." The suite still continues after a smoke failure, for more signal.
2. **The difficulty ladder — `TIER_0_TESTS` → `TIER_1_TESTS` → `TIER_2_TESTS` → ...** — `run_tests.py`'s `TIERS` list, walked by `check_plateau.py`. Each tier covers the same 8 [assessor capability labels](../README.md#ai-capability-labeling) except `SELF_IMPROVEMENT` (doesn't fit the pass/fail shape): `CODE_GENERATION`, `ERROR_HANDLING`, `STRUCTURED_OUTPUT`, `DECOMPOSITION`, `SECURITY_AWARENESS`, `INFRA_AS_CODE`, `TESTING`, `DOCUMENTATION`. `TIER_0_TESTS` is deliberately minimal so it's clearable from day one; `TIER_1_TESTS` is the original fixed 8; `TIER_2_TESTS` is harder still. Full case-by-case detail for `TIER_1_TESTS` (goal text, expected behavior, self-improve trigger) is in [`examples.md`](examples.md).
3. **Regression check.** Compares this run's average `code`-capability score (from episodic memory) against recent history. A real drop since the last skill revision is a materially different signal than "missed today's static threshold." Independent of the difficulty ladder above — this runs regardless of which tier is active.
4. **Candidate evaluation.** After the ordinary suite, `evaluate_candidate.py` selects at most one healthy, unevaluated executor candidate. It runs four deterministic held-out Tier 0 cases against both active and staged skills, one task at a time, and publishes a recommendation. It never applies a skill revision itself.

Everything runs fully sequentially (one submission at a time, waiting for it to settle) — running tests concurrently piles up enough simultaneous LLM calls on the single Ollama instance to blow past its read timeout. See [`run_tests.py`](run_tests.py) for the actual implementation, [`../docs/architecture.md`](../docs/architecture.md) for how this fits into the rest of the swarm.

The runner also checks the swarm's halt state before every single submission — if the swarm halts mid-run, remaining tests are marked `SKIPPED` (not `FAIL`) and reflect-triggering is skipped entirely, rather than cascading into a wall of 409s that reads as a false capability regression.

### Which failures may teach the system

- **Platform** — timeout, halt, web API interruption, or rollout gap. Recorded for operations; never evaluated as a skill candidate.
- **Contract** — malformed planner/verifier/assessor output. Stored as role-specific evidence; planner candidates remain review-only until planner isolation exists.
- **Deterministic validator** — syntax, YAML, or structural rejection. A healthy executor candidate from this class can enter automatic evaluation.
- **Judgment** — score-only, security, or documentation-quality conclusion. Retained for review, but excluded from automatic evaluation and promotion.

Candidate reports are retained in JetStream KV and `tests/results/`. A human promotion records the active skill revision and timestamp in the report.

### Per-test result statuses

`evaluate()` returns one of these `status` values per test; `main()`'s POST-RUN ANALYSIS groups on them to decide which reflect task (if any) to trigger:

- **`PASS`** — validator passed and score met the test's threshold.
- **`WARN`** — code/verify both completed, but either the validator rejected the artifact or the score fell short.
- **`TIMEOUT`** — no result at all within the test's timeout. Genuinely unexplained: could be an overloaded Ollama instance, a hung LLM call, or anything else that leaves zero trace in `get_state()`'s packets.
- **`PLANNER_FAILED`** — distinct from `TIMEOUT` on purpose. Found live 2026-08-20 (SECURITY_AWARENESS, task `563b0547`): a malformed/truncated LLM decomposition response makes planner.py's `_execute()` catch the `JSONDecodeError` and return with zero sub-tasks emitted — but `agent_shell.py` still marks that packet done and acks it (no exception was raised), so no code/verify packet is *ever* coming for that task_id. `wait_for_results()` now recognizes a done `plan` packet with zero spawned `code` packets and settles the task immediately instead of running out the full timeout — carrying the planner's own explanation forward as `evaluate()`'s `reason`/`issues`. In short: `TIMEOUT` means "we don't know why," `PLANNER_FAILED` means "the planner told us exactly why."
- **`SKIPPED`** — the swarm halted mid-run; not a capability failure, just an aborted attempt.

## Why a ladder, not a fixed 8

A single fixed set of 8 tests is a **good regression yardstick but a bad growth driver** — once the swarm reliably passes all 8, there's nothing left to *learn* from; it becomes a pure smoke/regression check. The ladder exists so the suite keeps teaching the swarm something after it clears the easy rung, without ever making the bar so hard on day one that nothing passes (which is what happened before this restructuring — see the [tiered test suite plan](../docs/superpowers/plans/2026-08-18-tiered-test-suite.md) for the original diagnosis).

**Promotion:** `determine_current_tier()` walks up from Tier 0. A tier promotes to the next once it clears **10 consecutive clean runs** (`STREAK_TARGET`, `check_plateau.py`) — all 8 capabilities `PASS` in the same run, back-to-back, no exceptions counted toward the streak. "Consecutive" is unforgiving: one flaky run resets the streak to zero, it doesn't just pause. Promotion never skips a tier and never walks past the top of `TIERS` on its own — reaching the top and plateauing there is a signal to *add* a new tier (append a `TIER_3_TESTS` list — no promotion-logic changes needed anywhere), not something the code invents on its own.

`run_tests.py`'s `main()` calls `select_active_tier()` (which calls `determine_current_tier()` against the git-tracked `tests/results/` history, fetched read-only before anything is submitted) to decide which tier's goals to actually run each hour.

Check current standing with:
```bash
python3 tests/check_plateau.py tests/results
```
prints every tier's streak and which one is currently active. This is also run automatically as part of the CronJob's git-push step (it has the full historical `tests/results/` clone available there, which no single run's pod has on its own), and additionally publishes the same summary to NATS KV (`cxp-state`/`tier-status`) so it's visible on the web dashboard's "Tier Progress" panel — the dashboard pod has no git credentials of its own to read the results history directly, so that publish step is the only path.

## Adding a harder tier later

When the top tier in `TIERS` plateaus (`check_plateau.py` prints a "FINISH LINE REACHED" notice when it does), options for where the next tier's goals come from, in increasing order of risk:

1. **Hand-written harder variants of the existing 8 categories** (what `TIER_2_TESTS` already is — e.g. `ERROR_HANDLING` adding a third exception type, `INFRA_AS_CODE` adding TLS and scheduled backups on top of Tier 1's Redis cluster). Lowest risk, no new infrastructure, just more upfront work. **Keep doing this for the next rung.**
2. **An established external benchmark** (HumanEval-style problems). Sidesteps self-invention entirely since the "right answer" already exists independently of this swarm.
3. **Generated from assessor's accumulated `gaps` data** (currently written to semantic memory on every completed artifact, and currently unused by anything). The most interesting option — targets the swarm's *actual* observed weaknesses — but reopens a real problem: a model can't reliably invent a hard test *and* grade its own attempt at it without an independent anchor. If this is ever built, it needs either a human-approval gate on generated test candidates, or a distinctly different/larger model doing the generating and grading, so the swarm isn't judging its own homework.

A new tier's validator should independently check something structural about the artifact — never let "the LLM call succeeded" alone count as a pass (see `tests/test_validators.py` for the existing pattern). `DECOMPOSITION` specifically: don't reach for a sub-task/packet count as the difficulty signal — confirmed via real packet history that this planner spawns exactly one `code`-type packet per task regardless of goal complexity, so a count-based check is unwinnable at any tier. Check the artifact's own content instead (`validate_decomposition`).

## Tier 2 — built, not yet promoted into

`TIER_2_TESTS` exists in `run_tests.py` today, ready to run once `TIER_1_TESTS` clears its 10-run streak. Every goal in it is grounded in something this swarm was actually observed attempting in real packet history (several already scoring 0.6-0.9), not invented difficulty:

| Tier 1 (today) | Tier 2 (next rung, built) |
|---|---|
| **CODE_GENERATION** — add two numbers, type hints + docstring | nth Fibonacci number iteratively, type hints + docstring, raises `ValueError` on negative input |
| **ERROR_HANDLING** — read a JSON file, handle `FileNotFoundError`/`JSONDecodeError` | Read a JSON config file, handle `FileNotFoundError`/`JSONDecodeError`/`PermissionError` (three exceptions, not two) |
| **STRUCTURED_OUTPUT** — one Kubernetes Deployment manifest with resource limits | Same, plus liveness/readiness probes and 2 replicas |
| **DECOMPOSITION** — scaffold a FastAPI+Postgres+Docker microservice | Same, plus a GitHub Actions CI workflow — same base scope, one more required deliverable |
| **SECURITY_AWARENESS** — web scraper downloading a user-supplied URL | A Flask endpoint accepting both a URL *and* a user-controlled filename — two distinct risk surfaces (SSRF + path traversal) instead of one |
| **INFRA_AS_CODE** — Helm values.yaml for a Redis cluster (persistence/auth/sentinel/resources) | Same, plus TLS between nodes and a scheduled backup CronJob |
| **TESTING** — factorial function + basic edge-case tests | Password-strength validator against multiple rules, plus ≥5 distinct pass/fail test cases |
| **DOCUMENTATION** — binary search with a comprehensive docstring | A full README.md (install/usage/API reference) — a genuinely different artifact type, not just a longer docstring |

`SECURITY_AWARENESS` and `DOCUMENTATION` stay partially LLM-judged on purpose — their validators (`validate_security`, `validate_readme`) catch the crudest structural failures, but real security/documentation-quality review still needs judgment a static check can't fully replace.
