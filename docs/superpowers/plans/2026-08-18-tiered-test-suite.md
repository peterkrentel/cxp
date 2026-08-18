# Tiered E2E Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure `tests/run_tests.py` so the E2E suite starts at a genuinely minimal difficulty per capability and only grows harder once the swarm proves it can reliably clear the current bar — instead of every capability being tested at whatever scope its label naturally implies (which put a full microservice-scaffold goal in the same tier as "add two numbers" on day one).

**Architecture:** Two tiers of the same 8 capability labels (Tier 0: minimal, Tier 1: today's existing goals, now with fixed validators and real timeouts). Each CronJob run determines which tier is currently unlocked by reading the git-tracked run history on `bot/test-results` (now that #31 fixed the push bug, this history actually accumulates), runs only that tier's tests, and tags its result file with the tier that ran. Promotion from Tier 0 to Tier 1 happens automatically once 10 consecutive clean Tier-0 runs are recorded — no manual re-configuration.

**Tech Stack:** Python 3.12 (`tests/run_tests.py`, `tests/check_plateau.py`), no new dependencies.

## Global Constraints

- Every test still goes through the real pipeline (`/api/submit` → plan → code → verify) — no shortcuts that bypass planner, since that's what's actually being validated.
- `STREAK_TARGET = 10` (already established in `check_plateau.py`) stays the promotion bar for both tiers — reuse it, don't invent a different number.
- Real, data-derived timeouts only: 900s for any single-hop-equivalent (~3 pipeline hops) test, derived from live-measured per-hop latency (median 120s, P90 438s across 75 measured transitions, 2026-08-18). Never hardcode a timeout without tracing it back to this math or an equivalent fresh measurement.
- Every validator must independently check something about the artifact or a structural fact (sub-task count, issue keywords) — never let "did the LLM call succeed" alone count as a pass.
- Follow this repo's standing workflow: every change on its own branch, through `make deploy` (not raw `helm upgrade`), CI green, then merge. Never add `Co-Authored-By: Claude` to commits.

---

### Task 1: Fix the three weak/absent validators

**Files:**
- Modify: `tests/run_tests.py:166-178` (`validate_python`), `tests/run_tests.py` (the `SECURITY_AWARENESS`/`STRUCTURED_OUTPUT` test dicts and their validators)
- Test: `tests/test_validators.py` (new — this project has no unit tests for the test-runner's own validators; add them now rather than trust them blind)

**Interfaces:**
- Produces: `validate_python(code: str, require_type_hints: bool = False) -> tuple[bool, list[str]]` — extended signature, backward compatible (existing callers that don't pass `require_type_hints` keep today's behavior).
- Produces: `validate_error_handling(code: str, required_exceptions: list[str]) -> tuple[bool, list[str]]` — new, replaces reusing `validate_python` for `ERROR_HANDLING`.
- Produces: `validate_k8s_deployment(text: str) -> tuple[bool, list[str]]` — new, replaces reusing generic `validate_yaml` for `STRUCTURED_OUTPUT`.
- Produces: `validate_security(code: str) -> tuple[bool, list[str]]` — new, gives `SECURITY_AWARENESS` an actual independent check instead of relying solely on the verifier's own issue-list wording.

- [ ] **Step 1: Write the failing tests for the fixed validators**

```python
# tests/test_validators.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import (
    validate_python, validate_error_handling, validate_k8s_deployment, validate_security,
)


def test_type_hints_check_actually_requires_annotations():
    no_hints = "def add(a, b):\n    return a + b\n"
    valid, issues = validate_python(no_hints, require_type_hints=True)
    assert valid is False
    assert any("type hint" in i.lower() for i in issues)

    with_hints = "def add(a: int, b: int) -> int:\n    return a + b\n"
    valid, issues = validate_python(with_hints, require_type_hints=True)
    assert valid is True


def test_error_handling_requires_the_named_exceptions():
    missing_one = (
        "def load(path):\n"
        "    try:\n"
        "        return open(path).read()\n"
        "    except FileNotFoundError:\n"
        "        return None\n"
    )
    valid, issues = validate_error_handling(missing_one, ["FileNotFoundError", "JSONDecodeError"])
    assert valid is False
    assert any("JSONDecodeError" in i for i in issues)

    both = missing_one.replace(
        "except FileNotFoundError:",
        "except (FileNotFoundError, __import__('json').JSONDecodeError):",
    )
    valid, issues = validate_error_handling(both, ["FileNotFoundError", "JSONDecodeError"])
    assert valid is True


def test_k8s_deployment_requires_kind_and_resource_limits():
    bare_yaml = "foo: bar\n"
    valid, issues = validate_k8s_deployment(bare_yaml)
    assert valid is False

    real_deployment = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            limits: { cpu: "500m", memory: "512Mi" }
"""
    valid, issues = validate_k8s_deployment(real_deployment)
    assert valid is True


def test_security_check_flags_unsanitized_path_from_input():
    unsafe = (
        "import requests\n"
        "def fetch(url):\n"
        "    return requests.get(url).content\n"
    )
    valid, issues = validate_security(unsafe)
    assert valid is False

    safer = (
        "import requests\n"
        "from urllib.parse import urlparse\n"
        "def fetch(url):\n"
        "    parsed = urlparse(url)\n"
        "    if parsed.scheme not in ('http', 'https'):\n"
        "        raise ValueError('invalid scheme')\n"
        "    return requests.get(url, timeout=10).content\n"
    )
    valid, issues = validate_security(safer)
    assert valid is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_validators.py -v`
Expected: `ImportError` (the new functions don't exist yet) or `AttributeError`.

- [ ] **Step 3: Implement the fixed validators**

```python
# tests/run_tests.py -- replace validate_python, add the three new functions

def validate_python(code: str, require_type_hints: bool = False) -> tuple[bool, list[str]]:
    code = _strip_markdown(code)
    issues = []
    try:
        tree = compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    if "def " not in code:
        issues.append("No function definition found")
    if require_type_hints:
        import ast
        parsed = ast.parse(code)
        funcs = [n for n in ast.walk(parsed) if isinstance(n, ast.FunctionDef)]
        annotated = [f for f in funcs if f.returns or any(a.annotation for a in f.args.args)]
        if not funcs or not annotated:
            issues.append("No real parameter/return type hints found (ast-checked, not just ':' in source)")
    return len(issues) == 0, issues


def validate_error_handling(code: str, required_exceptions: list[str]) -> tuple[bool, list[str]]:
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    for exc_name in required_exceptions:
        if exc_name not in code:
            issues.append(f"Never mentions {exc_name} -- not actually handled")
    return len(issues) == 0, issues


def validate_k8s_deployment(text: str) -> tuple[bool, list[str]]:
    try:
        import yaml
        doc = yaml.safe_load(_strip_markdown(text))
    except Exception as e:
        return False, [f"Invalid YAML: {e}"]
    if not isinstance(doc, dict):
        return False, ["YAML did not parse to a mapping"]
    issues = []
    if doc.get("kind") != "Deployment":
        issues.append(f"kind is {doc.get('kind')!r}, expected 'Deployment'")
    flat = str(doc).lower()
    if "resources" not in flat or "limits" not in flat:
        issues.append("No resources.limits found anywhere in the manifest")
    return len(issues) == 0, issues


def validate_security(code: str) -> tuple[bool, list[str]]:
    """Independent check, not dependent on the verifier's own wording:
    does user-controlled input reach a network/filesystem call without
    any visible validation step in between?"""
    code = _strip_markdown(code)
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    issues = []
    has_sink = any(s in code for s in ("requests.get(", "requests.post(", "open(", "urlopen("))
    has_validation = any(s in code for s in (
        "urlparse", "scheme", "raise ValueError", "startswith(", "in allowed", "sanitiz",
    ))
    if has_sink and not has_validation:
        issues.append("Passes input to a network/file call with no visible validation step")
    return len(issues) == 0, issues
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_validators.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Update the TESTS list to use the fixed validators**

```python
# tests/run_tests.py -- in the TESTS list, replace these three entries' "validator" wiring
{
    "label": "CODE_GENERATION",
    "goal": "write a Python function that adds two numbers with type hints and docstring",
    "validator": lambda code: validate_python(code, require_type_hints=True),
    "threshold": 0.75,
},
{
    "label": "ERROR_HANDLING",
    "goal": "write a Python function that reads a JSON file and returns a dict, handling FileNotFoundError and JSONDecodeError",
    "validator": lambda code: validate_error_handling(code, ["FileNotFoundError", "JSONDecodeError"]),
    "threshold": 0.75,
},
{
    "label": "STRUCTURED_OUTPUT",
    "goal": "generate a Kubernetes Deployment manifest for a Node.js API with resource limits",
    "validator": validate_k8s_deployment,
    "threshold": 0.70,
},
{
    "label": "SECURITY_AWARENESS",
    "goal": "generate a Python web scraper that downloads URLs from user input and saves to disk",
    "validator": validate_security,
    "threshold": 0.0,
    # required_issue_keywords stays too -- this becomes a second, independent
    # signal on top of the new real check, not a replacement for it.
    "required_issue_keywords": ["url", "valid", "path", "travers", "rate limit", "sanitiz"],
},
```

- [ ] **Step 6: Commit**

```bash
git add tests/run_tests.py tests/test_validators.py
git commit -m "fix: give CODE_GENERATION/ERROR_HANDLING/STRUCTURED_OUTPUT/SECURITY_AWARENESS real independent checks"
```

---

### Task 2: Add data-derived per-test timeouts

**Files:**
- Modify: `tests/run_tests.py` (every test dict in `TESTS`, plus `SMOKE_TEST`, plus the `wait_for_results` call sites)

**Interfaces:**
- Consumes: nothing new.
- Produces: every test dict now carries its own `"timeout"` key; `wait_for_results` reads it via `test.get("timeout", 900)` instead of the caller hardcoding `240`/`480`.

- [ ] **Step 1: Add `"timeout": 900` to `SMOKE_TEST` and all 8 entries in `TESTS`**

(Manual edit — same value for all of them at this tier, since they're all ~3-hop journeys. `DECOMPOSITION` gets its own tier in Task 4, not a special-cased timeout here.)

- [ ] **Step 2: Update call sites to use the per-test value**

```python
# tests/run_tests.py, in main()
smoke_result = wait_for_results({smoke_task_id: SMOKE_TEST}, timeout=SMOKE_TEST["timeout"]) if smoke_task_id else {}
...
one_result = wait_for_results({task_id: test}, timeout=test["timeout"])
...
retry_result = wait_for_results({retry_id: test}, timeout=test["timeout"])
```

- [ ] **Step 3: Commit**

```bash
git add tests/run_tests.py
git commit -m "fix: derive test timeouts from measured pipeline latency (900s), not a guessed flat 240/480s"
```

---

### Task 3: Define Tier 0 — genuinely minimal goals for all 8 capabilities

**Files:**
- Modify: `tests/run_tests.py` (rename existing `TESTS` to `TIER_1_TESTS`, add new `TIER_0_TESTS`)

**Interfaces:**
- Produces: `TIER_0_TESTS: list[dict]` — same 8 labels as `TIER_1_TESTS`, each with a deliberately smaller goal and (where relevant) a relaxed validator, so Tier 0 is actually clearable.
- Produces: `TIER_1_TESTS` — the existing (Task 1/2-fixed) list, renamed, unchanged in content otherwise.

- [ ] **Step 1: Rename the existing list**

```python
# tests/run_tests.py
TIER_1_TESTS = TESTS  # rename; keep the existing 8 entries as-is (post Task 1/2 fixes)
```

- [ ] **Step 2: Write Tier 0's minimal versions**

```python
TIER_0_TESTS = [
    {
        "label": "CODE_GENERATION",
        "goal": "write a Python function that returns the sum of two numbers",
        "validator": validate_python,  # no require_type_hints yet
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "ERROR_HANDLING",
        "goal": "write a Python function that opens a file and catches FileNotFoundError",
        "validator": lambda code: validate_error_handling(code, ["FileNotFoundError"]),  # one exception, not two
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "STRUCTURED_OUTPUT",
        "goal": "generate a Kubernetes Deployment manifest for a Node.js API",
        "validator": lambda text: (lambda v, i: (v, [x for x in i if "resources" not in x]))(*validate_k8s_deployment(text)),
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "DECOMPOSITION",
        "goal": "write a Python function and a test for it",
        "validator": validate_always,
        "threshold": 0.0,
        "min_subtasks": 2,  # not 3 -- genuinely the smallest meaningful decomposition
        "timeout": 900,
    },
    {
        "label": "SECURITY_AWARENESS",
        "goal": "write a Python function that fetches a URL using the requests library",
        "validator": validate_always,
        "threshold": 0.0,
        "required_issue_keywords": ["url", "valid", "sanitiz"],  # narrower list for a narrower goal
        "timeout": 900,
    },
    {
        "label": "INFRA_AS_CODE",
        "goal": "generate a Helm values.yaml with persistence enabled for a Redis deployment",
        "validator": lambda text: (lambda v, i: (v, [x for x in i if "auth" not in x and "sentinel" not in x]))(*validate_infra_yaml(text)),
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "TESTING",
        "goal": "write a Python function that doubles a number, plus one test for it",
        "validator": validate_has_tests,
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "DOCUMENTATION",
        "goal": "write a Python function that reverses a string, with a docstring",
        "validator": validate_has_docstring,
        "threshold": 0.6,
        "timeout": 900,
    },
]
```

- [ ] **Step 3: Commit**

```bash
git add tests/run_tests.py
git commit -m "feat: add Tier 0 -- genuinely minimal per-capability goals"
```

---

### Task 4: Tier-aware plateau tracking and automatic promotion

**Files:**
- Modify: `tests/check_plateau.py`
- Modify: `tests/run_tests.py` (`main()` — determine tier before submitting anything, tag saved results with it)

**Interfaces:**
- Consumes: `TIER_0_TESTS`, `TIER_1_TESTS` from Task 3.
- Produces: `determine_current_tier(results_dir: str) -> int` — returns `0` or `1`.
- Produces: each saved `tests/results/run_*.json` now includes a top-level `"tier": 0` or `"tier": 1` key.

- [ ] **Step 1: Make `check_plateau.py` tier-aware**

```python
# tests/check_plateau.py
TIER_LABELS = {
    0: {"CODE_GENERATION", "ERROR_HANDLING", "STRUCTURED_OUTPUT", "DECOMPOSITION",
        "SECURITY_AWARENESS", "INFRA_AS_CODE", "TESTING", "DOCUMENTATION"},
    1: {"CODE_GENERATION", "ERROR_HANDLING", "STRUCTURED_OUTPUT", "DECOMPOSITION",
        "SECURITY_AWARENESS", "INFRA_AS_CODE", "TESTING", "DOCUMENTATION"},
}


def is_clean_run(run: dict, tier: int) -> bool | None:
    if run.get("tier") != tier:
        return None  # a run from a different tier isn't evidence for this tier's streak
    by_label = {r["label"]: r for r in run.get("results", [])}
    labels = TIER_LABELS[tier]
    if not labels.issubset(by_label.keys()):
        return None
    return all(by_label[label].get("status") == "PASS" for label in labels)


def streak_for_tier(results_dir: str, tier: int) -> int:
    files = sorted(f for f in os.listdir(results_dir) if f.startswith("run_") and f.endswith(".json"))
    streak = 0
    for fname in reversed(files):
        with open(os.path.join(results_dir, fname)) as f:
            run = json.load(f)
        clean = is_clean_run(run, tier)
        if clean is None:
            continue
        if clean:
            streak += 1
        else:
            break
    return streak


def determine_current_tier(results_dir: str = "tests/results") -> int:
    """Tier 1 unlocks once Tier 0 has hit the streak target; otherwise
    stay on Tier 0. Never skips ahead based on Tier 1 history alone --
    Tier 0 must have actually been cleared first."""
    if not os.path.isdir(results_dir):
        return 0
    if streak_for_tier(results_dir, tier=0) >= STREAK_TARGET:
        return 1
    return 0
```

Replace the existing `main()` (which calls the old single-arg `is_clean_run(run)` and will break once `is_clean_run` takes a `tier` parameter) with a tier-aware version:

```python
# tests/check_plateau.py
def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "tests/results"
    if not os.path.isdir(results_dir):
        print(f"No results directory at {results_dir} — nothing to check yet.")
        return
    files = [f for f in os.listdir(results_dir) if f.startswith("run_") and f.endswith(".json")]
    if not files:
        print("No run history found — nothing to check yet.")
        return

    for tier in (0, 1):
        streak = streak_for_tier(results_dir, tier)
        print(f"Tier {tier}: {streak} / {STREAK_TARGET} consecutive clean runs")

    active = determine_current_tier(results_dir)
    print(f"\nCurrently active: Tier {active}")
    if active == 1 and streak_for_tier(results_dir, 1) >= STREAK_TARGET:
        print("✓ FINISH LINE REACHED on Tier 1 — the fixed suite has plateaued. "
              "Time to consider a harder Tier 2 instead of the CronJob continuing to just confirm the same thing.")


if __name__ == "__main__":
    main()
```

This keeps the CLI invocation (`python3 check_plateau.py [results_dir]`) identical — only the printed output changes, now showing both tiers' streaks and which one is currently active.

- [ ] **Step 2: Wire tier selection into `run_tests.py`'s `main()`**

This requires knowing history *before* submitting any tests — which means fetching `tests/results/` from `bot/test-results` at the start of the run, not just at the end (today it only clones for the push step, after tests already ran). Add an early, read-only clone:

```python
# tests/run_tests.py, near the top of main(), before check_halted()
import subprocess

def _fetch_results_history() -> str | None:
    """Read-only clone of the results history, used only to determine the
    current tier before submitting anything. Separate from the write clone
    at the end of the run (git-creds may be absent locally; that's fine,
    an absent history just means tier 0)."""
    if not (os.path.exists("/git-creds/token") and os.path.exists("/git-creds/repo")):
        return None
    repo = open("/git-creds/repo").read().strip()
    token = open("/git-creds/token").read().strip()
    dest = "/tmp/repo-history"
    subprocess.run(["rm", "-rf", dest])
    result = subprocess.run(
        ["git", "clone", "-q", "--depth=50", "--branch", "bot/test-results",
         f"https://x-access-token:{token}@github.com/{repo}.git", dest],
        capture_output=True, text=True,
    )
    return f"{dest}/tests/results" if result.returncode == 0 else None
```

```python
# tests/run_tests.py, in main(), before the smoke test section
from tests.check_plateau import determine_current_tier

results_history_dir = _fetch_results_history()
current_tier = determine_current_tier(results_history_dir) if results_history_dir else 0
active_tests = TIER_0_TESTS if current_tier == 0 else TIER_1_TESTS
print(f"\n[TIER] Running Tier {current_tier} ({len(active_tests)} capability tests)", flush=True)
```

Replace the later `shuffled = random.sample(TESTS, len(TESTS))` with `shuffled = random.sample(active_tests, len(active_tests))`.

- [ ] **Step 3: Tag saved results with the tier that ran**

```python
# tests/run_tests.py, wherever the results dict is built before json.dump
output = {
    "tier": current_tier,
    "results": results,
    ...
}
```

- [ ] **Step 4: Commit**

```bash
git add tests/run_tests.py tests/check_plateau.py
git commit -m "feat: tier-aware plateau tracking with automatic Tier 0 -> Tier 1 promotion"
```

---

### Task 5: Deploy and verify end to end

**Files:** none (verification only)

- [ ] **Step 1: Deploy**

```bash
make deploy
```

- [ ] **Step 2: Manually trigger a run and confirm it selects Tier 0**

```bash
kubectl create job -n cxp cxp-test-runner-tier-check --from=cronjob/cxp-test-runner
kubectl logs -n cxp job/cxp-test-runner-tier-check -f
```

Expected: log line `[TIER] Running Tier 0 (8 capability tests)` before any test is submitted, and (given the ~900s timeouts and the swarm's current healthy state) at least several of the 8 Tier-0 tests should now register PASS rather than TIMEOUT — this is the actual proof the restructuring worked, not just that it deployed.

- [ ] **Step 3: Confirm results actually persist this time**

```bash
git fetch origin bot/test-results
git log origin/bot/test-results --oneline -3
```

Expected: a new commit from this run, containing a `tier: 0`-tagged result file — confirming both this plan's fix and #31's git-push fix are working together.

- [ ] **Step 4: Clean up the manual test job**

```bash
kubectl delete job -n cxp cxp-test-runner-tier-check
```

---

## Self-Review Notes

- **Spec coverage**: all three problems raised tonight are addressed — weak/absent validators (Task 1), mismatched flat timeouts (Task 2), and tests sized without regard to progression (Tasks 3-4).
- **`SECURITY_AWARENESS` stays partially LLM-judged on purpose**: `validate_security`'s regex-ish check catches the crudest failures (no validation at all), but real security review still needs judgment a static check can't fully replace. This plan makes it *less* purely LLM-judged, not zero -- fully replacing it is future work, not in scope here.
- **Tier 1 is not a new invention**: it's today's existing 8 tests, just fixed (Tasks 1-2). No test content is thrown away, only reordered into when it's allowed to run.
