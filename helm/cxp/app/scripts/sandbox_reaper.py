#!/usr/bin/env python3
"""Deletes Deployments in cxp-sandbox that never became healthy.

deployer (src/agents/deployer.py) reports success as soon as `kubectl
apply` is accepted -- it never checks whether the pods it creates
actually come up. Since the LLM invents plausible-looking but nonexistent
image references, most deploys end up permanently stuck in
ImagePullBackOff and nothing ever cleaned them up before this: one sat
for 14+ hours, found live 2026-08-17 while looking at the cluster.

Only removes a Deployment once it's had real time to become healthy
(MIN_AGE_SECONDS) and still has zero ready replicas -- a Deployment that
actually worked is never touched, regardless of age.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

NAMESPACE = "cxp-sandbox"
MIN_AGE_SECONDS = 900  # 15 min -- generous margin past any real image pull


def _kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=30)


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

        if ready:
            print(f"keep {name}: {ready} ready replica(s)")
            continue
        if age_seconds < MIN_AGE_SECONDS:
            print(f"keep {name}: only {int(age_seconds)}s old, still within the grace period")
            continue

        delete_result = _kubectl("delete", "deployment", name, "-n", NAMESPACE, "--ignore-not-found")
        if delete_result.returncode == 0:
            print(f"removed {name}: {int(age_seconds)}s old, never became ready")
            removed += 1
        else:
            print(f"FAILED to remove {name}: {delete_result.stderr.strip()}", file=sys.stderr)

    print(f"sandbox reaper done: {removed} of {len(deployments)} deployment(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
