# OpenTelemetry setup (Grafana Cloud)

Every LLM call gets an OTel span with the full system/user prompt and full
response (or, on a timeout, whatever partial content had actually been
generated) — see [`src/telemetry.py`](../src/telemetry.py) for why this
exists. Spans export via OTLP straight to Grafana Cloud; there's no
in-cluster collector (no Alloy, no Collector Deployment) — Grafana Cloud's
gateway ingests OTLP directly.

Gated behind `helm/cxp/values.yaml`'s `otel.enabled` (default `false`), so
none of this does anything until the Secret below actually exists.

## One-time setup

1. In Grafana Cloud, go to **Connections → Add new connection → search
   "OpenTelemetry"** (or your stack's own OTLP setup page). Generate an
   API token there (**Password/API token → Generate now**) — name it
   something identifiable, e.g. `cxp-swarm-otlp`. It's shown only once.

2. That page gives you three things:
   - **OTLP endpoint** — e.g. `https://otlp-gateway-prod-us-east-3.grafana.net/otlp`.
     Not secret; already set in `helm/cxp/values.yaml`'s `otel.endpoint`.
   - **Instance ID** — a plain number (e.g. `1701874`), the *username*
     half of Basic auth. Easiest place to find it if it's not obviously
     labeled: the Alloy config snippet the page generates for you
     includes an `otelcol.auth.basic` block with `username`/`password`
     fields spelled out directly.
   - **API token** (the `glc_...` string) — the *password* half.

3. Base64-encode `<instanceID>:<token>` and build the Secret. Do this
   with your own shell, not by pasting the token anywhere it'll be
   logged or committed:

   ```bash
   B64=$(printf '%s' "<instanceID>:<token>" | base64 | tr -d '\n')
   kubectl create secret generic cxp-otel-credentials -n cxp \
     --from-literal=headers="Authorization=Basic%20${B64}"
   ```

   The `%20` (not a literal space) matters — Python's OTLP exporter is
   picky about a raw space in `OTEL_EXPORTER_OTLP_HEADERS` and silently
   mis-parses it otherwise.

4. Create `helm/cxp/values.local.yaml` (gitignored, `make deploy` picks
   it up automatically if present — see the Makefile's `LOCAL_VALUES_FLAG`):

   ```yaml
   otel:
     enabled: true
   ```

   **Don't** flip `otel.enabled` in the committed `values.yaml` itself —
   CI's `deploy-check` spins up its own ephemeral cluster with no
   matching Secret, and would fail to deploy if that default were ever
   `true` there. `values.local.yaml` keeps this a local-only override.

5. `make deploy`.

## Verifying it's working

Submit any task, then check Grafana Cloud's trace explorer (Tempo) for a
`cxp-<role>` service (e.g. `cxp-planner`, `cxp-executor`) with `llm.call`
spans. Each span carries:

| Attribute | Meaning |
|---|---|
| `agent.id` | which replica handled the call (e.g. `planner-1`) |
| `packet.id` | the packet this call was for — cross-reference against the dashboard's packet list |
| `task.id` | the whole task's lineage — every packet (plan/code/verify/reflect/assess/deploy) spawned for one submitted goal shares this, so you can filter Tempo to one task's full timeline |
| `parent.packet.id` | the packet that spawned this one — walk this to reconstruct the exact plan→code→verify→... chain, since `task.id` alone doesn't give ordering |
| `llm.system_prompt` / `llm.user_prompt` | the full, untruncated prompt sent to Ollama |
| `llm.response` | the full response, if the call completed normally |
| `llm.partial_response` | whatever content had been generated so far, if the call timed out (`llm.timed_out: true`) — `llm.response` is absent in this case, not empty |
| `llm.duration_seconds` | wall-clock time for the call |

If Grafana's own "Test connection" button on the OTLP setup page spins
forever: that's expected before any real data has been sent, not a sign
anything's broken — it's polling for data that doesn't exist yet. Skip it
and just check the trace explorer directly once the swarm is submitting
tasks.

## Rotating or revoking the token

Delete the access policy/token in Grafana Cloud, generate a new one, then
just re-run the `kubectl create secret` step above (add `--dry-run=client
-o yaml | kubectl apply -f -` to overwrite the existing Secret in place
without deleting it first). No code or Helm changes needed — the pods
pick up a new Secret's contents automatically on their next scheduled
restart, or immediately if you `kubectl rollout restart` them.

## Why no local Alloy collector

Grafana's own onboarding page for OTLP defaults to recommending you run
**Grafana Alloy** locally (app → `localhost:4317` → Alloy → Grafana
Cloud) — useful for enrichment (resource detection, batching) and for
not needing cloud credentials on every host. This project skips it:
each agent already exports directly to Grafana Cloud's OTLP gateway over
HTTP, which the gateway supports natively. One fewer moving part in a
single-node kind cluster, at the cost of the enrichment Alloy would add
(not currently missed for what this project needs from tracing).
