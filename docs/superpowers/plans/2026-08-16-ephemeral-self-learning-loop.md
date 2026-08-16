# Ephemeral Self-Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Status: design/backlog, not approved for execution.** This idea was validated as architecturally sound during a 2026-08-16 discussion but the user was explicit it is **not to be built yet** — "ill circle back to do that with you." Do not start Task 1 without a fresh, explicit go-ahead in a future session. Tasks below are written execution-ready so that when that go-ahead happens, work can start immediately instead of re-deriving the design.

**Goal:** Add a second, independent improvement loop — an ephemeral GitHub-Actions-based pipeline that spins up its own kind cluster, runs the test suite against the swarm, and git-checkpoints both results and skill revisions — running alongside (not replacing) today's live/interactive kind cluster, with a human-gated promotion step to move a validated skill revision from the ephemeral loop into the live cluster.

**Architecture:** Two loops sharing one source of truth (git), not one shared cluster. **Loop A (existing, unchanged):** the live kind cluster on the user's laptop (or eventually the Oracle Cloud VM per [`2026-08-16-oracle-cloud-migration.md`](2026-08-16-oracle-cloud-migration.md)) — real-time NATS/JetStream KV for skills and halt state, human-facing web dashboard, hourly CronJob test-runner pushing results to the `bot/test-results` git branch. **Loop B (new):** a GitHub Actions workflow that deploys a throwaway kind cluster from the same Helm chart, pulls Ollama models in-runner (confirmed workable — user has done this before), runs the existing test suite against it, and instead of writing skill revisions to a live JetStream KV (which wouldn't survive the runner's teardown anyway), commits both the test results *and* the resulting skill revision text to a new git-tracked directory. A separate promotion step — human-approved for now — diffs an ephemeral-loop skill revision against the live cluster's current KV entry and, if accepted, writes it into Loop A's `cxp-skills` KV. Git is the only thing both loops share; neither loop talks to the other's cluster directly.

**Tech Stack:** GitHub Actions, `helm/kind-action` (already in use for [`ci.yml`](../../../.github/workflows/ci.yml)), Ollama (pulled in-runner, no new hosting), existing Helm chart (`helm/cxp`), existing NATS/JetStream KV client code in `src/agent_shell.py`, git (new directory under version control, no new storage system).

## Global Constraints

- Do not begin implementation without explicit user go-ahead in a future session — this plan documents the design, it does not authorize building it.
- Never trigger ad-hoc/manual test runs against the **live** cluster to validate this work — the live cluster's test cadence stays cron-only, per standing instruction.
- Do not change the Ollama model (`qwen2.5:1.5b` / `qwen2.5:0.5b`) as part of this work — deferred separately, unrelated to this plan.
- Never add `Co-Authored-By: Claude` to any commit this plan produces.
- The ephemeral loop must not be able to write directly to the live cluster's KV or NATS endpoint — promotion is git-mediated and human-gated (Task 4), not a live network call from a GitHub-hosted runner into the user's cluster.
- Reuse the existing Helm chart and agent code as-is for Loop B's cluster — the whole premise is "same swarm, different place it runs," not a fork.

---

### Task 1: Add a git-tracked skill-revision directory that both loops can write to

**Files:**
- Create: `skills/README.md`
- Create: `skills/.gitkeep`
- Modify: `src/agent_shell.py` (the `put_skill`/`get_skill_with_revision` KV helpers — see current implementation for exact method names before touching)
- Test: `tests/test_skill_export.py`

**Interfaces:**
- Consumes: `AgentShell._kv()`, `AgentShell.get_skill_with_revision(capability: str) -> tuple[str, int]` (existing methods — read `src/agent_shell.py` to confirm exact signatures before writing calls against them, since this summary is reconstructed from a prior session and the file may have moved on).
- Produces: `export_skill_to_git(capability: str, text: str, revision: int, source: str) -> pathlib.Path` — writes `skills/<capability>/<revision>-<source>.txt` (source is `"live"` or `"ephemeral"`) and returns the path written, so Task 3 and Task 4 can call it identically regardless of which loop is running.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_export.py
import pathlib
from src.agent_shell import export_skill_to_git

def test_export_skill_to_git_writes_expected_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    result = export_skill_to_git("executor", "improved skill text", 7, "ephemeral")
    expected = tmp_path / "skills" / "executor" / "7-ephemeral.txt"
    assert result == expected
    assert expected.read_text() == "improved skill text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_export.py -v`
Expected: FAIL with `ImportError: cannot import name 'export_skill_to_git'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/agent_shell.py` (module-level function, not a method — it has no need of `self` or a live NATS connection):

```python
import pathlib

def export_skill_to_git(capability: str, text: str, revision: int, source: str) -> pathlib.Path:
    target_dir = pathlib.Path("skills") / capability
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{revision}-{source}.txt"
    target_path.write_text(text)
    return target_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_skill_export.py -v`
Expected: PASS

- [ ] **Step 5: Copy the change into the Helm-packaged duplicate**

This repo keeps `src/` and `helm/cxp/app/src/` as manually synced duplicates (confirmed pattern from every prior session change). Run:

```bash
cp src/agent_shell.py helm/cxp/app/src/agent_shell.py
```

- [ ] **Step 6: Write `skills/README.md` explaining the directory**

```markdown
# skills/

Git-checkpointed snapshots of skill text, written by `export_skill_to_git()`
(`src/agent_shell.py`). Filename format: `<capability>/<revision>-<source>.txt`,
where `source` is `live` (exported from the interactive kind cluster's KV) or
`ephemeral` (produced by a GitHub Actions self-learning run — see
`docs/superpowers/plans/2026-08-16-ephemeral-self-learning-loop.md`).

This directory is the diffable history the live KV store doesn't give you —
`git log -p skills/executor/` shows every accepted skill revision over time.
It is not itself consumed at runtime; the live cluster's KV (`cxp-skills`)
is still the source of truth for what agents actually run with. Promotion
from an ephemeral revision into the live KV is a manual step (see the plan
doc) — nothing here auto-applies.
```

- [ ] **Step 7: Commit**

```bash
git add src/agent_shell.py helm/cxp/app/src/agent_shell.py skills/README.md skills/.gitkeep tests/test_skill_export.py
git commit -m "feat: add git-checkpointed skill revision export"
```

---

### Task 2: Wire live-loop skill export into `reflect.py`

**Files:**
- Modify: `src/agents/reflect.py` (after the existing `put_skill` call that writes a new revision to KV — locate it by searching for `SKILL_TARGET`)
- Modify: `helm/cxp/app/src/agents/reflect.py` (synced duplicate)
- Test: `tests/test_reflect_git_export.py`

**Interfaces:**
- Consumes: `export_skill_to_git(capability: str, text: str, revision: int, source: str) -> pathlib.Path` (Task 1).
- Produces: nothing new consumed downstream — this task's deliverable is the side effect (a git-tracked file appears whenever `reflect` writes a new live skill revision), independently verifiable by the test below.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reflect_git_export.py
import pathlib
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_reflect_exports_new_revision_to_git(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    from src.agents.reflect import ReflectAgent

    agent = ReflectAgent.__new__(ReflectAgent)  # bypass __init__, avoids needing a live NATS connection
    with patch.object(agent, "put_skill", new=AsyncMock(return_value=5)):
        await agent._export_new_revision("executor", "new skill text", 5)

    expected = tmp_path / "skills" / "executor" / "5-live.txt"
    assert expected.read_text() == "new skill text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reflect_git_export.py -v`
Expected: FAIL with `AttributeError: 'ReflectAgent' object has no attribute '_export_new_revision'`

- [ ] **Step 3: Write minimal implementation**

Add a method to `ReflectAgent` in `src/agents/reflect.py`, and call it right after the existing KV `put_skill` call in whatever method currently performs the reflect-and-rewrite step:

```python
from src.agent_shell import export_skill_to_git

class ReflectAgent(AgentShell):
    # ... existing methods unchanged ...

    async def _export_new_revision(self, capability: str, text: str, revision: int) -> None:
        export_skill_to_git(capability, text, revision, source="live")
```

Then, at the existing call site where `reflect.py` currently does something like `revision = await self.put_skill(SKILL_TARGET, new_skill_text)`, add immediately after it:

```python
await self._export_new_revision(SKILL_TARGET, new_skill_text, revision)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reflect_git_export.py -v`
Expected: PASS

- [ ] **Step 5: Copy the change into the Helm-packaged duplicate**

```bash
cp src/agents/reflect.py helm/cxp/app/src/agents/reflect.py
```

- [ ] **Step 6: Decide and implement how `skills/` gets committed from the live cluster**

The live cluster's pods have no git remote credentials by default except the test-runner Job (`cxp-git-creds`, used today to push to `bot/test-results`). Reuse that: extend the existing test-runner git-push step in `helm/cxp/templates/test-runner.yaml` to also `git add skills/` before its existing commit, so live-loop skill exports ride along on the same hourly cron push that already commits test results — no new credential or push path needed.

Locate the existing commit step in `helm/cxp/templates/test-runner.yaml` (search for the `git commit` line added when `bot/test-results`-only pushing was implemented) and add `skills/` to its existing `git add` invocation.

- [ ] **Step 7: Commit**

```bash
git add src/agents/reflect.py helm/cxp/app/src/agents/reflect.py helm/cxp/templates/test-runner.yaml tests/test_reflect_git_export.py
git commit -m "feat: export live skill revisions to git on every reflect pass"
```

---

### Task 3: Ephemeral GitHub Actions self-learning workflow

**Files:**
- Create: `.github/workflows/self-learning.yml`
- Modify: `helm/cxp/templates/test-runner.yaml` (confirm the ephemeral run can reuse the same Job/script with an env-var toggle rather than forking the logic — see Step 3 below)

**Interfaces:**
- Consumes: `export_skill_to_git` (Task 1) — invoked identically to the live loop, just with `source="ephemeral"`, from within the ephemeral cluster's own `reflect` pod.
- Produces: a new file per run at `skills/<capability>/<revision>-ephemeral.txt`, committed by this workflow directly to a new dedicated branch `bot/ephemeral-learning` (mirrors the existing `bot/test-results` pattern — never pushes to `main`, consistent with the standing git workflow rule established for the live loop's bot).

- [ ] **Step 1: Confirm the reflect-export path works identically when `SKILL_TARGET`'s KV writes happen against an ephemeral, throwaway JetStream instance**

No code change — this is a design check. `export_skill_to_git()` (Task 1) takes plain arguments and writes to a relative `skills/` path; it has no dependency on which NATS instance the revision came from. The only thing that differs between Loop A and Loop B is the `source` argument, which `reflect.py`'s call site should read from an environment variable rather than hardcoding `"live"`:

```python
import os

async def _export_new_revision(self, capability: str, text: str, revision: int) -> None:
    export_skill_to_git(capability, text, revision, source=os.environ.get("CXP_LOOP_SOURCE", "live"))
```

- [ ] **Step 2: Update `_export_new_revision`'s hardcoded `"live"` to read `CXP_LOOP_SOURCE`**

Modify the method added in Task 2, Step 3, in both `src/agents/reflect.py` and `helm/cxp/app/src/agents/reflect.py`, to the version shown in Step 1 above.

- [ ] **Step 3: Set `CXP_LOOP_SOURCE=live` explicitly in the live Helm deployment**

In `helm/cxp/templates/agents.yaml`, find the `reflect` container's `env:` block and add:

```yaml
            - name: CXP_LOOP_SOURCE
              value: "live"
```

This keeps the live cluster's default behavior identical to today even though the code now reads an env var — the ephemeral workflow (Step 4) is what actually sets it to `"ephemeral"`.

- [ ] **Step 4: Write the GitHub Actions workflow**

```yaml
# .github/workflows/self-learning.yml
name: Ephemeral self-learning run

# Deliberately NOT on pull_request/push — this must never block or run on
# every commit. Manual dispatch only until the design has been exercised
# a few times and a real cadence decision gets made (see Open Questions).
on:
  workflow_dispatch: {}

jobs:
  ephemeral-loop:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4

      - name: Set up kind
        uses: helm/kind-action@v1
        with:
          cluster_name: cxp-ephemeral
          wait: 120s

      - name: Deploy the chart with CXP_LOOP_SOURCE=ephemeral
        run: |
          kubectl create namespace cxp
          helm install cxp helm/cxp --namespace cxp \
            --set agents.reflect.env.CXP_LOOP_SOURCE=ephemeral

      - name: Wait for pods
        run: kubectl wait --for=condition=Ready pods --all -n cxp --timeout=600s

      - name: Pull Ollama models into the ephemeral cluster
        run: |
          kubectl exec -n cxp deploy/ollama -- ollama pull qwen2.5:1.5b
          kubectl exec -n cxp deploy/ollama-small -- ollama pull qwen2.5:0.5b

      - name: Run the test suite against the ephemeral cluster
        run: kubectl exec -n cxp deploy/test-runner-manual -- python tests/run_tests.py

      - name: Copy results and skill exports out of the cluster
        run: |
          kubectl cp cxp/$(kubectl get pod -n cxp -l app=test-runner -o name | head -1):/app/tests/results ./tests/results
          kubectl cp cxp/$(kubectl get pod -n cxp -l app=reflect -o name | head -1):/app/skills ./skills

      - name: Commit to bot/ephemeral-learning
        run: |
          git config user.name "cxp-ephemeral-bot"
          git config user.email "cxp-ephemeral-bot@users.noreply.github.com"
          git checkout -B bot/ephemeral-learning
          git add tests/results skills
          git commit -m "ephemeral: self-learning run $(date -u +%Y-%m-%dT%H:%M:%SZ)" || echo "nothing to commit"
          git push origin bot/ephemeral-learning --force-with-lease
```

Note: this step list assumes `kubectl cp` can reach the pods' filesystem paths for `tests/results` and `skills` — confirm the actual container mount paths against the live `test-runner.yaml` and agent Dockerfiles before running this for the first time; the paths above are placeholders based on the repo's existing directory layout and may need adjusting.

- [ ] **Step 5: Commit the workflow file itself**

```bash
git add .github/workflows/self-learning.yml src/agents/reflect.py helm/cxp/app/src/agents/reflect.py helm/cxp/templates/agents.yaml
git commit -m "feat: add manually-triggered ephemeral self-learning workflow"
```

---

### Task 4: Human-gated promotion from ephemeral to live

**Files:**
- Create: `scripts/promote_skill.py`
- Test: `tests/test_promote_skill.py`

**Interfaces:**
- Consumes: files under `skills/<capability>/<revision>-ephemeral.txt` (Task 3's output, once merged/fetched from `bot/ephemeral-learning`); `AgentShell.put_skill(capability: str, text: str) -> int` (existing live-loop KV write method — confirm exact signature in `src/agent_shell.py` before calling).
- Produces: a CLI entry point `python scripts/promote_skill.py <capability> <ephemeral-revision-file>` that a human runs manually against the **live** cluster (via the same kind of one-off `kubectl exec` or local NATS connection the rest of this codebase already uses) to push an ephemeral-loop skill into the live KV. No automatic promotion — this is intentionally a manual step.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_promote_skill.py
from unittest.mock import AsyncMock, patch
import pytest
from scripts.promote_skill import promote

@pytest.mark.asyncio
async def test_promote_reads_file_and_calls_put_skill(tmp_path):
    skill_file = tmp_path / "12-ephemeral.txt"
    skill_file.write_text("candidate skill text")

    mock_shell = AsyncMock()
    mock_shell.put_skill = AsyncMock(return_value=13)

    with patch("scripts.promote_skill.build_agent_shell", new=AsyncMock(return_value=mock_shell)):
        new_revision = await promote("executor", str(skill_file))

    mock_shell.put_skill.assert_awaited_once_with("executor", "candidate skill text")
    assert new_revision == 13
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_promote_skill.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.promote_skill'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/promote_skill.py
"""Manually promote a git-checkpointed ephemeral skill revision into the live cluster's KV."""
import asyncio
import pathlib
import sys

from src.agent_shell import AgentShell


async def build_agent_shell() -> AgentShell:
    shell = AgentShell(name="promote-skill-cli", capability="none")
    await shell.connect()
    return shell


async def promote(capability: str, skill_file_path: str) -> int:
    text = pathlib.Path(skill_file_path).read_text()
    shell = await build_agent_shell()
    return await shell.put_skill(capability, text)


if __name__ == "__main__":
    capability, skill_file_path = sys.argv[1], sys.argv[2]
    new_revision = asyncio.run(promote(capability, skill_file_path))
    print(f"Promoted {skill_file_path} to {capability} KV revision {new_revision}")
```

Before wiring this up for real, confirm `AgentShell.__init__`'s actual required constructor arguments and its `connect()` method name in `src/agent_shell.py` — reconstructed here from the class's general shape, not copied from a fresh read of the file at plan-writing time.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_promote_skill.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/promote_skill.py tests/test_promote_skill.py
git commit -m "feat: add manual promote-skill CLI for ephemeral-to-live gate"
```

---

## Addendum (2026-08-16): a second resolution tier is missing, not just a second loop

A `diagnostician` agent has since been built and merged directly against the live cluster (Loop A) — on every halt, it investigates via `kubectl top`/Ollama's `/api/ps` and, for a narrow class of plain network/LLM timeouts, auto-clears the halt without a human. That's real, working self-resolution — but only for *infra noise*. While validating it live, a real bug surfaced in the diagnostician's own code (its LLM call could itself time out and cascade into a second halt), and the thing that diagnosed and fixed *that* bug was Claude — the swarm had no mechanism to fix its own code, only to shrug off transient timeouts.

That's the actual gap this plan should account for going forward: two distinct resolution tiers, not one.

- **Tier 1 (built, live in the cluster today):** the `diagnostician` agent — resolves pure infra noise (timeouts) in place, no code change, no human, no PR.
- **Tier 2 (not built — the natural extension of this plan's Loop B):** a real coding-agent pass — not the small local Ollama models, which is the whole reason Loop B calls for git-checkpointed, human-reviewed cycles rather than live auto-apply — that takes an *actual code defect* the diagnostician correctly declined to auto-resolve (e.g. the planner's uncaught `JSONDecodeError`, found the same day, that unlike verifier's has no fallback), and proposes a real fix via the same branch → PR → CI pattern this project already uses for every other change, running inside Loop B's ephemeral pipeline rather than live against Loop A.

Explicitly not scoped further or approved for execution — captured here so Task 3's design doesn't get re-derived without this context once this plan is picked back up.

## Open Questions (unresolved by this plan — decide before Task 3 execution)

- **Trigger cadence for `self-learning.yml`.** Drafted as `workflow_dispatch`-only (manual) above, deliberately, since the user has an explicit standing rule against ad-hoc *live*-cluster test runs — but that rule was about the live loop specifically, and whether it should also bound how often the *ephemeral* loop runs is an open call for the user, not assumed here.
- **`kubectl cp` mount paths in Task 3, Step 4.** Marked as unconfirmed placeholders — needs a real check against current container paths before first run.
- **Diff/review UX for Task 4.** Right now promotion is "human reads the file, runs a CLI command" — no side-by-side score comparison. Worth deciding whether `promote_skill.py` should also print the episodic score delta (ephemeral run's score for that skill revision vs. live's last recorded score) before a human commits to promoting, so the decision isn't just "read the raw skill text and guess."
- **Cost/runtime of a 45-minute GitHub Actions job on every manual trigger** — GitHub-hosted runners are free-tier-limited; worth checking actual usage against plan limits once this runs a few times for real.

## Self-Review Notes

- **Spec coverage:** covers git-checkpointed skill export (Task 1-2), the ephemeral pipeline itself (Task 3), and the promotion gate back into the live loop (Task 4) — the three pieces discussed in the prior session (live loop unchanged, ephemeral loop via GH Actions, git as the shared checkpoint).
- **Placeholder scan:** the `kubectl cp` paths in Task 3 Step 4 and the `AgentShell` constructor signature in Task 4 Step 3 are flagged inline as unconfirmed rather than silently assumed, since this plan was written without a fresh read of every file it touches (several were read in a prior, now-compacted session). Confirm both against the live files before executing those steps.
- **Type consistency:** `export_skill_to_git(capability, text, revision, source)` (Task 1) is called with identical argument order and names in Task 2 and Task 3 — no drift introduced.
