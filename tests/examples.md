# CXP Test Cases

Submit via web UI at http://localhost or:
```bash
kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "GOAL"
```

Each test has a label — what AI capability it evaluates. Score > 0.8 = pass.

**Automation status matters here** — it's easy to assume everything below runs automatically; it doesn't. `tests/run_tests.py`'s hourly CronJob currently automates 8 of these 9 (marked **Automated** below, at whichever difficulty tier is currently active — see [`STRATEGY.md`](STRATEGY.md); goal text below is the `TIER_1_TESTS` version specifically), plus a SMOKE test ("print hello world", not listed here since it's not capability-specific and always runs regardless of tier) and a regression check comparing each run's scores against recent history. The rest are marked **Manual only** — real scenarios worth trying by hand, but nothing currently submits them on a schedule.

---

## CODE_GENERATION — basic output quality

**Automated** (hourly CronJob)

```
write a Python function that adds two numbers with type hints and docstring
```
**Expect:** Clean Python, type hints, docstring, no syntax errors
**Self-improve trigger:** Missing type hints → verifier scores < 0.8 → reflect updates executor skill

---

## ERROR_HANDLING — robustness patterns

**Automated** (hourly CronJob)

```
write a Python function that reads a JSON file and returns a dict, handling file not found and JSON decode errors
```
**Expect:** try/except blocks for FileNotFoundError and json.JSONDecodeError
**Self-improve trigger:** Missing exception types → reflect adds "always name specific exceptions" to skill

---

## STRUCTURED_OUTPUT — multi-section generation

**Automated** (hourly CronJob)

```
generate a Kubernetes Deployment manifest for a Node.js API with health checks, resource limits, and non-root security context
```
**Expect:** Valid YAML, readinessProbe, livenessProbe, resources.limits, securityContext.runAsNonRoot
**Self-improve trigger:** Missing security context → verifier flags → executor skill learns security defaults

---

## DECOMPOSITION — planner quality

**Automated** (hourly CronJob) — same goal text as below, but checks the returned artifact's *content* for evidence of each named component (`validate_decomposition`), not sub-task count. Confirmed via real packet history that this planner always spawns exactly one `code`-type packet per task regardless of goal complexity, so a sub-task-count check (what this used to be) can never distinguish an easy goal from a hard one — fixed 2026-08-19.

```
scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, and README
```
**Expect:** the single returned artifact shows evidence of each named component (fastapi, postgres, docker-compose, test, readme) — not necessarily as five separate sub-tasks
**Self-improve trigger:** missing a component → verifier scores < 0.7 → reflect fires, but note it only ever rewrites the `executor` skill (hardcoded `SKILL_TARGET` in `reflect.py`) — planner's own skill file is never actually updated by anything today, despite the failure originating in planner's decomposition

---

## SELF_IMPROVEMENT — reflect loop visible

**Manual only** — doesn't fit the automated suite's pass/fail shape

Run this 3 times in a row and watch the executor skill file evolve:
```
generate a Python class for a rate limiter with per-user limits, thread safety, and logging
```
**Expect:** Run 1 might miss thread safety. Verifier scores 0.6. Reflect rewrites the `executor` skill.
Run 2 includes thread safety. Score 0.85.

Check skill evolution — the live skill lives in NATS JetStream KV, not a file (the ConfigMap-seeded `/skills/executor_v1.md` is only the fallback used before the very first reflect write):
```bash
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv get cxp-skills executor
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv history cxp-skills executor
```

---

## INFRA_AS_CODE — real-world DevOps

**Automated** (hourly CronJob)

```
generate a Helm values.yaml for a production Redis cluster with persistence, auth, sentinel, and resource limits
```
**Expect:** Valid YAML, persistence.enabled, auth.password (placeholder), sentinel config, resources block

---

## SECURITY_AWARENESS — verifier catches risks

**Automated** (hourly CronJob) — pass means the verifier *flagged* a risk (checked via keyword match on its issues text), not that the artifact scored well; "no risk flagged" is the failure mode

```
generate a Python web scraper that downloads URLs from user input and saves to disk
```
**Expect:** Verifier should flag: no URL validation, path traversal risk, no rate limiting
**Self-improve trigger:** Executor skill learns to always validate external input

---

## TESTING — does the artifact include actual tests

**Automated** (hourly CronJob) — not sourced from this doc originally; added directly to `run_tests.py` to complete capability-label coverage

```
write a Python function that calculates the factorial of a number, plus unit tests covering zero, one, and a typical positive input
```
**Expect:** A dedicated `test_` function or at least 2 `assert` statements — not just the function under test with no tests at all
**Self-improve trigger:** No test code found → verifier likely scores low → reflect fires

---

## DOCUMENTATION — real docstrings, not just code

**Automated** (hourly CronJob) — not sourced from this doc originally; added directly to `run_tests.py` to complete capability-label coverage

```
write a Python function for binary search over a sorted list, with a comprehensive docstring covering parameters, return value, and an example usage
```
**Expect:** A real docstring (not just a `#` comment) mentioning the return value and an example — the validator does a literal keyword check for "return"/"example" in the text, so it can false-negative on a docstring that covers the same ground with different wording
**Self-improve trigger:** No docstring, or missing those sections → reflect fires

---

## MULTI_AGENT_PARALLELISM — executor scaling

**Manual only** — an operational demo (scaling replicas, watching the dashboard), not something with a pass/fail shape

Scale executors to 4 first:
```bash
kubectl scale deployment/cxp-executor -n cxp --replicas=4
```
Then submit 5 tasks quickly:
```bash
for i in {1..5}; do
  kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "generate a Dockerfile for a Python $i.x application"
  sleep 1
done
```
**Expect:** All 5 tasks processed in parallel. Dashboard shows 4 executors all Working simultaneously.

---

## REPUTATION_ROUTING — best agent wins

**Manual only** — an observability check against `memory.json`, not a gradeable test

Submit 10 tasks of the same type. After ~5, reputation scores settle.
Check which executor has highest score:
```bash
kubectl exec -n cxp deploy/cxp-dashboard -- cat /data/memory.json | python3 -c "
import sys, json
m = json.load(sys.stdin)
for agent, caps in m['reputation'].items():
    for cap, scores in caps.items():
        s = scores['successes']
        f = scores['failures']
        print(f'{agent} {cap}: {s/(s+f)*100:.0f}% ({s}/{s+f})')
"
```

---

## What makes a good test result

| Score | Meaning |
|-------|---------|
| 0.9+ | Perfect — complete, safe, production-ready |
| 0.7-0.9 | Good — minor gaps, reflect will improve |
| 0.5-0.7 | Weak — major issues, reflect will rewrite skill |
| < 0.5 | Fail — fundamental problem, check agent logs |
