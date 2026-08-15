NAMESPACE  = cxp
RELEASE    = cxp
CHART      = helm/cxp

.PHONY: deploy upgrade destroy submit dashboard logs

# First install or idempotent upgrade
deploy:
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) --create-namespace

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

# Live terminal dashboard
dashboard:
	kubectl exec -it -n $(NAMESPACE) deploy/cxp-dashboard -- python /app/main.py dashboard

# Tail all agent logs
logs:
	kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/name=cxp --prefix --follow
