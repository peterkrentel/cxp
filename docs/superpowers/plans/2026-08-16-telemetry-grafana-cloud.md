# Telemetry via Grafana Cloud (OTel SDK) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Status: design/backlog, not approved for execution.** Explicitly deferred until the Oracle Cloud VM exists (see [`2026-08-16-oracle-cloud-migration.md`](2026-08-16-oracle-cloud-migration.md)) — the user was clear: "not until we get to oracle." Captured now so the design (Grafana Cloud, OTel SDK, no self-hosted Prometheus) doesn't need re-deriving later.

**Goal:** Answer, without another manual `kubectl exec` investigation session, the questions that took a full day of ad-hoc digging today: how often does the swarm halt, what fraction auto-resolves vs. needs a human, how slow are LLM calls actually running, and is Ollama under contention right now — via metrics/traces shipped to Grafana Cloud, not a self-hosted stack. Metrics alone answer "what's happening"; Task 4 adds alert rules on top so deviations from expected behavior are pushed to the user instead of only found by going looking — which is how all three bugs found today (the dead `cxp.cap.any` subject, the 80-minute hung LLM call, the diagnostician's own recursive crash loop) actually got discovered: noticing something *felt* off, not a signal telling anyone so.

**Architecture:** Each Python agent (`AgentShell` subclasses) instruments itself directly with the OpenTelemetry Python SDK — metrics (counters/histograms) and optionally traces for the plan→code→verify→reflect pipeline — and exports via OTLP straight to Grafana Cloud's OTLP gateway endpoint. **No self-hosted Prometheus, no Grafana Agent/Alloy scrape step, no local OTel Collector** — the user's explicit call, since Grafana Cloud accepts OTLP directly and a self-hosted metrics stack has its own real CPU/memory footprint on a node that's already tight on Ollama's budget (the exact contention problem this whole project has fought all day). Grafana Cloud's hosted backend (Mimir-based) is the storage and query layer; Grafana Cloud's own hosted Grafana is the dashboard — nothing new to operate locally.

**Tech Stack:** `opentelemetry-sdk`, `opentelemetry-exporter-otlp` (Python), Grafana Cloud (OTLP ingestion endpoint + hosted Grafana), no new Kubernetes workloads.

## Global Constraints

- No self-hosted Prometheus, Grafana, Grafana Agent, or OTel Collector in-cluster — OTLP export goes directly from each agent process to Grafana Cloud's endpoint. If direct export ever proves unreliable, a local OTel Collector as a buffering hop is the fallback (see Open Questions), not a default.
- Do not begin implementation before the Oracle Cloud VM exists and this plan gets an explicit go-ahead.
- Grafana Cloud API credentials (OTLP endpoint URL + Basic Auth instance ID/API token) must be stored as a Kubernetes Secret, never committed to git or hardcoded in a ConfigMap-sourced `.py` file (the existing `cxp-git-creds` Secret pattern, used for the test-runner's git push credentials, is the precedent to follow).
- Never add `Co-Authored-By: Claude` to any commit this plan produces.

---

### Task 1: Instrument `AgentShell` with OTel metrics

**Files:**
- Modify: `src/agent_shell.py` (add OTel SDK setup + metric instruments to the shared base class so every agent gets them for free)
- Modify: `requirements.txt` (add `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`)

**Interfaces:**
- Consumes: nothing new from existing code — instruments the *existing* `handle()` control flow (success path, exception path, halt-drop path) already in `agent_shell.py`.
- Produces: four metric instruments available on every `AgentShell` instance: `cxp_llm_call_duration_seconds` (histogram), `cxp_llm_calls_total` (counter, labeled `outcome=success|timeout|error`), `cxp_halts_total` (counter, labeled `auto_resolved=true|false` — incremented only in `diagnostician.py`, Task 2), `cxp_packets_processed_total` (counter, labeled `agent`, `capability`, `status`).

- [ ] **Step 1: Add the OTel setup and instrument definitions**

```python
# src/agent_shell.py — add near the other module-level constants
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

if OTEL_EXPORTER_OTLP_ENDPOINT:
    _reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))

_meter = metrics.get_meter("cxp.agent_shell")
LLM_CALL_DURATION = _meter.create_histogram(
    "cxp_llm_call_duration_seconds", unit="s", description="Duration of a single self.llm() call"
)
LLM_CALLS_TOTAL = _meter.create_counter(
    "cxp_llm_calls_total", description="Count of self.llm() calls by outcome"
)
PACKETS_PROCESSED_TOTAL = _meter.create_counter(
    "cxp_packets_processed_total", description="Count of packets processed by agent/capability/status"
)
```

If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset (e.g. running locally without a Grafana Cloud credential configured), the SDK's default no-op behavior means these instruments simply do nothing — no crash, no required credential for local dev.

- [ ] **Step 2: Record LLM call duration and outcome in `llm()`**

Wrap the existing timed section in `agent_shell.py`'s `llm()` method (the `asyncio.wait_for(_stream(client), ...)` call added earlier today) with timing and a metric record:

```python
import time

start = time.monotonic()
outcome = "success"
try:
    full = await asyncio.wait_for(_stream(client), timeout=LLM_TOTAL_TIMEOUT)
except asyncio.TimeoutError:
    outcome = "timeout"
    raise TimeoutError(f"LLM call exceeded total budget of {LLM_TOTAL_TIMEOUT}s")
except httpx.HTTPStatusError as e:
    outcome = "error"
    if e.response.status_code == 404:
        raise RuntimeError(
            f"Model '{OLLAMA_MODEL}' not found in Ollama. "
            f"Available models must be pre-cached in PVC. "
            f"Auto-pull is disabled to prevent PostStartHook failures."
        )
    raise
finally:
    LLM_CALL_DURATION.record(time.monotonic() - start, {"agent": self.agent_id})
    LLM_CALLS_TOTAL.add(1, {"agent": self.agent_id, "outcome": outcome})
```

- [ ] **Step 3: Record packet outcome in the shared `handle()` closure**

At the two existing recording points in `handle()` — the success path (after `packet.complete(...)`) and the exception path (after `packet.fail(...)`) — add:

```python
PACKETS_PROCESSED_TOTAL.add(1, {"agent": self.agent_id, "capability": packet.capability, "status": "done"})
# ...and in the except branch:
PACKETS_PROCESSED_TOTAL.add(1, {"agent": self.agent_id, "capability": packet.capability, "status": "error"})
```

- [ ] **Step 4: Add the OTel packages to `requirements.txt`**

```
opentelemetry-sdk
opentelemetry-exporter-otlp-proto-http
```

- [ ] **Step 5: Copy the change into the Helm-packaged duplicate and commit**

```bash
cp src/agent_shell.py helm/cxp/app/src/agent_shell.py
cp requirements.txt helm/cxp/app/requirements.txt
git add src/agent_shell.py helm/cxp/app/src/agent_shell.py requirements.txt helm/cxp/app/requirements.txt
git commit -m "feat: instrument AgentShell with OTel metrics (LLM duration, packet outcomes)"
```

---

### Task 2: Record halt/auto-resolve metrics in the diagnostician

**Files:**
- Modify: `src/agents/diagnostician.py`

**Interfaces:**
- Consumes: the `_meter` and metric-creation pattern from Task 1 (import `metrics.get_meter("cxp.diagnostician")` the same way).
- Produces: `cxp_halts_total{auto_resolved="true"|"false"}`, incremented at the same two points `_execute()` already decides the outcome (the `is_timeout_class` auto-resolve branch, and the non-timeout LLM-diagnosis branch).

- [ ] **Step 1: Add the halt-outcome counter**

```python
from opentelemetry import metrics

_meter = metrics.get_meter("cxp.diagnostician")
HALTS_TOTAL = _meter.create_counter("cxp_halts_total", description="Swarm halts by whether they auto-resolved")
```

- [ ] **Step 2: Increment it at both existing outcome points in `_execute()`**

In the `if is_timeout_class:` branch, right before `return json.dumps(...)`:
```python
HALTS_TOTAL.add(1, {"auto_resolved": "true"})
```

In the non-timeout branch, right before `return json.dumps(...)`:
```python
HALTS_TOTAL.add(1, {"auto_resolved": "false"})
```

- [ ] **Step 3: Copy into the Helm duplicate and commit**

```bash
cp src/agents/diagnostician.py helm/cxp/app/src/agents/diagnostician.py
git add src/agents/diagnostician.py helm/cxp/app/src/agents/diagnostician.py
git commit -m "feat: record halt auto-resolve outcome as an OTel metric"
```

---

### Task 3: Wire the Grafana Cloud credential and OTLP endpoint into Helm

**Files:**
- Create: `helm/cxp/templates/grafana-cloud-secret.yaml` (references an externally-created Secret — does not embed the credential itself)
- Modify: `helm/cxp/templates/agents.yaml` (add `OTEL_EXPORTER_OTLP_ENDPOINT` and OTLP auth env vars to every agent container)
- Modify: `helm/cxp/values.yaml` (add the OTLP endpoint URL as a value; the auth token stays out of values.yaml entirely, sourced only from the Secret)

**Interfaces:**
- Consumes: `OTEL_EXPORTER_OTLP_ENDPOINT` (read by Task 1's SDK setup).
- Produces: nothing consumed by later tasks — this is the last task, wiring credentials through to what Tasks 1-2 already built.

- [ ] **Step 1: Create the Secret out-of-band (not via a committed manifest — same pattern as `cxp-git-creds`)**

```bash
kubectl create secret generic cxp-grafana-cloud \
  --namespace cxp \
  --from-literal=otlp-endpoint="https://otlp-gateway-<region>.grafana.net/otlp" \
  --from-literal=otlp-auth-header="Basic <base64 instance-id:api-token>"
```

- [ ] **Step 2: Add the env vars to every agent container in `agents.yaml`**

In the `env:` block that already exists for every role (`helm/cxp/templates/agents.yaml`, near `NATS_URL`/`OLLAMA_URL`):

```yaml
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              valueFrom:
                secretKeyRef:
                  name: cxp-grafana-cloud
                  key: otlp-endpoint
                  optional: true
            - name: OTEL_EXPORTER_OTLP_HEADERS
              valueFrom:
                secretKeyRef:
                  name: cxp-grafana-cloud
                  key: otlp-auth-header
                  optional: true
```

`optional: true` on both means a cluster with no `cxp-grafana-cloud` Secret created (e.g. local dev, or before this is rolled out) just runs with telemetry disabled (Task 1's no-op fallback), not a crash.

- [ ] **Step 3: Commit**

```bash
git add helm/cxp/templates/agents.yaml
git commit -m "feat: wire optional Grafana Cloud OTLP credential into every agent"
```

Note: no `values.yaml` change ended up needed — the endpoint comes from the Secret (Step 1), not a plain value, since it's not meaningfully different in sensitivity from the auth header itself (both identify which Grafana Cloud account receives data).

---

## Open Questions (unresolved by this plan)

- **Existing Node proxy on Render.** The user mentioned already running a Node.js proxy on Render that relays to Grafana Cloud for other projects — unclear whether that's for hiding credentials from a browser-facing client, CORS, request transformation, or some other reason specific to those projects. If that reason applies here too, CXP's agents would export OTLP to that proxy's URL instead of Grafana Cloud's endpoint directly — same `OTEL_EXPORTER_OTLP_ENDPOINT` env var, different value, no code change either way. Needs a decision once this plan is picked back up: export directly to Grafana Cloud, or through the existing Render proxy?
- **Traces, not just metrics.** This plan only covers metrics (the concrete questions from today: halt rate, auto-resolve rate, LLM latency). Full distributed tracing of a plan→code→verify→reflect chain (via OTel spans linked by `task_id`) would be a richer but bigger addition — deliberately out of scope for this first pass.
- **Direct-export reliability.** Exporting straight from each short-lived agent process (no local buffering Collector) means a pod restart mid-export could drop a data point. Acceptable for a first pass given the low stakes (dashboards, not alerting-critical data); revisit with a local OTel Collector as a buffering hop only if data loss turns out to matter in practice.

## Self-Review Notes

- **Spec coverage:** Task 1 covers the core metrics (LLM duration/outcome, packet outcome) added to the shared base class so every agent gets them without per-agent duplication; Task 2 covers the halt/auto-resolve metric specific to the diagnostician; Task 3 covers getting a real credential into the cluster safely.
- **Placeholder scan:** the Grafana Cloud OTLP endpoint URL and auth header in Task 3 Step 1 are real formats (Grafana Cloud's documented OTLP gateway URL pattern and Basic Auth header scheme) with placeholder `<region>`/`<base64 ...>` values that the user fills in with their actual Grafana Cloud instance details — not a TBD, an expected per-account substitution.
- **Type consistency:** the `_meter`/instrument-creation pattern in Task 2 mirrors Task 1's exactly (same `metrics.get_meter(...)` call shape, same `.add()`/`.record()` usage) — no drift.
