# CXP — Context Exchange Protocol

A self-improving distributed AI swarm: small local LLMs (via Ollama) passing structured packets through a routing layer, testing themselves hourly, and rewriting their own skill files when they fail. Prototype — built to explore the pattern, not production-hardened.

> **Naming note:** "Protocol" is aspirational right now. What exists is a well-defined internal message *schema* (`src/packet.py`'s `CXPPacket`) that this one Python codebase uses to talk to itself across pods — there's no spec independent of that class, no schema version number, and nothing outside this repo has ever produced or consumed a CXP packet. See [docs/architecture.md](docs/architecture.md#is-this-actually-a-protocol) for what it'd take to actually earn the name.

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

## Screenshots

**Web dashboard** (`http://localhost`) — live agent state, reputation per capability, packet history, and the LLM thinking stream. The red banner and error trace here show the halt gate catching a real `ReadTimeout` from Ollama under concurrent load:

![Web dashboard](docs/screenshots/web-dashboard.png)

**The swarm's pods in `kubectl get pods`** — one Deployment per agent role, `cxp-nats` for JetStream, `cxp-ollama` for inference, and a `cxp-test-*` Job from the hourly self-test CronJob:

![kubectl get pods -n cxp](docs/screenshots/k8s-pods.png)

---

## Hardware requirements

- **Realistic minimum: 10-core CPU, 8GB RAM, 10GB disk.** This isn't a conservative guess — it's what this project has actually been run on. Ollama alone was observed running two loaded-model runner processes simultaneously at ~470% CPU *each* (nearly the entire node) before resource limits were added; on anything smaller, expect the same contention (timeouts, even real `500` errors from Ollama itself under load) that this project hit and fixed. There are now **two Ollama instances**, split by model rather than shared: the main one (`qwen2.5:1.5b`, serving planner/executor/verifier/reflect) capped at 3.5 CPU/3Gi, and a smaller dedicated one (`qwen2.5:0.5b`, `assessor` only) at 1.5 CPU/1.5Gi — combined, about the same total budget the single shared instance had before, just no longer colliding when `assessor` and `reflect` fire at the same instant (which happens on every single verifier pass). Tune down only if you also reduce how many agents/models are in play.
- Current setup uses `qwen2.5:1.5b` (assessor uses the smaller `qwen2.5:0.5b`) — small models chosen to run comfortably on modest hardware, at the cost of occasionally malformed JSON output (planner is the most sensitive to this).
- Bigger/better models: edit `helm/cxp/values.yaml` per-agent `model:` field; any Ollama-compatible model works. Check actual headroom first (`docker exec <kind-node> sh -c 'nproc; free -h'`) — a bigger model needs more of both, on top of what's already tightly budgeted here.

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

A **CronJob** runs the test suite hourly inside the cluster. Full detail — including the finish-line definition and drafted tier-3 tests for after that — lives in [`tests/STRATEGY.md`](tests/STRATEGY.md); short version:

- **Tier 0 — smoke test.** One trivial task ("print hello world"), always first. A failure here means the pipeline itself is broken — a different, more urgent signal than "bad at capability X" — but the suite still continues for more signal rather than aborting.
- **Tier 1 — capability coverage.** 8 tests, one per [assessor capability label](#ai-capability-labeling) except `SELF_IMPROVEMENT` (doesn't fit the pass/fail shape): `CODE_GENERATION`, `ERROR_HANDLING`, `STRUCTURED_OUTPUT`, `DECOMPOSITION`, `SECURITY_AWARENESS`, `INFRA_AS_CODE`, `TESTING`, `DOCUMENTATION`. Each retries once on failure, and failures trigger reflect, categorized by timeout / format / quality (plus a catch-all for anything uncategorized).
- **Tier 2 — regression check.** Compares this run's average `code`-capability score against recent history in episodic memory (the same PVC agents write to). A real drop since the last skill revision — not just "missed today's threshold" — triggers a distinct `REGRESSION` reflect task.

All of this runs fully sequentially (one task submitted at a time, waiting for it to settle before the next — running them concurrently piled up enough simultaneous LLM calls to blow past Ollama's read timeout), and checks the swarm's halt state before every single submission — if the swarm halts mid-run, remaining tests are marked `SKIPPED` (not `FAIL`) and reflect-triggering is skipped, instead of cascading into a wall of 409s that reads as a false capability regression.

Results are written to `tests/results/`, then cloned and pushed to a dedicated **`bot/test-results` branch — never `main` directly** (the bot racing a human's own push to `main` is exactly what happened once already; a human merges that branch in whenever they choose). The push step also runs [`check_plateau.py`](tests/check_plateau.py), which reports how many consecutive 8/8-pass runs have accumulated toward the finish line. The Job deletes itself only *after* the push completes, and only if every test passed — kept around for debugging on failure, with `backoffLimit: 1` so a systemic failure (e.g. Ollama under load) fails fast instead of retrying 7 times over ~2 hours.

Trigger immediately:
```bash
make test-now
```

Run locally — **note:** `run_tests.py` targets `http://cxp-web:8080` (in-cluster DNS), unreachable from the host without a port-forward first:
```bash
kubectl port-forward -n cxp svc/cxp-web 8080:8080 &
CXP_WEB_API=http://localhost:8080 make test
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
  Chart.yaml             nats + ollama (×2, aliased ollama-small for assessor) + traefik sub-charts
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
- **`src`/`helm/cxp/app/` duplication is real, but `make deploy`/`make sync` handle it** — the Makefile's `sync` target copies `src/`, `main.py`, and `tests/run_tests.py` into `helm/cxp/app/` before every deploy. Only a problem if you edit the `helm/cxp/app/` copy directly, or run `helm upgrade` without going through `make deploy` first.
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
