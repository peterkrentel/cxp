# CXP Test Cases

Submit via web UI at http://localhost or:
```bash
kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "GOAL"
```

Each test has a label — what AI capability it evaluates. Score > 0.8 = pass.

---

## CODE_GENERATION — basic output quality

```
write a Python function that adds two numbers with type hints and docstring
```
**Expect:** Clean Python, type hints, docstring, no syntax errors
**Self-improve trigger:** Missing type hints → verifier scores < 0.8 → reflect updates executor skill

---

## ERROR_HANDLING — robustness patterns

```
write a Python function that reads a JSON file and returns a dict, handling file not found and JSON decode errors
```
**Expect:** try/except blocks for FileNotFoundError and json.JSONDecodeError
**Self-improve trigger:** Missing exception types → reflect adds "always name specific exceptions" to skill

---

## STRUCTURED_OUTPUT — multi-section generation

```
generate a Kubernetes Deployment manifest for a Node.js API with health checks, resource limits, and non-root security context
```
**Expect:** Valid YAML, readinessProbe, livenessProbe, resources.limits, securityContext.runAsNonRoot
**Self-improve trigger:** Missing security context → verifier flags → executor skill learns security defaults

---

## DECOMPOSITION — planner quality

```
scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, and README
```
**Expect:** Planner breaks into 5+ sub-tasks (API code, DB model, Docker, tests, docs)
**Self-improve trigger:** Missing test sub-task → verifier scores < 0.7 → planner skill updated

---

## SELF_IMPROVEMENT — reflect loop visible

Run this 3 times in a row and watch the executor skill file evolve:
```
generate a Python class for a rate limiter with per-user limits, thread safety, and logging
```
**Expect:** Run 1 might miss thread safety. Verifier scores 0.6. Reflect rewrites executor_v1.md.
Run 2 includes thread safety. Score 0.85.

Check skill evolution:
```bash
kubectl exec -n cxp deploy/cxp-dashboard -- cat /skills/executor_v1.md
```

---

## INFRA_AS_CODE — real-world DevOps

```
generate a Helm values.yaml for a production Redis cluster with persistence, auth, sentinel, and resource limits
```
**Expect:** Valid YAML, persistence.enabled, auth.password (placeholder), sentinel config, resources block

---

## SECURITY_AWARENESS — verifier catches risks

```
generate a Python web scraper that downloads URLs from user input and saves to disk
```
**Expect:** Verifier should flag: no URL validation, path traversal risk, no rate limiting
**Self-improve trigger:** Executor skill learns to always validate external input

---

## MULTI_AGENT_PARALLELISM — executor scaling

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
