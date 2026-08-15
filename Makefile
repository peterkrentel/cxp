NAMESPACE  = cxp
RELEASE    = cxp
CHART      = helm/cxp

.PHONY: deploy sync destroy reset submit dashboard logs web cluster

# Sync src → Helm chart, install/upgrade — access via http://localhost
deploy: sync
	helm dependency update $(CHART)
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace
	@echo "✓ Access: http://localhost"

# Sync src files into Helm ConfigMaps
sync:
	cp src/*.py $(CHART)/app/src/
	cp src/agents/*.py $(CHART)/app/src/agents/
	cp main.py $(CHART)/app/main.py
	cp requirements.txt $(CHART)/app/requirements.txt

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
