# Git-Pull Deploy Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Status: design/backlog, not approved for execution.** Depends on the Oracle Cloud VM existing first (see [`2026-08-16-oracle-cloud-migration.md`](2026-08-16-oracle-cloud-migration.md), Phase 1 — not yet started as of this writing). Task 1 below (the sync-deploy script itself) has no such dependency and is safe to build and test against the local kind cluster right away; Task 2 (wiring it to run periodically) needs the VM. Do not start either without a fresh go-ahead — this is captured so the design doesn't need re-deriving when that go-ahead happens.

**Goal:** Keep a deployed cluster's running state in sync with `main` automatically, without adopting Flux/ArgoCD — a plain polling script that pulls the repo and redeploys only when `main` actually changed.

**Architecture:** A single idempotent shell script (`scripts/git-sync-deploy.sh`) that does `git fetch` + fast-forward-only pull, compares the commit hash before and after, and runs `make deploy` only if it changed — skipping a redundant `helm upgrade` on every poll when nothing's new. A systemd timer (preferred over cron on a systemd-based Ubuntu VM — better logging via `journalctl`, easier to inspect with `systemctl status`) runs it every 5 minutes. This deliberately does **not** attempt continuous drift correction against the live cluster (that's Flux's job, explicitly not wanted here per the 2026-08-16 ROADMAP decision) — it only reacts to git changing, and leaves manual `kubectl` pokes between polls alone.

**Tech Stack:** bash, `git`, the existing `Makefile`'s `deploy`/`sync` targets (no new deploy mechanism — this only decides *when* to call them), systemd timer + service units (Ubuntu, matches the Oracle Cloud VM's planned OS).

## Global Constraints

- Never force-push or reset the deploy target's local clone — fast-forward-only (`git pull --ff-only`), so a diverged local clone fails loudly instead of silently discarding something.
- Do not attempt to reconcile *live cluster drift* — this trigger only reacts to git changes, on purpose (see Architecture). Building drift correction would be reinventing Flux; if that's ever wanted, adopt Flux itself rather than growing this script into it.
- Do not run this against the *local* laptop kind cluster as a background always-on service — it's designed for the Oracle Cloud VM once that exists. Testing the script by hand locally (Task 1's test step) is fine; installing the timer is a VM-only step (Task 2).
- Never add `Co-Authored-By: Claude` to any commit this plan produces.

---

### Task 1: Write and test the sync-deploy script

**Files:**
- Create: `scripts/git-sync-deploy.sh`
- Test: manual (see Step 3 below) — this is an ops script, not application code; there's no existing pytest harness for scripts in this repo (see `tests/STRATEGY.md`), so validation here is a real dry run against the actual local kind cluster rather than a mocked unit test.

**Interfaces:**
- Consumes: `make deploy` (existing Makefile target — `helm dependency update` + `helm upgrade --install`), `make sync` (existing target, already invoked by `make deploy` — this script does not call it directly).
- Produces: exit code 0 on "up to date, nothing to do" or "successfully redeployed"; non-zero on any git or deploy failure, so a systemd timer (Task 2) can surface failures via `systemctl status`/`journalctl` without extra plumbing.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/git-sync-deploy.sh
# Poll-based git-to-cluster sync: pulls main, redeploys only if it changed.
# Intended to run on a timer (see Task 2) on the deploy host, not the
# developer's laptop.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "$REPO_DIR"

before="$(git rev-parse HEAD)"

git fetch origin "$BRANCH" --quiet
# --ff-only: a diverged local clone (e.g. someone committed directly on the
# deploy host by mistake) fails loudly here rather than being silently
# discarded by a --hard reset.
git checkout "$BRANCH" --quiet
git merge --ff-only "origin/$BRANCH"

after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "$(date -u +%FT%TZ) up to date at ${after:0:8}, nothing to deploy"
    exit 0
fi

echo "$(date -u +%FT%TZ) ${before:0:8} -> ${after:0:8}, deploying"
make deploy
echo "$(date -u +%FT%TZ) deploy complete at ${after:0:8}"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/git-sync-deploy.sh
```

- [ ] **Step 3: Dry-run it against the local kind cluster**

Run: `REPO_DIR=$(pwd) ./scripts/git-sync-deploy.sh`
Expected: since local `HEAD` already matches `origin/main` (nothing new to pull), it prints `up to date at <hash>, nothing to deploy` and exits 0 — confirms the script's git logic and path resolution work before ever running it unattended. To confirm the "changed" branch actually redeploys, make a trivial commit on a throwaway branch, merge it locally without pushing, run the script (it will report `nothing to deploy` since `origin/main` hasn't moved — this is expected and correct; a full redeploy test requires an actual push to `origin/main`, which is out of scope for a dry run and better verified the first time this runs for real on the VM).

- [ ] **Step 4: Commit**

```bash
git add scripts/git-sync-deploy.sh
git commit -m "feat: add git-pull-triggered deploy sync script"
```

---

### Task 2: Wire the script to a systemd timer on the deploy host

**Files:**
- Create: `deploy/systemd/cxp-sync.service`
- Create: `deploy/systemd/cxp-sync.timer`
- Modify: [`2026-08-16-oracle-cloud-migration.md`](2026-08-16-oracle-cloud-migration.md) Phase 2 — add a step referencing this once the VM exists.

**Interfaces:**
- Consumes: `scripts/git-sync-deploy.sh` (Task 1) — invoked as-is, no arguments.
- Produces: nothing consumed downstream — this task's deliverable is the running timer itself, verified via `systemctl status cxp-sync.timer` and `journalctl -u cxp-sync.service`.

- [ ] **Step 1: Write the service unit**

```ini
# deploy/systemd/cxp-sync.service
[Unit]
Description=CXP git-pull deploy sync
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/ubuntu/cxp
ExecStart=/home/ubuntu/cxp/scripts/git-sync-deploy.sh
User=ubuntu
```

- [ ] **Step 2: Write the timer unit**

```ini
# deploy/systemd/cxp-sync.timer
[Unit]
Description=Run cxp-sync.service every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Install and enable on the VM (manual — not scripted, since it's a one-time host setup step)**

Run on the VM, once it exists:
```bash
sudo cp deploy/systemd/cxp-sync.service deploy/systemd/cxp-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cxp-sync.timer
```

- [ ] **Step 4: Verify**

Run: `systemctl status cxp-sync.timer` — expect `active (waiting)` with a `Trigger:` time within the next 5 minutes.
Run: `journalctl -u cxp-sync.service -n 20` — expect either `up to date` or a successful `deploy complete` line from the most recent run.

- [ ] **Step 5: Commit**

```bash
git add deploy/systemd/cxp-sync.service deploy/systemd/cxp-sync.timer
git commit -m "feat: add systemd timer to run git-sync-deploy every 5 minutes"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers the sync-deploy script and its own dry-run validation; Task 2 covers the periodic trigger mechanism (systemd timer, chosen over cron for better `journalctl` observability on the Ubuntu VM this targets) and its host-install step.
- **Placeholder scan:** the VM install step (Task 2, Step 3) is manual by necessity (it's a one-time action on a host that doesn't exist yet) but every command is concrete, not a placeholder.
- **Type consistency:** n/a — this is bash/systemd config, not typed application code.
