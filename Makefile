NAMESPACE  = cxp
RELEASE    = cxp
CHART      = helm/cxp

.PHONY: deploy upgrade destroy submit dashboard logs

# First install or idempotent upgrade — auto-syncs src into Helm chart first
deploy: sync
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace
	@echo "Waiting for web pod..."
	kubectl rollout status deployment/cxp-web -n $(NAMESPACE) --timeout=120s
	@pkill -f "port-forward.*cxp-web" 2>/dev/null || true
	kubectl port-forward -n $(NAMESPACE) svc/cxp-web 8080:8080 --address 0.0.0.0 &
	@echo "✓ Web dashboard: http://localhost:8080"

# Sync src files into Helm chart so ConfigMaps pick up latest code
sync:
	cp src/*.py $(CHART)/app/src/
	cp src/agents/*.py $(CHART)/app/src/agents/
	cp main.py $(CHART)/app/main.py
	cp requirements.txt $(CHART)/app/requirements.txt

# Alias
upgrade: deploy

# Destroy everything EXCEPT the memory PVC (annotated keep)
destroy:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE) || true

# Hard reset: wipe PVC too, then redeploy fresh
reset:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE) || true
	kubectl delete pvc cxp-memory -n $(NAMESPACE) || true
	sleep 3
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace

# Submit a task
submit:
	kubectl exec -n $(NAMESPACE) deploy/cxp-dashboard -- python /app/main.py submit "$(GOAL)"

# Open web dashboard in browser (port-forward)
web:
	kubectl port-forward -n $(NAMESPACE) svc/cxp-web 8080:8080

# Live terminal dashboard
dashboard:
	kubectl exec -it -n $(NAMESPACE) deploy/cxp-dashboard -- python /app/main.py dashboard

# Tail all agent logs
logs:
	kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/name=cxp --prefix --follow
