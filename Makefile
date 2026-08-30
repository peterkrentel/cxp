NAMESPACE  = cxp
RELEASE    = cxp
CHART      = helm/cxp

# Isolated Helm repo config, scoped to just this project's 3 dependencies
# (nats, ollama-helm, traefik) -- without this, `helm dependency update`
# refreshes every repo registered globally on the machine (kyverno, istio,
# grafana, etc. from unrelated projects), making real network calls to
# repos cxp has nothing to do with on every single deploy.
export HELM_REPOSITORY_CONFIG := $(CURDIR)/$(CHART)/.helm-repos.yaml

.PHONY: deploy sync destroy reset submit dashboard logs web cluster helm-repos otel-secret otel-secret-if-present ollama-pull

# Local, gitignored overrides (e.g. otel.enabled=true) -- kept out of the
# committed values.yaml on purpose, since CI's ephemeral cluster has no
# matching Secret (cxp-otel-credentials) and would fail to deploy if that
# default were ever flipped on there. Applied last, so it wins over the
# chart's own defaults, only when the file actually exists.
LOCAL_VALUES := $(CHART)/values.local.yaml
LOCAL_VALUES_FLAG := $(if $(wildcard $(LOCAL_VALUES)),-f $(LOCAL_VALUES))

# Local, gitignored Grafana Cloud OTLP credentials -- see docs/otel-setup.md.
# A kind cluster's Secrets live only in that node's container, so recreating
# the cluster (make cluster/reset) silently wipes cxp-otel-credentials even
# though values.local.yaml still says otel.enabled: true, leaving every pod
# stuck in CreateContainerConfigError until this is re-run.
OTEL_ENV := .env.otel

# Sync src → Helm chart, install/upgrade — access via http://localhost
deploy: sync helm-repos otel-secret-if-present
	helm dependency update $(CHART)
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace $(LOCAL_VALUES_FLAG)
	@echo "✓ Access: http://localhost"

# Ensure the isolated repo config (above) has exactly this chart's
# dependencies registered. Idempotent -- re-adding an already-present repo
# at the same URL is a silent no-op.
helm-repos:
	@helm repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
	@helm repo add ollama-helm https://otwld.github.io/ollama-helm/ >/dev/null 2>&1 || true
	@helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true

# Recreate the cxp-otel-credentials Secret from .env.otel (gitignored,
# CXP_OTEL_INSTANCE_ID + CXP_OTEL_API_TOKEN). Idempotent (server-side apply)
# -- safe to re-run any time, in particular right after every make cluster/reset.
otel-secret:
	@test -f $(OTEL_ENV) || { echo "✗ Missing $(OTEL_ENV) -- see docs/otel-setup.md"; exit 1; }
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@set -a; . ./$(OTEL_ENV); set +a; \
	B64=$$(printf '%s:%s' "$$CXP_OTEL_INSTANCE_ID" "$$CXP_OTEL_API_TOKEN" | base64 | tr -d '\n'); \
	kubectl create secret generic cxp-otel-credentials -n $(NAMESPACE) \
	  --from-literal=headers="Authorization=Basic%20$${B64}" \
	  --dry-run=client -o yaml | kubectl apply -f -
	@echo "✓ cxp-otel-credentials ready in namespace $(NAMESPACE)"

# Called by deploy itself -- silent no-op when .env.otel doesn't exist (e.g.
# CI, or a dev who hasn't set up Grafana Cloud), so this never breaks anyone
# else's deploy the way a hard dependency on otel-secret would.
otel-secret-if-present:
	@test -f $(OTEL_ENV) && $(MAKE) otel-secret || true

# Sync src files into Helm ConfigMaps
sync:
	cp src/*.py $(CHART)/app/src/
	cp src/agents/*.py $(CHART)/app/src/agents/
	cp main.py $(CHART)/app/main.py
	cp requirements.txt $(CHART)/app/requirements.txt
	mkdir -p $(CHART)/app/tests
	cp tests/run_tests.py $(CHART)/app/tests/run_tests.py
	cp tests/check_plateau.py $(CHART)/app/tests/check_plateau.py
	cp tests/evaluate_candidate.py $(CHART)/app/tests/evaluate_candidate.py
	mkdir -p $(CHART)/app/scripts
	cp scripts/sandbox_reaper.py $(CHART)/app/scripts/sandbox_reaper.py

# Destroy release but keep memory PVC
destroy:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE) || true

# Full reset: recreate kind cluster with correct port mappings, then deploy
reset:
	kind delete cluster --name cxp || true
	kind create cluster --config kind-config.yaml
	$(MAKE) deploy
	$(MAKE) ollama-pull

# Models aren't part of the Helm chart (values.yaml's `models: []` --
# auto-pull at pod startup causes PostStartHook failures, see agent_shell.py).
# A fresh kind cluster means a fresh, empty PVC per Ollama instance -- this
# restores what agents actually need to run. Idempotent: `ollama pull` is a
# no-op if the model's already cached. Model names must track
# values.yaml's ollamaModel (main) / assessor.model (small).
OLLAMA_MODEL := qwen2.5:1.5b
OLLAMA_SMALL_MODEL := qwen2.5:0.5b

ollama-pull:
	@kubectl rollout status deployment/cxp-ollama -n $(NAMESPACE) --timeout=180s
	@kubectl rollout status deployment/cxp-ollama-small -n $(NAMESPACE) --timeout=180s
	kubectl exec -n $(NAMESPACE) deploy/cxp-ollama -- ollama pull $(OLLAMA_MODEL)
	kubectl exec -n $(NAMESPACE) deploy/cxp-ollama-small -- ollama pull $(OLLAMA_SMALL_MODEL)

# Recreate kind cluster only
cluster:
	kind delete cluster --name cxp || true
	kind create cluster --config kind-config.yaml

# Submit a task
submit:
	kubectl exec -n $(NAMESPACE) deploy/cxp-dashboard -- python /app/main.py submit "$(GOAL)"

# Fallback port-forward if ingress unavailable
web:
	kubectl port-forward -n $(NAMESPACE) svc/cxp-web 8080:8080 --address 0.0.0.0

# Live terminal dashboard
dashboard:
	kubectl exec -it -n $(NAMESPACE) deploy/cxp-dashboard -- python /app/main.py dashboard

# Tail all agent logs
logs:
	kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/name=cxp --prefix --follow

# Run test suite manually
test:
	python3 tests/run_tests.py

# Watch test results as they accumulate
test-watch:
	watch -n 30 'ls -lt tests/results/*.json 2>/dev/null | head -5'

# Trigger test CronJob immediately
test-now:
	kubectl create job -n $(NAMESPACE) --from=cronjob/cxp-test-runner cxp-test-$(shell date +%s)
