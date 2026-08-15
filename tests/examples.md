# CXP Test Examples — Start Here

Run these in order. Each one teaches something different.

---

## Level 1: Simple Generation

**Goal:** Verify basic planner → executor → verifier loop works.

```bash
kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit \
  "generate a Python function that adds two numbers"
```

**What to expect:**
- Planner decomposes into [generate, verify]
- Executor produces a simple function
- Verifier scores it (should be high, task is trivial)
- Result appears in dashboard in ~10 seconds

**Success metric:** Score > 0.8, function is syntactically valid

---

## Level 2: Structure & Standards

**Goal:** Test if verifier enforces quality rules.

```bash
kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit \
  "generate a Python module for parsing CSV files with error handling"
```

**What to expect:**
- Executor should include try/catch, logging, type hints
- Verifier checks for these; if missing, scores < 0.8
- Reflect agent rewrites executor_v1.md skill to emphasize error handling
- Next similar task includes error handling automatically

**Success metric:** First run might score 0.6, next run scores 0.85+

---

## Level 3: Multi-Step Decomposition

**Goal:** Verify planner breaks complex tasks into sub-steps.

```bash
kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit \
  "create a Kubernetes manifest for a PostgreSQL StatefulSet with persistence, backup sidecar, and resource limits"
```

**What to expect:**
- Planner spawns 4-5 packets: [main StatefulSet, backup spec, security policy, resource limits, verify]
- Multiple executors claim different tasks in parallel
- Verifier checks for security (non-root user), persistence (PVC), resources (CPU/memory limits)

**Success metric:** All sub-artifacts generated, final score > 0.75

---

## Level 4: Failure → Self-Improvement

**Goal:** Watch reflect agent improve the system.

```bash
# Run 3 times with similar task
for i in {1..3}; do
  echo "=== Run $i ==="
  kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit \
    "generate a REST API endpoint in Go with validation and error responses"
  sleep 3
done
```

**Watch happen:**
- Run 1: Maybe missing input validation
- Run 2: Reflect agent tweaks executor skill to include validation
- Run 3: Validation included automatically

**Success metric:** Reputation score for executor increases across runs

---

## Level 5: Full Loop

**Goal:** End-to-end self-improving workflow.

```bash
# Submit batches over 10 minutes
for batch in 1 2 3; do
  for i in {1..3}; do
    kubectl exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit \
      "generate a Helm values.yaml for a production microservice with monitoring, scaling, and security"
    sleep 2
  done
  echo "Batch $batch complete, waiting 2 min..."
  sleep 120
done
```

**Observe:**
- Reputation scores climb
- Skill files evolve (`cat /skills/executor_v1.md` changes)
- Quality improves per batch

---

## Validation Rules to Check

After each task, verify:

```bash
# Get the task output
TASK_ID="<from submit output>"
kubectl exec -n cxp deploy/cxp-dashboard -- cat /data/memory.json | grep $TASK_ID

# Check score
jq '.packets[] | select(.id == "'$TASK_ID'") | .payload.score'

# Check skill evolution
kubectl exec -n cxp deploy/cxp-dashboard -- diff /skills/executor_v1.md /skills/executor_v1.md.bak
```

---

## Common Failures & Fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Timeout (task hangs) | Ollama model still loading | Wait 5 min, check `kubectl logs -n cxp deploy/cxp-ollama` |
| No output | Agent crashed | Check `kubectl logs -n cxp deploy/cxp-executor` |
| Same mistakes repeat | Reflect not triggering | Check verifier score threshold in `helm/cxp/values.yaml` |
| Scores always 1.0 | Verifier too lenient | Edit `skills/verifier_v1.md` to be stricter |

---

## What to Try Next

Once basics work:
1. **Vary task complexity** — simple → medium → complex
2. **Measure improvement** — track scores over time
3. **Tweak skills** — edit `executor_v1.md` manually, redeploy, see impact
4. **Scale executors** — change `helm/cxp/values.yaml` agents.executor.replicas to 5, rerun same task, watch parallelism
5. **Change model** — swap `ollamaModel: mistral:latest`, redeploy, compare outputs
