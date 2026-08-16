# CXP — Context Exchange Protocol

A self-improving distributed AI swarm: small local LLMs passing structured packets through a routing layer, testing themselves hourly, and rewriting their own skill files when they fail.

---

## How it works

```
human (or CronJob) submits task
       ↓
  planner agent        decomposes goal → spawns sub-packets (cap: plan)
       ↓
  executor agent(s)    generates artifact — code, YAML, config (cap: code)
       ↓
  verifier agent       scores output 0.0–1.0, spawns reflect if score < threshold (cap: verify)
       ↓
  assessor agent       labels artifact with capability tags (cap: assess)
       ↓
  reflect agent        rewrites skill file based on failure (cap: reflect)
       ↓
  next run is smarter
```

All agents communicate over **NATS JetStream** using **CXP packets** — typed, traced, immutable events routed by capability subject (`cxp.cap.plan`, `cxp.cap.code`, etc). Reputation scores route future work to the best-performing agent for each capability.

**No OpenAI. No Anthropic. All local via Ollama.**

---

## Hardware requirements

- **Minimum:** 8GB RAM, 4-core CPU, 10GB disk (runs `qwen3:8b` comfortably)
- **Better:** 16GB+ RAM, 8+ cores, 20GB+ disk (supports larger models + more replicas)
- **Bigger models:** With 32GB+ GPU VRAM, can run `qwen2:70b` or `llama2:70b` for higher quality

Models adapt to your hardware: edit `helm/cxp/values.yaml` to use different models per agent. Current setup uses `qwen3:8b` (8B parameters).

---

## Quick start

**Requirements:** Docker Desktop, kind, kubectl, helm

```bash
git clone git@github.com:peterkrentel/cxp.git
cd cxp
make reset    # creates kind cluster with correct port mappings + deploys everything
```

Then open **http://localhost** (Traefik ingress, no port-forward needed).

---

## Submitting tasks

Via web UI at **http://localhost** — type goal, press Enter.

Via CLI:
```bash
make submit GOAL="generate a Kubernetes Deployment for a Node.js API with health checks"
```

---

## Self-improvement loop

When verifier scores output below threshold:
1. `reflect` packet spawned with failure details
2. Reflect agent reads current `executor_v1.md` skill file
3. Rewrites it with guidance to prevent the same failure
4. Old skill saved as `executor_v1.md.bak`
5. Next executor run loads improved skill

Check skill evolution:
```bash
kubectl exec -n cxp deploy/cxp-dashboard -- cat /skills/executor_v1.md
```

---

## Autonomous testing

A **CronJob** runs the test suite hourly inside the cluster:
- Submits 3 test tasks (CODE_GENERATION, ERROR_HANDLING, STRUCTURED_OUTPUT)
- Evaluates results against pass thresholds
- Triggers reflect loop automatically on failures
- Re-runs failed tests after skill update
- Writes JSON results to `tests/results/`

Trigger immediately:
```bash
make test-now
```

Run locally:
```bash
make test
```

---

## Agents

| Agent | Capability | Model | Role |
|-------|-----------|-------|------|
| planner | `plan` | qwen3:8b | Decomposes goals into sub-tasks |
| executor ×2 | `code` | qwen3:8b | Generates artifacts |
| verifier | `verify` | qwen3:8b | Scores output quality |
| assessor | `assess` | qwen3:8b | Labels artifacts with capability tags |
| reflect | `reflect` | qwen3:8b | Rewrites skill files on failure |
| dashboard | — | — | Terminal UI (`make dashboard`) |
| web | — | — | Browser UI at http://localhost |

---

## Project layout

```
src/
  agent_shell.py      base agent: NATS listener, LLM caller, auto-pull model
  packet.py           CXP packet schema (Pydantic)
  memory.py           reputation + episodic/semantic memory (JSON on PVC)
  dashboard.py        Rich terminal UI
  web_dashboard.py    FastAPI web UI + task submission + LLM thinking stream
  agents/
    planner.py
    executor.py
    verifier.py       auto-spawns reflect + assess on completion
    reflect.py        rewrites skill files
    assessor.py       AI capability labeling

skills/               versioned prompt specs loaded at runtime
  planner_v1.md
  executor_v1.md      ← rewritten by reflect agent
  verifier_v1.md

tests/
  run_tests.py        self-improving test runner (submit → evaluate → reflect → retry)
  examples.md         labeled test cases with expected behavior
  results/            JSON result files per run (tracked in git)

helm/cxp/
  Chart.yaml          nats + ollama + traefik sub-charts
  values.yaml         per-agent model config, replicas, storage
  templates/
    agents.yaml       Deployments — python:3.12-slim + init container code assembly
    app-code.yaml     ConfigMaps embedding all source code
    config.yaml       PVC (helm.sh/resource-policy: keep) + skills ConfigMap
    web-service.yaml  ClusterIP service + Traefik IngressRoute
    test-runner.yaml  CronJob for hourly autonomous testing

Makefile              deploy / reset / test / submit / dashboard / logs
kind-config.yaml      kind cluster with ports 80, 443, 4222 mapped
```

---

## System Status (2026-08-15)

### ✅ Working
- **Agent-to-agent pipeline**: Plan → Code → Verify → Deploy → Assess (full end-to-end)
- **Memory safeguard**: Deployer backs up `/data/memory.json` before executing artifacts, restores on failure
- **Markdown parsing**: Deployer strips ```language ... ``` blocks to extract YAML/Python code
- **Parallel execution**: Multiple tasks flow through agents simultaneously without blocking
- **Quality scoring**: Verifier assigns 0.0-1.0 scores, filters deployment (threshold: 0.85)
- **Git automation**: Helm deploys increment git revisions, committed + pushed automatically
- **Web dashboard**: Real-time task tracking, agent status, LLM thinking stream at http://localhost:8080

### ⚠️ Known Issues
1. **Test runner failing** — CronJob pods stuck in `Init:Error`
   - Issue: ConfigMap embedding code may be too large or malformed
   - Impact: Autonomous self-testing disabled; manual `make test-now` works
   - Fix: Need to reduce ConfigMap size or split across multiple maps

2. **Task submission stalls** — New tasks submitted via API not flowing to planner
   - Old tasks (from earlier session) work fine; recent submissions hang
   - Likely: Dashboard not receiving plan packets from NATS
   - Status: Investigating NATS subscription timing

3. **Executor artifact type detection** — Fixed but needs testing
   - Was: Python code showing as "unrecognized artifact type"
   - Fix: Added markdown block stripping (```yaml ... ```)
   - Status: Deployed, awaiting test verification

### 📊 Session Statistics
- **Total packets processed**: 19
- **Tasks completed**: 18
- **Failed tasks**: 1 (cake recipe verification error)
- **LLM calls**: 27
- **Skill updates**: 4 (reflect rewrites triggered)
- **Agents deployed**: 8 (planner, executor×2, verifier, assessor, deployer, reflect, web/dashboard)

### 🔍 Reputation Table
```
executor-1:
  code: 95% (18/19 successes) — Strong
  k8s-manifest: 0% (0/1) — Never attempted (executor constrained to "code")

verifier-1:
  verify: 75% (3/4 successes) — Good discrimination

planner-1:
  plan: 100% (1/0) — Limited sample but reliable
```

### 🚀 Recent Improvements
1. **Memory backup/restore** (commit 2b452dd)
   - Deployer creates backup before executing quality_score >= 0.85
   - Auto-restores on deployment failure or exception
   - Prevents generated code from corrupting reputation system

2. **Markdown code block stripping** (commit 399fad7)
   - Executor wraps code in ```yaml ... ``` 
   - Deployer now extracts raw code before detection
   - Fixes YAML Deployment execution in sandbox

3. **Executor capability constraint** (earlier)
   - Changed from ["code", "k8s-manifest", "python-code", "any"] → ["code"]
   - Prevents invalid capabilities polluting reputation table

---

## Debugging Commands

### Watch system in real-time
```bash
kubectl get pods -n cxp -w
```

### Check NATS connectivity
```bash
kubectl exec -n cxp cxp-nats-box-* -- nats stat
```

### Tail agent logs
```bash
kubectl logs -n cxp deploy/cxp-planner -f
kubectl logs -n cxp deploy/cxp-executor -f
kubectl logs -n cxp deploy/cxp-verifier -f
```

### View dashboard API state
```bash
kubectl port-forward -n cxp svc/cxp-web 8080:8080 &
curl http://localhost:8080/api/state | jq .
```

### Check memory (reputation + skills)
```bash
kubectl exec -n cxp deploy/cxp-planner -- cat /data/memory.json | jq '.reputation'
```

### Submit task manually
```bash
make submit GOAL="your task here"
```

### Restart stuck agents
```bash
kubectl rollout restart -n cxp deploy/cxp-planner deploy/cxp-executor deploy/cxp-verifier
```

### Run test suite
```bash
make test-now          # trigger CronJob immediately (currently failing)
make test              # run tests locally
```

---

## Known Limitations

### LLM Model Constraints
- **qwen2.5:0.5b** (planner, verifier): Very small, ~500M parameters
  - Limited to simple coding patterns
  - No real-time knowledge (training cutoff ~2024)
  - May struggle with complex logic
  
- **qwen2.5:1.5b** (executor, reflect): Better but still small
  - Good at: Python code, YAML generation, simple explanations
  - Bad at: Complex algorithms, math proofs, multi-file projects

### System Scope
- **Can execute in sandbox only** — Kubernetes deployments go to `cxp-sandbox` namespace
- **No external APIs** — Can't call real-time data (price, weather, news)
- **No internet access** — Sandbox restricts network
- **15s subprocess timeout** — Long-running code will be killed

### Data Isolation
- **Shared memory vulnerability** — Deployed code can read/corrupt `/data/memory.json`
  - Mitigated: Backup/restore safeguard now in place
  - Future: Make memory mount read-only to deployer

---

## Roadmap

### Immediate (Fix blockers)
- [ ] Debug test runner ConfigMap issue — why is Init failing?
- [ ] Verify task submission flow — new tasks should hit planner
- [ ] Test markdown stripping with real YAML deployments

### Short-term (Polish)
- [ ] Increase executor replicas to 4 (parallel task processing)
- [ ] Add timeout to planner LLM calls (prevent hangs)
- [ ] Implement memory snapshot before each deploy execution

### Medium-term (Expand capability)
- [ ] MCP server integration for safe external execution
- [ ] Multi-model support (switch qwen2.5 ↔ mistral ↔ llama2)
- [ ] Skill version management (git-backed rollback)
- [ ] Persistent dashboard state (task history)

### Long-term (Advanced)
- [ ] Self-modifying agent topology (add/remove agents based on task load)
- [ ] Cross-agent reputation exchange (planner can learn from executor's failures)
- [ ] Structured knowledge graph (semantic memory → vector DB)
- [ ] Browser-based skill editor with YAML validation

---

## Configuration (helm/cxp/values.yaml)

| Key | Default | What it does |
|-----|---------|-------------|
| `agents.*.model` | varies | Per-agent LLM model |
| `agents.executor.replicas` | 2 | Parallel executors |
| `memoryPVC.size` | 1Gi | Shared memory + results |
| `ollamaModel` | qwen2.5:1.5b | Default fallback model |

---

## Deploying to a server

Works on any k8s cluster. For cheap cloud options:
- **Oracle Cloud Free** — 4 ARM cores, 24GB RAM, permanently free
- **Hetzner CX21 + k3s** — 2 vCPU, 4GB, €6/month

```bash
# Point kubeconfig at remote cluster, then:
make deploy
```

---

## Make commands

```
make deploy      sync src → Helm → install/upgrade
make reset       recreate kind cluster + deploy
make test        run test suite locally
make test-now    trigger in-cluster test CronJob immediately
make submit GOAL="..."   submit a task
make dashboard   open terminal dashboard
make logs        tail all agent logs
make destroy     remove release (keeps PVC)
```

---

## Project layout

```
main.py               entry point — submit tasks, run agents, run dashboard
Dockerfile            single image; role set via CMD arg (planner/executor/etc.)
deploy.sh             one-command cluster bootstrap via kind + helm
kind-config.yaml      kind cluster with NATS port forwarded to localhost:4222

src/
  packet.py           CXP packet schema (Pydantic)
  agent_shell.py      base agent: NATS listener, LLM caller, reputation recording
  memory.py           reputation + episodic/semantic memory (JSON-backed)
  dashboard.py        Rich live terminal UI
  agents/
    planner.py        decomposes tasks, spawns sub-packets
    executor.py       generates artifacts, auto-spawns verify
    verifier.py       grades output (0.0–1.0), spawns reflect on failure
    reflect.py        rewrites skill files, versions old ones as .bak

skills/               versioned prompt specs loaded by each agent at runtime
  planner_v1.md
  executor_v1.md      ← rewritten by reflect agent when verifier score is low
  verifier_v1.md

helm/cxp/
  Chart.yaml          depends on nats + ollama Helm sub-charts
  values.yaml         replicas, image tag, model name, storage config
  templates/
    agents.yaml       Deployment per agent role
    config.yaml       PVC for shared memory, ConfigMap for skill files
```

---

## Configuration

All config lives in `helm/cxp/values.yaml`. Key knobs:

| Value | Default | What it does |
|---|---|---|
| `ollamaModel` | `qwen2.5:1.5b` | LLM used by all agents |
| `agents.executor.replicas` | `2` | Parallel executors = parallel task processing |
| `memoryPVC.size` | `1Gi` | Shared memory store for reputation + episodic memory |

---

## AI Software Factory

CXP is an **autonomous software factory** — small local LLMs doing specialized work in a loop, getting measurably better over time.

| Factory Role | CXP Agent |
|---|---|
| Requirements | Task goals you submit |
| Developer | Executor (generates code/YAML/config) |
| Code review | Verifier (scores quality 0–1) |
| QA | CronJob test runner (hourly, autonomous) |
| Postmortem | Reflect (reads failures) |
| Process improvement | Skill file rewrite |
| CI/CD | Helm + kubectl |
| Observability | Web dashboard + LLM thinking stream |

**What's missing to make it a full factory:** a `deployer` agent that runs `kubectl apply` or `python script.py` on verified artifacts. Currently generates — doesn't execute. Add with a dry-run safety gate.

---

## Self-Improvement Loop (detailed)

When verifier scores output below threshold (default 0.75):

1. Verifier spawns a `reflect` packet with failure details + original artifact
2. Reflect agent reads current `executor_v1.md` skill file from `/skills/`
3. Reflect calls LLM: *"Here's what failed. Rewrite this skill file to prevent it."*
4. New `executor_v1.md` written to shared emptyDir volume
5. Old version saved as `executor_v1.md.bak` (auditable history)
6. Next executor run loads updated skill as its system prompt → better output

This is **prompt-level evolution** — behavioral improvement without retraining weights.

Check current skill and history:
```bash
kubectl exec -n cxp deploy/cxp-reflect -- cat /skills/executor_v1.md
kubectl exec -n cxp deploy/cxp-reflect -- cat /skills/executor_v1.md.bak
```

---

## AI Capability Labeling

Every completed artifact is labeled by the **assessor agent** which reads it and classifies what AI capabilities it demonstrates.

**Available labels:**
`CODE_GENERATION`, `ERROR_HANDLING`, `STRUCTURED_OUTPUT`, `SECURITY_AWARENESS`, `DECOMPOSITION`, `INFRA_AS_CODE`, `TESTING`, `DOCUMENTATION`, `SELF_IMPROVEMENT`

**Output format:**
```json
{
  "labels": ["CODE_GENERATION", "ERROR_HANDLING"],
  "verdict": "Function correctly handles FileNotFoundError with specific exception type",
  "strengths": ["named exception types", "returns empty dict on error"],
  "gaps": ["no logging", "no type hints"]
}
```

Labels flow into semantic memory, building a searchable knowledge base of what the swarm has produced and what gaps remain.

---


1. A `reflect` packet is spawned with the failure details
2. The reflect agent reads the current `executor_v1.md` skill file
3. It rewrites the skill with guidance to prevent the same failure
4. The old skill is saved as `executor_v1.md.bak` (Git-style versioning)
5. The next executor run loads the improved skill automatically

---

## Extending

**Add a new capability** — subclass `AgentShell`, set `capabilities`, implement `_execute`. Deploy as a new Deployment in `helm/cxp/values.yaml` under `agents`.

**Swap the LLM** — change `OLLAMA_MODEL` env var. Any Ollama-compatible model works.

**Scale executors** — increase `agents.executor.replicas` in values.yaml and `helm upgrade`.
