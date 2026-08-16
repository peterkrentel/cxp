# CXP — Context Exchange Protocol

A self-improving distributed AI swarm: small local LLMs (via Ollama) passing structured packets through a routing layer, testing themselves hourly, and rewriting their own skill files when they fail. Prototype — built to explore the pattern, not production-hardened.

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
  deployer agent       kubectl-applies YAML / runs code, if score ≥ 0.85 (cap: deploy)
       ↓
  reflect agent        rewrites skill file based on failure (cap: reflect)
       ↓
  next run is smarter
```

All agents communicate over **NATS JetStream** using **CXP packets** — typed, traced, immutable events routed by capability subject (`cxp.cap.plan`, `cxp.cap.code`, etc). Reputation scores route future work to the best-performing agent for each capability.

**No OpenAI. No Anthropic. All local via Ollama** for every agent's own LLM calls (this codebase was built with Claude's help — the swarm's runtime model is a small local Ollama model, not Claude).

If any agent hits an unhandled error, the swarm **halts swarm-wide** — new task submissions are rejected with a clear reason until a human clicks "Resume" in the web UI. It no longer just logs the failure and keeps going.

---

## Hardware requirements

- **Minimum:** 8GB RAM, 4-core CPU, 10GB disk
- Current setup uses `qwen2.5:1.5b` (assessor uses the smaller `qwen2.5:0.5b`) — small models chosen to run comfortably on modest hardware, at the cost of occasionally malformed JSON output (planner is the most sensitive to this).
- Bigger/better models: edit `helm/cxp/values.yaml` per-agent `model:` field; any Ollama-compatible model works.

---

## Quick start

**Requirements:** Docker Desktop, kind, kubectl, helm

```bash
git clone git@github.com:peterkrentel/cxp.git
cd cxp
make reset    # creates kind cluster with correct port mappings + deploys everything
```

Then open **http://localhost** (Traefik ingress, no port-forward needed).

This project runs **only in the local kind cluster** — there is no supported "run it locally outside Kubernetes" mode.

---

## Submitting tasks

Via web UI at **http://localhost** — type goal, press Enter.

Via CLI:
```bash
make submit GOAL="generate a Kubernetes Deployment for a Node.js API with health checks"
```

If the swarm is currently halted (see below), submission is rejected with a 409 and the halt reason — clear it from the UI first.

---

## Self-improvement loop

When verifier scores output below threshold:
1. `reflect` packet spawned with failure details
2. Reflect agent reads the current `executor` skill text from the shared **NATS JetStream KV** store (`cxp-skills` bucket) — falling back to the ConfigMap-seeded file only before the very first write
3. Rewrites it with guidance to prevent the same failure
4. Writes it back to the KV store — JetStream versions every write with an atomic revision number, so there's no separate `.bak`/file-glob bookkeeping and no race between concurrent reflect runs
5. Every planner/executor/verifier replica re-reads its skill from KV **per task** (not once at process start), so the update is live everywhere on the very next task — no pod restart needed

Inspect the current skill and its revision history:
```bash
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv get cxp-skills executor
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv history cxp-skills executor
```

---

## Swarm halt on error

Any unhandled agent exception sets a shared halt flag (`cxp-state` KV bucket, key `halt`) with the failing agent, packet, and reason. While halted:
- Every agent stops processing new packets (checked at the top of `agent_shell.py`'s message handler)
- `POST /api/submit` returns `409` with the halt reason instead of queuing new work
- The web dashboard shows a red "⛔ SWARM HALTED" banner with the reason and a **Resume** button

Clear it manually if needed:
```bash
curl -X POST http://localhost/api/halt/clear
```

---

## Autonomous testing

A **CronJob** runs the test suite hourly inside the cluster:
- Submits 3 test tasks (CODE_GENERATION, ERROR_HANDLING, STRUCTURED_OUTPUT)
- Evaluates results against pass thresholds, retries failures once
- Triggers the reflect loop on failures, categorized by timeout / format / quality (plus a catch-all for anything uncategorized)
- Writes JSON results to `tests/results/`, then **clones and pushes** to `main` (a real fast-forward push now, not a disconnected `git init` that silently failed)
- Deletes its own Job only *after* the push completes, and only if every test passed — keeps the job around for debugging on failure

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
| planner | `plan` | qwen2.5:1.5b | Decomposes goals into sub-tasks |
| executor ×2 | `code` | qwen2.5:1.5b | Generates artifacts |
| verifier | `verify` | qwen2.5:1.5b | Scores output quality |
| assessor | `assess` | qwen2.5:0.5b | Labels artifacts with capability tags |
| deployer | `deploy` | — (no LLM) | `kubectl apply`s YAML / runs code in `cxp-sandbox`, score ≥ 0.85 only |
| reflect | `reflect` | qwen2.5:1.5b | Rewrites skill files on failure |
| dashboard | — | — | Terminal UI (`make dashboard`) |
| web | — | — | Browser UI at http://localhost |

Only the `deployer` pod carries the `kubectl` binary (downloaded at init-container time) and a ServiceAccount scoped — via Role/RoleBinding — to the `cxp-sandbox` namespace only. It cannot touch the `cxp` namespace the agents themselves run in.

---

## Project layout

```
main.py                 entry point — submit tasks, run agents, run dashboard/web
requirements.txt

src/
  packet.py             CXP packet schema (Pydantic)
  agent_shell.py        base agent: NATS listener, LLM caller, JetStream KV helpers
                         (shared skills, swarm halt flag), reputation recording
  memory.py             reputation + episodic/semantic memory — JSON file on a
                         shared PVC, cross-process-safe via flock + delta merge
  dashboard.py           Rich terminal UI
  web_dashboard.py       FastAPI web UI + task submission + halt banner/resume
  agents/
    planner.py           decomposes tasks, spawns sub-packets
    executor.py          generates artifacts, auto-spawns verify
    verifier.py          grades output (0.0–1.0), spawns reflect/assess/deploy
    assessor.py          labels artifacts with AI capability tags
    deployer.py           kubectl-applies YAML / runs code artifacts, sandbox-scoped
    reflect.py            rewrites skill files via the shared KV store

skills/                  ConfigMap-seeded starting text for each skill
  planner_v1.md
  executor_v1.md         ← live copy lives in NATS KV once reflect runs once
  verifier_v1.md

tests/
  run_tests.py           self-improving test runner (submit → evaluate → reflect → retry)
  results/               JSON result files per run (pushed to git by the CronJob)

helm/cxp/
  app/                   mirror of main.py / src/ / tests/run_tests.py — this copy
                         is what app-code.yaml actually bakes into ConfigMaps.
                         Kept manually in sync with the top-level copies; nothing
                         enforces that automatically, so edit both or diff before
                         deploying if you're not using `make deploy`.
  Chart.yaml             nats + ollama + traefik sub-charts
  values.yaml            per-agent model config, replicas, storage
  templates/
    agents.yaml          Deployments — python:3.12-slim + init container code assembly
    deployer-rbac.yaml    ServiceAccount/Role/RoleBinding scoping deployer to cxp-sandbox
    app-code.yaml         ConfigMaps embedding all source code (from helm/cxp/app/)
    config.yaml           PVC (helm.sh/resource-policy: keep) + skills ConfigMap
    web-service.yaml      ClusterIP service + Traefik IngressRoute
    test-runner.yaml      CronJob for hourly autonomous testing
    sandbox-namespace.yaml  cxp-sandbox namespace the deployer targets

Makefile               deploy / reset / test / submit / dashboard / logs
kind-config.yaml       kind cluster with ports 80, 443, 4222 mapped
```

---

## Configuration (helm/cxp/values.yaml)

| Key | Default | What it does |
|-----|---------|-------------|
| `agents.*.model` | varies | Per-agent LLM model |
| `agents.executor.replicas` | 2 | Parallel executors |
| `memoryPVC.size` | 1Gi | Shared memory + results (ReadWriteOnce — fine on this single-node kind cluster; would need ReadWriteMany on a real multi-node cluster) |
| `ollamaModel` | qwen2.5:1.5b | Default fallback model |

---

## Known limitations

- **Deploy path is sandbox-scoped but not fully isolated.** Only the YAML→`kubectl apply` path is namespace-scoped. Python/shell artifacts still run as a plain `subprocess` on the deployer pod (minimal explicit env, but no namespace/seccomp/network isolation).
- **`ReadWriteOnce` memory PVC** works today only because the kind cluster is single-node. Move to ReadWriteMany (or the KV-store pattern used for skills) before running multi-node.
- **`src`/`helm/cxp/app/` duplication is manual.** `make deploy` presumably keeps them in sync (check the Makefile); editing one tree directly without the other will drift silently.
- **Reflect only maintains the `executor` skill** — planner/verifier skill files exist but nothing currently rewrites them.
- **Small local models** (`qwen2.5:0.5b`/`1.5b`) occasionally emit malformed JSON, especially from planner's sub-task decomposition — this is normal and will trigger the halt gate, not a bug to chase.

---

## Debugging Commands

### Watch system in real-time
```bash
kubectl get pods -n cxp -w
```

### Check NATS / JetStream KV state
```bash
kubectl exec -n cxp deploy/cxp-nats-box -- nats stat
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv ls
kubectl exec -n cxp deploy/cxp-nats-box -- nats kv get cxp-state halt
```

### Tail agent logs
```bash
kubectl logs -n cxp deploy/cxp-planner -f
kubectl logs -n cxp deploy/cxp-executor -f
kubectl logs -n cxp deploy/cxp-verifier -f
```

### View dashboard API state (includes halt status)
```bash
kubectl port-forward -n cxp svc/cxp-web 8080:8080 &
curl http://localhost:8080/api/state | jq .
```

### Check memory (reputation + episodic/semantic)
```bash
kubectl exec -n cxp deploy/cxp-planner -- cat /data/memory.json | jq '.reputation'
```

### Clear a swarm halt
```bash
curl -X POST http://localhost/api/halt/clear
```

### Restart stuck agents
```bash
kubectl rollout restart -n cxp deploy/cxp-planner deploy/cxp-executor deploy/cxp-verifier
```

### Run test suite
```bash
make test-now          # trigger CronJob immediately
make test               # run tests locally
```

---

## Deploying to a server

Works on any k8s cluster, though this project has only been run against a local single-node kind cluster — check the "Known limitations" above (especially the memory PVC access mode) before pointing it at anything multi-node or shared.

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

## AI capability labeling

Every completed artifact is labeled by the **assessor agent**, which reads it and classifies what AI capabilities it demonstrates.

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

## Extending

**Add a new capability** — subclass `AgentShell`, set `capabilities`, implement `_execute`. Deploy as a new Deployment in `helm/cxp/values.yaml` under `agents`.

**Swap the LLM** — change `OLLAMA_MODEL` env var / the agent's `model:` field in values.yaml. Any Ollama-compatible model works.

**Scale executors** — increase `agents.executor.replicas` in values.yaml and `helm upgrade`.
