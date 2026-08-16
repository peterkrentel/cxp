#!/usr/bin/env bash
# One-shot snapshot of swarm health, consolidating the ad-hoc kubectl/nats
# investigation this project has repeatedly needed by hand (halt state,
# stuck agents, the dead cxp.cap.any subject, pod resource pressure,
# JetStream backlog, recent Warning events). Run manually, or as the check
# a /loop health-watch invokes periodically.
set -uo pipefail

NAMESPACE="${CXP_NAMESPACE:-cxp}"

echo "=== Halt state + agent states ==="
kubectl exec -n "$NAMESPACE" deploy/cxp-web -- python3 -c "
import httpx
r = httpx.get('http://localhost:8080/api/state')
d = r.json()
print('halt:', d.get('halt'))
print('agents:', {k: v.get('state') for k, v in d.get('agents', {}).items()})
print('stats:', d.get('stats'))
" 2>&1 | grep -v "^Defaulted"

echo
echo "=== Pods not Running ==="
kubectl get pods -n "$NAMESPACE" 2>&1 | awk 'NR==1 || $3!="Running"'

echo
echo "=== Pod resource usage (requires metrics-server) ==="
kubectl top pods -n "$NAMESPACE" 2>&1 || echo "(metrics unavailable — is metrics-server installed and Ready?)"

echo
echo "=== JetStream per-subject message counts — cxp.cap.any should stay at whatever it was last check, never grow ==="
kubectl exec -n "$NAMESPACE" deploy/cxp-nats-box -- nats --server nats://cxp-nats:4222 \
  req '$JS.API.STREAM.INFO.CXP_PACKETS' '{"subjects_filter":">"}' 2>&1 \
  | grep -oE '"cxp\.cap\.[^"]+": *[0-9]+' || echo "(nats-box unreachable)"

echo
echo "=== JetStream consumer backlog — Unprocessed should stay near 0 ==="
kubectl exec -n "$NAMESPACE" deploy/cxp-nats-box -- nats --server nats://cxp-nats:4222 \
  consumer report CXP_PACKETS 2>&1

echo
echo "=== CronJob + most recent test-runner jobs ==="
kubectl get cronjob -n "$NAMESPACE" 2>&1
kubectl get jobs -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp 2>&1 | tail -3

echo
echo "=== Recent Warning events ==="
kubectl get events -n "$NAMESPACE" --field-selector type=Warning --sort-by=.lastTimestamp 2>&1 | tail -10
