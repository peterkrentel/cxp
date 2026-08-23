#!/usr/bin/env python3
"""Deletes Deployments in cxp-sandbox that never became healthy, and
deployments that DID become healthy once they've had long enough to be
observed -- cxp-sandbox is meant to be ephemeral proof that an artifact
worked, not a place for the swarm's own test deployments to live forever.

deployer (src/agents/deployer.py) reports success as soon as `kubectl
apply` is accepted -- it never checks whether the pods it creates
actually come up. Since the LLM invents plausible-looking but nonexistent
image references, most deploys end up permanently stuck in
ImagePullBackOff and nothing ever cleaned them up before this: one sat
for 14+ hours, found live 2026-08-17 while looking at the cluster.

Only removes a never-healthy Deployment once it's had real time to become
healthy (MIN_AGE_SECONDS) and still has zero ready replicas. A Deployment
that did become healthy gets a much longer window (MAX_HEALTHY_AGE_SECONDS)
before it's reaped too -- confirmed live 2026-08-23: several test
deployments (hello-world, my-app, api-deployment) were still running 2-3
days later, since nothing had ever reaped a *successful* deploy before.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

NAMESPACE = "cxp-sandbox"
MIN_AGE_SECONDS = 900  # 15 min -- generous margin past any real image pull
MAX_HEALTHY_AGE_SECONDS = 3600  # 1 hour -- long enough to observe a working
                                # deploy; sandbox isn't meant to host it forever


def _kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=30)


def _decide(name: str, age_seconds: float, ready: int) -> tuple[bool, str]:
    """Should this Deployment be deleted, and why? Pure decision logic,
    kept separate from the kubectl I/O below so it's testable directly."""
    if ready:
        if age_seconds >= MAX_HEALTHY_AGE_SECONDS:
            return True, f"{int(age_seconds)}s old, healthy but past max sandbox lifetime ({MAX_HEALTHY_AGE_SECONDS}s)"
        return False, f"{ready} ready replica(s), {int(age_seconds)}s old"
    if age_seconds < MIN_AGE_SECONDS:
        return False, f"only {int(age_seconds)}s old, still within the grace period"
    return True, f"{int(age_seconds)}s old, never became ready"


def main() -> int:
    result = _kubectl("get", "deployments", "-n", NAMESPACE, "-o", "json")
    if result.returncode != 0:
        print(f"could not list deployments in {NAMESPACE}: {result.stderr.strip()}", file=sys.stderr)
        return 1

    deployments = json.loads(result.stdout).get("items", [])
    now = datetime.now(timezone.utc)
    removed = 0

    for dep in deployments:
        name = dep["metadata"]["name"]
        created = datetime.fromisoformat(dep["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
        age_seconds = (now - created).total_seconds()
        ready = dep.get("status", {}).get("readyReplicas", 0)

        should_delete, reason = _decide(name, age_seconds, ready)
        if not should_delete:
            print(f"keep {name}: {reason}")
            continue

        delete_result = _kubectl("delete", "deployment", name, "-n", NAMESPACE, "--ignore-not-found")
        if delete_result.returncode == 0:
            print(f"removed {name}: {reason}")
            removed += 1
        else:
            print(f"FAILED to remove {name}: {delete_result.stderr.strip()}", file=sys.stderr)

    print(f"sandbox reaper done: {removed} of {len(deployments)} deployment(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
