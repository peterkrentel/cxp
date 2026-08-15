# CXP — Context Exchange Protocol

A distributed cognitive swarm: small LLMs passing structured packets through a routing layer, self-improving via versioned skill files.

---

## How it works

```
human submits task
       ↓
  planner agent        decomposes goal → spawns sub-packets
       ↓
  executor agent(s)    generates artifact (code, YAML, etc.) → auto-spawns verify
       ↓
  verifier agent       scores output → spawns reflect on failure
       ↓
  reflect agent        rewrites skill file → next run is smarter
```

All agents communicate over **NATS JetStream** using **CXP packets** — typed, traced, immutable events. Reputation scores route future work to the best-performing agent for each capability.

---

## Quick start

**Requirements:** Docker Desktop with kind + kubectl + helm installed.

```bash
# 1. spin up the cluster and deploy
helm dependency update helm/cxp
helm upgrade --install cxp helm/cxp --namespace cxp --create-namespace

# 2. watch the swarm (new terminal)
kubectl exec -it -n cxp deploy/cxp-dashboard -- python main.py dashboard

# 3. submit a task (new terminal)
kubectl exec -n cxp deploy/cxp-dashboard -- python main.py submit "scaffold a Redis StatefulSet for Kubernetes"
```

The dashboard shows agent states, packet flow, reputation scores, and a live log in real time.

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
