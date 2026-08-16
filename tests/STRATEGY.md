# Testing Strategy

Where the test suite is today, and what happens next — written down so it isn't a mystery next time someone (including future-you) looks at this folder.

## Where we are: three tiers, run hourly by the CronJob

1. **Tier 0 — smoke test.** One trivial task ("print hello world"), always first. A failure here means the pipeline itself is broken — a different, more urgent signal than "bad at capability X." The suite still continues after a smoke failure, for more signal.
2. **Tier 1 — capability coverage.** 8 tests, one per [assessor capability label](../README.md#ai-capability-labeling) except `SELF_IMPROVEMENT` (doesn't fit the pass/fail shape): `CODE_GENERATION`, `ERROR_HANDLING`, `STRUCTURED_OUTPUT`, `DECOMPOSITION`, `SECURITY_AWARENESS`, `INFRA_AS_CODE`, `TESTING`, `DOCUMENTATION`. Full case-by-case detail (goal text, expected behavior, self-improve trigger) is in [`examples.md`](examples.md) — that file also tracks which scenarios are automated vs. manual-only, since 3 of the 9 originally-documented scenarios only ever existed as manual demos until today.
3. **Tier 2 — regression check.** Compares this run's average `code`-capability score (from episodic memory) against recent history. A real drop since the last skill revision is a materially different signal than "missed today's static threshold."

Everything runs fully sequentially (one submission at a time, waiting for it to settle) — running tests concurrently piles up enough simultaneous LLM calls on the single Ollama instance to blow past its read timeout. See [`run_tests.py`](run_tests.py) for the actual implementation, [`../docs/architecture.md`](../docs/architecture.md) for how this fits into the rest of the swarm.

The runner also checks the swarm's halt state before every single submission — if the swarm halts mid-run, remaining tests are marked `SKIPPED` (not `FAIL`) and reflect-triggering is skipped entirely, rather than cascading into a wall of 409s that reads as a false capability regression.

## The known ceiling — and how we'll know we've hit it

The current 8 tests are a **fixed, repeated yardstick on purpose** — that repetition is what makes the regression check meaningful (you can't tell a skill revision made things worse if the test changes every run too). But it's a real ceiling: once the swarm reliably passes all 8, there's nothing left in this suite to *learn* from — it becomes a pure smoke/regression check, not a driver of further improvement.

**Finish line:** 8/8 pass for **10 consecutive hourly runs**. Enough to rule out a lucky streak given the real score variance we've already observed (the same test scoring 0.5 one run and 0.9 the next, purely from small-model non-determinism), without dragging things out once the signal is actually clear.

Check it with:
```bash
python3 tests/check_plateau.py tests/results
```
This is also run automatically as part of the CronJob's git-push step (it has the full historical `tests/results/` clone available there, which no single run's pod has on its own).

## What happens after the finish line — tier 3 (not built yet)

Three options for where harder tests come from, in increasing order of risk:

1. **Hand-written harder variants of the existing 8 categories** (e.g., "add two numbers" → "add two numbers with overflow and type-coercion edge cases"). Lowest risk, no new infrastructure, just more upfront work. **Start here.**
2. **An established external benchmark** (HumanEval-style problems). Sidesteps self-invention entirely since the "right answer" already exists independently of this swarm.
3. **Generated from assessor's accumulated `gaps` data** (currently written to semantic memory on every completed artifact, and currently unused by anything). The most interesting option — targets the swarm's *actual* observed weaknesses — but reopens a real problem: a model can't reliably invent a hard test *and* grade its own attempt at it without an independent anchor. If this is ever built, it needs either a human-approval gate on generated test candidates, or a distinctly different/larger model doing the generating and grading, so the swarm isn't judging its own homework.

Whichever option comes next, keep it in a separate bucket from the tier-1/tier-2 tracking above — a new, harder test failing shouldn't get conflated with the stable 8 regressing.

## Tier 3 — drafted, not wired in

Harder variants of each Tier-1 category, ready to move into `run_tests.py`'s `TESTS` list (and a new `TESTS_TIER3` list, kept separate per above) once `check_plateau.py` reports the finish line. Deliberately not activated yet — Tier 1 and Tier 3 need to stay distinct so a hard new test failing isn't confused with the stable baseline regressing.

| Tier 1 (today) | Tier 3 (next, drafted) |
|---|---|
| **CODE_GENERATION** — add two numbers, type hints + docstring | Add two numbers, handling both `int` and `float`, raising `TypeError` on non-numeric input, and correctly handling very large integers without overflow |
| **ERROR_HANDLING** — read a JSON file, handle `FileNotFoundError`/`JSONDecodeError` | Fetch data from a URL, parse as JSON, and handle connection errors, timeouts, HTTP error codes, and JSON decode errors — each with a distinct custom exception type and logging |
| **STRUCTURED_OUTPUT** — one Kubernetes Deployment manifest | A full manifest set (Deployment, Service, ConfigMap, HorizontalPodAutoscaler) with correct cross-references — Service selector matching Deployment labels, HPA targeting the Deployment |
| **DECOMPOSITION** — scaffold a FastAPI+Postgres+Docker microservice (≥3 sub-tasks) | Scaffold a multi-service system: FastAPI backend, React frontend, Postgres with migrations, Redis cache, Docker Compose, CI pipeline config, tests per service, docs (raise `min_subtasks` to 5-6) |
| **SECURITY_AWARENESS** — web scraper downloading user-supplied URLs | A file-upload endpoint saving to disk using a user-provided filename — more surface area to flag correctly (path traversal, arbitrary file write, no size limit, no content-type check), not just one obvious risk |
| **INFRA_AS_CODE** — Helm values.yaml for a Redis cluster | A complete Helm chart (`Chart.yaml`, `values.yaml`, templates for Deployment/Service/Ingress) for Postgres with replication, automated backups, and a NetworkPolicy restricting access to the app namespace only |
| **TESTING** — factorial function + basic unit tests | A thread-safe LRU cache class, plus a pytest suite covering eviction order, concurrent-access thread safety, and edge cases (`max_size=0`, `max_size=1`) |
| **DOCUMENTATION** — binary search with a docstring | A rate limiter class with multiple strategies (fixed window, sliding window, token bucket) — full docstrings per method (params, returns, raised exceptions) plus a runnable usage example for each strategy |

No validators written yet on purpose — writing them now, before there's any real signal these are the right next tests, would be guessing. Write them when Tier 3 actually activates, informed by whatever Tier 1 turned out to actually teach us about this model's failure modes.
