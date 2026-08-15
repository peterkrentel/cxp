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
| planner | `plan` | qwen2.5:0.5b | Decomposes goals into sub-tasks |
| executor ×2 | `code` | qwen2.5:1.5b | Generates artifacts |
| verifier | `verify` | qwen2.5:0.5b | Scores output quality |
| assessor | `assess` | qwen2.5:0.5b | Labels artifacts with capability tags |
| reflect | `reflect` | qwen2.5:1.5b | Rewrites skill files on failure |
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

## Self-improvement loop

When the verifier scores an artifact below threshold:
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
