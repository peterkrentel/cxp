NAMESPACE  = cxp
RELEASE    = cxp
CHART      = helm/cxp

# Isolated Helm repo config, scoped to just this project's 3 dependencies
# (nats, ollama-helm, traefik) -- without this, `helm dependency update`
# refreshes every repo registered globally on the machine (kyverno, istio,
# grafana, etc. from unrelated projects), making real network calls to
# repos cxp has nothing to do with on every single deploy.
export HELM_REPOSITORY_CONFIG := $(CURDIR)/$(CHART)/.helm-repos.yaml

.PHONY: deploy sync destroy reset submit dashboard logs web cluster helm-repos

# Sync src → Helm chart, install/upgrade — access via http://localhost
deploy: sync helm-repos
	helm dependency update $(CHART)
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace
	@echo "✓ Access: http://localhost"

# Ensure the isolated repo config (above) has exactly this chart's
# dependencies registered. Idempotent -- re-adding an already-present repo
# at the same URL is a silent no-op.
helm-repos:
	@helm repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
	@helm repo add ollama-helm https://otwld.github.io/ollama-helm/ >/dev/null 2>&1 || true
	@helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true

# Sync src files into Helm ConfigMaps
sync:
	cp src/*.py $(CHART)/app/src/
	cp src/agents/*.py $(CHART)/app/src/agents/
	cp main.py $(CHART)/app/main.py
	cp requirements.txt $(CHART)/app/requirements.txt
	mkdir -p $(CHART)/app/tests
	cp tests/run_tests.py $(CHART)/app/tests/run_tests.py
	cp tests/check_plateau.py $(CHART)/app/tests/check_plateau.py
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
