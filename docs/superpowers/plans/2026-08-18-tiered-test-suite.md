# Tiered E2E Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure `tests/run_tests.py` so the E2E suite starts at a genuinely minimal difficulty per capability and only grows harder once the swarm proves it can reliably clear the current bar — instead of every capability being tested at whatever scope its label naturally implies (which put a full microservice-scaffold goal in the same tier as "add two numbers" on day one).

**Architecture:** An open-ended ladder of tiers (not just two) over the same 8 capability labels — `TIERS = [TIER_0_TESTS, TIER_1_TESTS, TIER_2_TESTS, ...]`. Tier 0 is genuinely minimal; Tier 1 is today's existing goals (fixed validators, real timeouts); Tier 2 is deliberately harder, built from goals this swarm has *already been observed* attempting historically (see Task 3b) — not invented from scratch. Each CronJob run determines which tier is currently unlocked by reading the git-tracked run history on `bot/test-results` (now that #31 fixed the push bug, this history actually accumulates), runs only that tier's tests, and tags its result file with the tier that ran. Promotion from tier *N* to *N+1* happens automatically once 10 consecutive clean runs are recorded at tier *N* — the same mechanism repeats at every rung, so adding a Tier 3 later is just appending a list, not touching the promotion logic. Chaining: `check_plateau.py` reports the streak at every tier every run, so the *whole ladder's* progress is visible at once ("quantify all together"), not just the currently-active tier.

**Tech Stack:** Python 3.12 (`tests/run_tests.py`, `tests/check_plateau.py`), no new dependencies.

## Global Constraints

- Every test still goes through the real pipeline (`/api/submit` → plan → code → verify) — no shortcuts that bypass planner, since that's what's actually being validated.
- `STREAK_TARGET = 10` (already established in `check_plateau.py`) stays the promotion bar for both tiers — reuse it, don't invent a different number.
- Real, data-derived timeouts only: 900s for any single-hop-equivalent (~3 pipeline hops) test, derived from live-measured per-hop latency (median 120s, P90 438s across 75 measured transitions, 2026-08-18). Never hardcode a timeout without tracing it back to this math or an equivalent fresh measurement.
- Every validator must independently check something about the artifact or a structural fact (sub-task count, issue keywords) — never let "did the LLM call succeed" alone count as a pass.
- Follow this repo's standing workflow: every change on its own branch, through `make deploy` (not raw `helm upgrade`), CI green, then merge. Never add `Co-Authored-By: Claude` to commits.

---

### Task 1: Fix the weak/absent validators

**Files:**
- Modify: `tests/run_tests.py:166-178` (`validate_python`), `tests/run_tests.py` (the `SECURITY_AWARENESS`/`STRUCTURED_OUTPUT`/`INFRA_AS_CODE`/`TESTING`/`DOCUMENTATION`/`DECOMPOSITION` test dicts and their validators, and `evaluate()`'s `min_subtasks` branch)
- Test: `tests/test_validators.py` (new — this project has no unit tests for the test-runner's own validators; add them now rather than trust them blind)

**Interfaces:**
- Produces: `validate_python(code: str, require_type_hints: bool = False) -> tuple[bool, list[str]]` — extended signature, backward compatible (existing callers that don't pass `require_type_hints` keep today's behavior).
- Produces: `validate_error_handling(code: str, required_exceptions: list[str]) -> tuple[bool, list[str]]` — new, replaces reusing `validate_python` for `ERROR_HANDLING`.
- Produces: `validate_k8s_deployment(text: str, require_resources: bool = True) -> tuple[bool, list[str]]` — new, replaces reusing generic `validate_yaml` for `STRUCTURED_OUTPUT`; `require_resources` lets Tier 0 skip that requirement without post-hoc issue-filtering.
- Produces: `validate_security(code: str) -> tuple[bool, list[str]]` — new, gives `SECURITY_AWARENESS` an actual independent check instead of relying solely on the verifier's own issue-list wording.
- Produces: `validate_infra_yaml(text: str, required_keys: tuple[str, ...] = (...)) -> tuple[bool, list[str]]` — extended signature, backward compatible, lets each tier require a different key set without post-hoc issue-filtering.
- Produces: `validate_has_tests(code: str, min_asserts: int = 2) -> tuple[bool, list[str]]` — extended signature, backward compatible, lets harder tiers demand a wider test surface.
- Produces: `validate_readme(text: str) -> tuple[bool, list[str]]` — new, checks markdown section structure; needed because Tier 2's `DOCUMENTATION` goal produces a README, not Python code, so `validate_has_docstring` cannot validate it.
- Produces: `validate_decomposition(text: str, required_pieces: tuple[str, ...]) -> tuple[bool, list[str]]` — new, **replaces `min_subtasks` entirely for `DECOMPOSITION`**. Confirmed via live packet history (5/5 sampled tasks, trivial through complex, including a Redis-cluster-with-persistence/auth/sentinel goal) that this swarm's planner always spawns exactly one `code`-type packet per task regardless of goal complexity — `min_subtasks >= 2` is therefore not a difficulty gate, it's an unwinnable check at any tier. This validates decomposition at the artifact level instead (does the one returned artifact show evidence of each distinct piece the goal asked for), which works with how the planner actually behaves instead of how the plan originally assumed it behaved.

- [ ] **Step 1: Write the failing tests for the fixed validators**

```python
# tests/test_validators.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import (
    validate_python, validate_error_handling, validate_k8s_deployment, validate_security,
    validate_infra_yaml, validate_has_tests, validate_readme, validate_decomposition,
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

    # Tier 0 doesn't require resources yet -- parameterized, not post-filtered
    no_resources = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
    valid, issues = validate_k8s_deployment(no_resources)
    assert valid is False
    valid, issues = validate_k8s_deployment(no_resources, require_resources=False)
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


def test_infra_yaml_required_keys_are_parameterized_not_hardcoded():
    minimal = "persistence:\n  enabled: true\nresources:\n  limits: {cpu: '500m'}\n"
    # default call keeps today's 4-key behavior
    valid, issues = validate_infra_yaml(minimal)
    assert valid is False
    assert any("auth" in i for i in issues)

    # Tier 0 asks for fewer keys -- must be able to narrow the requirement,
    # not just strip matching issue strings out after the fact
    valid, issues = validate_infra_yaml(minimal, required_keys=("persistence", "resources"))
    assert valid is True

    # Tier 2 asks for more keys than today's default 4
    harder = minimal + "auth:\n  enabled: true\nsentinel:\n  enabled: true\ntls:\n  enabled: true\nbackup:\n  schedule: '0 * * * *'\n"
    valid, issues = validate_infra_yaml(harder, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup"))
    assert valid is True
    valid, issues = validate_infra_yaml(minimal, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup"))
    assert valid is False
    assert any("tls" in i for i in issues) and any("backup" in i for i in issues)


def test_has_tests_min_asserts_is_parameterized():
    two_asserts = "def double(n):\n    return n * 2\n\nassert double(2) == 4\nassert double(0) == 0\n"
    valid, issues = validate_has_tests(two_asserts)  # default min_asserts=2, today's behavior
    assert valid is True
    valid, issues = validate_has_tests(two_asserts, min_asserts=5)
    assert valid is False
    assert any("5" in i for i in issues)


def test_decomposition_checks_artifact_content_not_packet_count():
    # Real packet history (5/5 sampled tasks, trivial through complex) shows
    # this planner always spawns exactly one code-type packet -- min_subtasks
    # was checking something that never varies. This checks the one artifact
    # actually covers each distinct piece the goal asked for instead.
    from tests.run_tests import validate_decomposition
    thin = "def add(a, b):\n    return a + b\n"
    valid, issues = validate_decomposition(thin, required_pieces=("def ", "assert"))
    assert valid is False
    assert any("assert" in i for i in issues)

    real = "def add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n"
    valid, issues = validate_decomposition(real, required_pieces=("def ", "assert"))
    assert valid is True


def test_readme_checks_markdown_sections_not_python_docstring_syntax():
    # validate_has_docstring would fail this every time -- a real README never
    # contains triple-quoted Python docstrings. This is a distinct check.
    bare = "# My Package\n\nA thing that does stuff.\n"
    valid, issues = validate_readme(bare)
    assert valid is False

    real = (
        "# My Package\n\n"
        "## Installation\n\n```\npip install my-package\n```\n\n"
        "## Usage\n\n```python\nimport my_package\n```\n\n"
        "## API Reference\n\n`my_package.run()` -- runs the thing.\n"
    )
    valid, issues = validate_readme(real)
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


def validate_k8s_deployment(text: str, require_resources: bool = True) -> tuple[bool, list[str]]:
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
    if require_resources:
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


def validate_infra_yaml(
    text: str,
    required_keys: tuple[str, ...] = ("persistence", "auth", "sentinel", "resources"),
) -> tuple[bool, list[str]]:
    """Parameterized so each tier can require a different key set without
    running the full check and string-filtering issues out after the fact
    (which is what the original Tier 0 draft did -- fragile, since it breaks
    the moment an issue message's wording changes)."""
    try:
        import yaml
        doc = yaml.safe_load(_strip_markdown(text))
    except Exception as e:
        return False, [f"Invalid YAML: {e}"]
    if not isinstance(doc, dict):
        return False, ["YAML did not parse to a mapping"]
    flat = str(doc).lower()
    issues = [f"Missing '{key}' config" for key in required_keys if key not in flat]
    return len(issues) == 0, issues


def validate_has_tests(code: str, min_asserts: int = 2) -> tuple[bool, list[str]]:
    """TESTING: does the artifact include actual test code, not just the
    function it's meant to test? min_asserts lets harder tiers demand a
    wider test surface instead of just a goal-text claim nothing checks."""
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    if "def test_" not in code and code.count("assert ") < min_asserts:
        issues.append(f"No dedicated test function and fewer than {min_asserts} assertions found")
    return len(issues) == 0, issues


def validate_decomposition(text: str, required_pieces: tuple[str, ...]) -> tuple[bool, list[str]]:
    """DECOMPOSITION: checks the single returned artifact for evidence of
    each distinct piece the goal asked for, matched case-insensitively.
    Deliberately NOT a sub-packet count -- confirmed via real packet history
    that this planner always spawns exactly one code-type packet regardless
    of goal complexity, so counting packets can never distinguish an easy
    goal from a hard one. This checks the artifact's own content instead."""
    lowered = text.lower()
    issues = [f"No evidence of {piece!r} in the artifact" for piece in required_pieces if piece.lower() not in lowered]
    return len(issues) == 0, issues


def validate_readme(text: str) -> tuple[bool, list[str]]:
    """DOCUMENTATION (Tier 2): checks a README's actual sections. Deliberately
    NOT validate_has_docstring -- that checks for a triple-quoted Python
    docstring, which a real README will never contain, so reusing it here
    would fail every legitimate submission."""
    lowered = text.lower()
    issues = []
    if "install" not in lowered:
        issues.append("No installation section found")
    if "usage" not in lowered and "example" not in lowered:
        issues.append("No usage/example section found")
    if "api" not in lowered and "reference" not in lowered:
        issues.append("No API reference section found")
    return len(issues) == 0, issues
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_validators.py -v`
Expected: all 8 tests PASS.

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
{
    "label": "DECOMPOSITION",
    "goal": "scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, and README",
    # No "min_subtasks" key any more -- confirmed via real packet history that
    # code_count never varies with goal complexity, so it never actually
    # measured this goal's difficulty. Checks the one returned artifact for
    # evidence of each named component instead.
    "validator": lambda text: validate_decomposition(
        text, required_pieces=("fastapi", "postgres", "docker-compose", "test", "readme")
    ),
    "threshold": 0.0,
},
```

Also remove the now-dead `min_subtasks` branch from `evaluate()` — it read `result.get("code_count", 0)`, a number confirmed to never vary with goal difficulty:

```python
# tests/run_tests.py -- evaluate(), delete this block entirely (validate_decomposition
# above is called through test["validator"](output) like every other test now,
# so no special-cased branch is needed)
    if "min_subtasks" in test:
        code_count = result.get("code_count", 0)
        if code_count < test["min_subtasks"]:
            valid = False
            issues = issues + [f"Only {code_count} sub-task(s) spawned, expected >= {test['min_subtasks']}"]
```

- [ ] **Step 6: Commit**

```bash
git add tests/run_tests.py tests/test_validators.py
git commit -m "fix: give all six weak validators real independent checks; replace DECOMPOSITION's unwinnable min_subtasks with an artifact-content check"
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

### Task 3a: Define Tier 0 — genuinely minimal goals for all 8 capabilities

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
        "validator": lambda text: validate_k8s_deployment(text, require_resources=False),
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "DECOMPOSITION",
        "goal": "write a Python function and a test for it",
        # No min_subtasks -- confirmed unwinnable at any tier (see Task 1).
        # Checks the artifact contains both a real function and a real test.
        "validator": lambda code: validate_decomposition(code, required_pieces=("def ", "assert")),
        "threshold": 0.0,
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
        "validator": lambda text: validate_infra_yaml(text, required_keys=("persistence", "resources")),
        "threshold": 0.6,
        "timeout": 900,
    },
    {
        "label": "TESTING",
        "goal": "write a Python function that doubles a number, plus one test for it",
        "validator": validate_has_tests,  # default min_asserts=2, matches this goal's size
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

### Task 3b: Define Tier 2 — harder goals, grounded in real historical evidence

**Why these exact goals:** every goal below is not invented — it's a task this swarm was *already observed attempting* in real packet history during this project (visible in the dashboard's packet list / `bot/test-results` runs), several already scoring in the 0.6-0.9 range. That's the same "data-derived, not guessed" principle Task 2's timeouts follow, applied to difficulty instead of duration. Using proven-attemptable-but-harder goals means Tier 2 is a real next rung, not a guess at what "harder" should mean.

**Files:**
- Modify: `tests/run_tests.py` (add `TIER_2_TESTS`, append to the `TIERS` list from Task 4)

**Interfaces:**
- Produces: `TIER_2_TESTS: list[dict]` — same 8 labels, each strictly harder than its `TIER_1_TESTS` counterpart (one more required exception / one more required field / one more sub-task), reusing the parameterized validator functions from Task 1 with tighter arguments, plus `validate_readme` for `DOCUMENTATION` (its Tier 2 artifact is a README, not Python code, so no existing validator applies).

- [ ] **Step 1: Write Tier 2's harder versions**

```python
TIER_2_TESTS = [
    {
        "label": "CODE_GENERATION",
        "goal": "write a Python function that computes the nth Fibonacci number iteratively, with type hints, a docstring, and input validation that raises ValueError for negative n",
        "validator": lambda code: (lambda v, i: (v and "raise" in code and "ValueError" in code,
                                                   i if v else i + (["missing ValueError on negative input"] if "ValueError" not in code else [])))(*validate_python(code, require_type_hints=True)),
        "threshold": 0.75,
        "timeout": 900,
    },
    {
        "label": "ERROR_HANDLING",
        "goal": "write a Python function that reads a JSON config file and returns a dict, handling FileNotFoundError, JSONDecodeError, and PermissionError",
        "validator": lambda code: validate_error_handling(code, ["FileNotFoundError", "JSONDecodeError", "PermissionError"]),
        "threshold": 0.75,
        "timeout": 900,
    },
    {
        "label": "STRUCTURED_OUTPUT",
        "goal": "generate a Kubernetes Deployment manifest for a Node.js API with resource limits, liveness and readiness probes, and 2 replicas",
        "validator": lambda text: (lambda v, i: (v and "livenessprobe" in text.lower() and "readinessprobe" in text.lower(),
                                                   i + ([] if "livenessprobe" in text.lower() else ["missing livenessProbe"])
                                                     + ([] if "readinessprobe" in text.lower() else ["missing readinessProbe"])))(*validate_k8s_deployment(text)),
        "threshold": 0.70,
        "timeout": 900,
    },
    {
        "label": "DECOMPOSITION",
        # Strictly harder than Tier 1's existing goal (scaffold a microservice:
        # FastAPI + Postgres + Docker Compose + tests + README) by requiring
        # evidence of one more concrete deliverable (a CI workflow) in the
        # artifact, not by shrinking the scope -- an earlier draft of this
        # entry accidentally asked for LESS than Tier 1. No min_subtasks --
        # confirmed unwinnable at any tier (see Task 1).
        "goal": "scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, README, and a GitHub Actions CI workflow",
        "validator": lambda text: validate_decomposition(
            text, required_pieces=("fastapi", "postgres", "docker-compose", "test", "readme", "workflow")
        ),
        "threshold": 0.0,
        "timeout": 900,
    },
    {
        "label": "SECURITY_AWARENESS",
        # Harder than Tier 1 (fetch a URL from user input) by adding a second,
        # distinct risk surface (SSRF via a web endpoint, plus a user-controlled
        # filename -- path traversal) rather than swapping user input for a
        # config file, which reads as no harder or even milder than Tier 1.
        "goal": "generate a Flask endpoint that accepts a URL and a filename from the request body, downloads the URL, and saves it to disk under that filename",
        "validator": validate_security,
        "threshold": 0.0,
        "required_issue_keywords": ["url", "valid", "path", "travers", "filename", "sanitiz", "ssrf"],
        "timeout": 900,
    },
    {
        "label": "INFRA_AS_CODE",
        # Harder than Tier 1's Redis Helm values (persistence + auth + sentinel +
        # resource limits) by adding TLS and a backup schedule. Uses the
        # parameterized required_keys -- plain `validate_infra_yaml` here would
        # silently fall back to its 4-key default and never check tls/backup,
        # making the harder wording cosmetic only.
        "goal": "generate a Helm values.yaml file for a production Redis cluster with persistence, auth, sentinel, resource limits, TLS between nodes, and a scheduled backup CronJob",
        "validator": lambda text: validate_infra_yaml(
            text, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup")
        ),
        "threshold": 0.65,
        "timeout": 900,
    },
    {
        "label": "TESTING",
        # Harder than Tier 1's factorial-plus-edge-case-tests by requiring a wider
        # test surface. Uses min_asserts=5 -- plain `validate_has_tests` would
        # accept as few as 2 asserts, never actually checking the "5 distinct
        # cases" the goal text asks for.
        "goal": "write a Python function that validates password strength against multiple rules (minimum length, at least one uppercase letter, at least one digit, at least one symbol), plus unit tests covering at least 5 distinct pass/fail cases",
        "validator": lambda code: validate_has_tests(code, min_asserts=5),
        "threshold": 0.65,
        "timeout": 900,
    },
    {
        "label": "DOCUMENTATION",
        # validate_has_docstring checks for a Python triple-quoted docstring --
        # a README will never have one, so an earlier draft of this entry would
        # have failed every legitimate submission. validate_readme checks
        # markdown section structure instead: still a structural presence
        # check, content quality stays partially LLM-judged, same caveat as
        # SECURITY_AWARENESS.
        "goal": "create a README.md file with comprehensive documentation for a Python package, including installation, usage examples, and API reference",
        "validator": validate_readme,
        "threshold": 0.65,
        "timeout": 900,
    },
]
```

- [ ] **Step 2: Commit**

```bash
git add tests/run_tests.py
git commit -m "feat: add Tier 2 -- harder goals grounded in real historical packet evidence"
```

---

### Task 4: Tier-aware plateau tracking and automatic promotion

**Files:**
- Modify: `tests/check_plateau.py`
- Modify: `tests/run_tests.py` (`main()` — determine tier before submitting anything, tag saved results with it)
- Test: `tests/test_check_plateau.py` (new), `tests/test_tier_wiring.py` (new)

**Interfaces:**
- Consumes: `TIER_0_TESTS`, `TIER_1_TESTS` from Task 3a, `TIER_2_TESTS` from Task 3b, combined into `TIERS: list[list[dict]]` (index = tier number).
- Produces: `determine_current_tier(results_dir: str) -> int` — returns the highest tier index unlocked so far. Adding a `TIER_3_TESTS` later means appending it to `TIERS`; this function's logic does not change.
- Produces: each saved `tests/results/run_*.json` now includes a top-level `"tier": N` key.

- [ ] **Step 1: Write the failing tests for the tier-walking logic**

`tests/check_plateau.py` has zero test coverage today, and this task rewrites its
core promotion logic — that combination is exactly what this plan's own Task 1
refused to accept for the validators. Write these first, against the *current*
(non-tier-aware) `check_plateau.py`, so Step 2 below has something real to turn green.

```python
# tests/test_check_plateau.py
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.check_plateau import is_clean_run, streak_for_tier, determine_current_tier, STREAK_TARGET, TIER_LABELS


def _run(tier, statuses: dict):
    """statuses: {label: "PASS"|"FAIL"|"TIMEOUT", ...} for a subset or all of TIER_LABELS."""
    return {"tier": tier, "results": [{"label": label, "status": status} for label, status in statuses.items()]}


def _all_pass(tier):
    return _run(tier, {label: "PASS" for label in TIER_LABELS})


def _all_pass_but_one_fail(tier):
    labels = list(TIER_LABELS)
    statuses = {label: "PASS" for label in labels}
    statuses[labels[0]] = "FAIL"
    return _run(tier, statuses)


def test_run_from_a_different_tier_is_not_evidence_either_way():
    run = _all_pass(tier=1)
    assert is_clean_run(run, tier=0) is None


def test_run_missing_a_label_is_not_evidence_either_way():
    labels = list(TIER_LABELS)
    partial = _run(tier=0, statuses={l: "PASS" for l in labels[:-1]})  # one label missing
    assert is_clean_run(partial, tier=0) is None


def test_all_labels_passing_is_clean():
    assert is_clean_run(_all_pass(tier=0), tier=0) is True


def test_any_label_failing_is_not_clean():
    assert is_clean_run(_all_pass_but_one_fail(tier=0), tier=0) is False


def test_streak_stops_walking_backward_at_first_non_clean_run(tmp_path):
    # oldest -> newest: clean, clean, FAIL, clean, clean -- streak should be 2, not 4
    runs = [_all_pass(0), _all_pass(0), _all_pass_but_one_fail(0), _all_pass(0), _all_pass(0)]
    for i, run in enumerate(runs):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    assert streak_for_tier(str(tmp_path), tier=0) == 2


def test_streak_skips_interleaved_runs_from_other_tiers_without_breaking(tmp_path):
    # a tier-1 run sitting in between two clean tier-0 runs shouldn't break the tier-0 streak
    runs = [_all_pass(0), _all_pass(1), _all_pass(0)]
    for i, run in enumerate(runs):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    assert streak_for_tier(str(tmp_path), tier=0) == 2


def test_determine_current_tier_is_0_with_no_results_dir():
    assert determine_current_tier("/nonexistent/path") == 0


def test_determine_current_tier_promotes_once_streak_target_is_met(tmp_path):
    for i in range(STREAK_TARGET):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(_all_pass(0)))
    assert determine_current_tier(str(tmp_path)) == 1


def test_determine_current_tier_never_exceeds_the_top_of_the_ladder(tmp_path):
    # every tier maxed out -- must not walk past the last index in TIERS
    from tests.check_plateau import TIERS
    top = len(TIERS) - 1
    i = 0
    for tier in range(top + 1):
        for _ in range(STREAK_TARGET):
            (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(_all_pass(tier)))
            i += 1
    assert determine_current_tier(str(tmp_path)) == top
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_check_plateau.py -v`
Expected: failures against today's `check_plateau.py` — `is_clean_run`/`streak_for_tier` currently take a single-arg (no `tier` parameter) and there's no `TIER_LABELS`, `determine_current_tier`, or `TIERS` import yet, so this should fail on import or `TypeError`, not silently pass.

- [ ] **Step 3: Make `check_plateau.py` tier-aware for an arbitrary number of tiers**

```python
# tests/check_plateau.py
# TIERS is imported from tests.run_tests: TIERS = [TIER_0_TESTS, TIER_1_TESTS, TIER_2_TESTS]
# All 8 labels are the same at every tier -- only goal difficulty changes -- so the
# label set can be read from tier 0 rather than repeated per tier.
from tests.run_tests import TIERS

TIER_LABELS = {label["label"] for label in TIERS[0]}


def is_clean_run(run: dict, tier: int) -> bool | None:
    if run.get("tier") != tier:
        return None  # a run from a different tier isn't evidence for this tier's streak
    by_label = {r["label"]: r for r in run.get("results", [])}
    if not TIER_LABELS.issubset(by_label.keys()):
        return None
    return all(by_label[label].get("status") == "PASS" for label in TIER_LABELS)


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
    """Walk up from tier 0: promote to tier N+1 only once tier N has hit
    the streak target, and stop at the top of the ladder (the highest
    defined tier) even if it also plateaus -- Task 5 in the plan calls out
    what to do when that happens (consider adding another tier), rather
    than this function silently inventing one."""
    if not os.path.isdir(results_dir):
        return 0
    tier = 0
    while tier < len(TIERS) - 1 and streak_for_tier(results_dir, tier) >= STREAK_TARGET:
        tier += 1
    return tier
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

    for tier in range(len(TIERS)):
        streak = streak_for_tier(results_dir, tier)
        print(f"Tier {tier}: {streak} / {STREAK_TARGET} consecutive clean runs")

    active = determine_current_tier(results_dir)
    top_tier = len(TIERS) - 1
    print(f"\nCurrently active: Tier {active}")
    if active == top_tier and streak_for_tier(results_dir, top_tier) >= STREAK_TARGET:
        print(f"✓ FINISH LINE REACHED on Tier {top_tier} — the suite has plateaued at the "
              "top of the current ladder. Time to consider adding a harder next tier "
              "instead of the CronJob continuing to just confirm the same thing.")


if __name__ == "__main__":
    main()
```

This keeps the CLI invocation (`python3 check_plateau.py [results_dir]`) identical — only the printed output changes, now showing every tier's streak (the "quantify all together" chain view) and which one is currently active. Adding a Tier 3 later means appending to `TIERS` in `run_tests.py` — nothing here needs to change.

- [ ] **Step 4: Run to verify Step 1's tests now pass**

Run: `python3 -m pytest tests/test_check_plateau.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Write the failing tests for the tier-selection wiring and the git-history fetch helper**

This is the exact class of bug that bit this project once already: the original
test-runner git-push logic went untested and silently failed on *every single
run* for the suite's entire lifetime before anyone noticed (fixed in #31). The
new `_fetch_results_history()` does the same kind of subprocess/git work, and
`main()`'s tier-selection is currently unwritten — write both sets of tests now,
before either exists, not after deploying and hoping.

```python
# tests/test_tier_wiring.py
import sys, os, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import select_active_tier, _fetch_results_history, TIER_0_TESTS, TIER_1_TESTS, TIERS


def test_select_active_tier_defaults_to_tier_0_with_no_history():
    tier, tests = select_active_tier(None)
    assert tier == 0
    assert tests is TIER_0_TESTS


def test_select_active_tier_picks_the_right_tests_list_for_each_tier(tmp_path):
    from tests.check_plateau import STREAK_TARGET, TIER_LABELS
    # 10 clean tier-0 runs on disk -> should select tier 1's actual test list
    run = {"tier": 0, "results": [{"label": l, "status": "PASS"} for l in TIER_LABELS]}
    for i in range(STREAK_TARGET):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    tier, tests = select_active_tier(str(tmp_path))
    assert tier == 1
    assert tests is TIER_1_TESTS


def test_fetch_results_history_returns_none_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert _fetch_results_history() is None


def test_fetch_results_history_builds_the_correct_clone_command(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("fake-token-value")
    (tmp_path / "repo").write_text("someorg/somerepo")
    monkeypatch.setattr("os.path.exists", lambda p: p in (str(tmp_path / "token"), str(tmp_path / "repo")) or p.startswith("/git-creds"))
    monkeypatch.setattr("builtins.open", lambda p, *a: open(str(tmp_path / "token") if "token" in p else str(tmp_path / "repo")))

    captured = {}
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr("subprocess.run", fake_run)

    _fetch_results_history()
    assert "cmd" in captured, "git clone was never invoked"
    assert "--depth=50" in captured["cmd"]
    assert "bot/test-results" in captured["cmd"]
    assert "fake-token-value" in captured["cmd"][-2]  # URL contains the token
    assert "someorg/somerepo" in captured["cmd"][-2]


def test_fetch_results_history_returns_none_on_clone_failure(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("t")
    (tmp_path / "repo").write_text("o/r")
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda p, *a: open(str(tmp_path / "token")))
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert _fetch_results_history() is None
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_tier_wiring.py -v`
Expected: `ImportError` — `select_active_tier` and `_fetch_results_history` don't exist in `run_tests.py` yet.

- [ ] **Step 7: Wire tier selection into `run_tests.py`'s `main()`**

This requires knowing history *before* submitting any tests — which means fetching `tests/results/` from `bot/test-results` at the start of the run, not just at the end (today it only clones for the push step, after tests already ran). Add an early, read-only clone, and keep the tier-selection decision itself in a small pure function so it stays testable without touching `main()`'s I/O:

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
# tests/run_tests.py -- near TIER_0_TESTS/TIER_1_TESTS/TIER_2_TESTS definitions
TIERS = [TIER_0_TESTS, TIER_1_TESTS, TIER_2_TESTS]  # append future tiers here only

def select_active_tier(results_dir: str | None) -> tuple[int, list[dict]]:
    """Pure and testable on its own -- no git, no network -- so a bug in
    picking the wrong tier's test list is caught by Step 5's tests, not
    discovered live on the cluster."""
    from tests.check_plateau import determine_current_tier
    current_tier = determine_current_tier(results_dir) if results_dir else 0
    return current_tier, TIERS[current_tier]
```

```python
# tests/run_tests.py, in main(), before the smoke test section
current_tier, active_tests = select_active_tier(_fetch_results_history())
print(f"\n[TIER] Running Tier {current_tier} ({len(active_tests)} capability tests)", flush=True)
```

Replace the later `shuffled = random.sample(TESTS, len(TESTS))` with `shuffled = random.sample(active_tests, len(active_tests))`.

- [ ] **Step 8: Run to verify Step 5's tests now pass**

Run: `python3 -m pytest tests/test_tier_wiring.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 9: Tag saved results with the tier that ran**

```python
# tests/run_tests.py, wherever the results dict is built before json.dump
output = {
    "tier": current_tier,
    "results": results,
    ...
}
```

- [ ] **Step 10: Commit**

```bash
git add tests/run_tests.py tests/check_plateau.py tests/test_check_plateau.py tests/test_tier_wiring.py
git commit -m "feat: tier-aware plateau tracking with automatic promotion up the tier ladder"
```

---

### Task 5: Deploy and verify via the next scheduled run

**Files:** none (verification only)

**Standing constraint for this repo:** never trigger the E2E suite ad hoc, outside the CronJob — only observe. So unlike a typical "deploy then manually fire a job" verification, this task deploys and then waits for the next natural hourly run.

- [ ] **Step 1: Deploy**

```bash
make deploy
```

- [ ] **Step 2: Wait for the next scheduled hourly run, then check its log**

```bash
kubectl get jobs -n cxp -l job-name --sort-by=.status.startTime 2>&1 | tail -3
# once the newest cxp-test-runner-* job is Complete/Failed:
kubectl logs -n cxp job/<newest-cxp-test-runner-job> -c test-runner | head -5
```

Expected: log line `[TIER] Running Tier 0 (8 capability tests)` before any test is submitted, and (given the ~900s timeouts and the swarm's current healthy state) at least several of the 8 Tier-0 tests should now register PASS rather than TIMEOUT — this is the actual proof the restructuring worked, not just that it deployed.

- [ ] **Step 3: Confirm results actually persist this time**

```bash
git fetch origin bot/test-results
git log origin/bot/test-results --oneline -3
```

Expected: a new commit from that run, containing a `tier: 0`-tagged result file — confirming both this plan's fix and #31's git-push fix are working together.

- [ ] **Step 4: Check the whole ladder's streak, not just Tier 0**

```bash
python3 tests/check_plateau.py tests/results
```

Expected output shows a streak line for every tier in `TIERS` (all `0 / 10` at first), and "Currently active: Tier 0" — the first real, chained readout of the whole progression this plan built.

---

## Baseline Expectations (for comparison against real runs)

Built only from real `verify` packet scores observed in this project's actual packet history, matched against near-identical goal text. Sample sizes are small (n=2-3 per capability) — treat as a rough baseline to diff real runs against, not a confident prediction. `DECOMPOSITION` and `SECURITY_AWARENESS` have no matched score samples (they're gated by artifact-content/keyword-match, not score), so no rate is given for either.

| Capability | Real historical scores (Tier-1-equivalent goal) | Threshold | Historical hit rate |
|---|---|---|---|
| ERROR_HANDLING | 0.80, 0.80 | 0.75 | 2/2 |
| STRUCTURED_OUTPUT | 0.80, 0.90 | 0.70 | 2/2 |
| DOCUMENTATION | 0.70, 0.75, 0.80 | 0.70 | 3/3 |
| INFRA_AS_CODE | 0.30, 0.90, 1.00 | 0.70 | 2/3 — same goal, high variance; this capability's real risk is inconsistency, not difficulty |
| CODE_GENERATION | 0.90, 0.50 | 0.75 | 1/2 |
| TESTING | 0.60, 0.70 | 0.70 | 1/2, right at the threshold |
| DECOMPOSITION | none found | — | unknown, but the validator itself is now confirmed sound (artifact-content check, not the disproven packet-count check — see Task 1) |
| SECURITY_AWARENESS | none found | — | unknown; gated by keyword match, not score |

**Directional expectations per tier:**
- **Tier 0**: no direct samples (never run at this exact wording), but every Tier-1-difficulty goal with real evidence above already cleared its bar most of the time — Tier 0, being strictly easier, should plausibly run higher (rough guess: 6-8 of 8 capabilities passing per run). Not measured yet.
- **Tier 1**: ~8 of 14 scored historical samples clear their bar (~55-80%, small n) → expect roughly 4-6 of 8 capabilities to PASS on a typical run, not a clean 8/8. That's a feature, not a bug — it means the 10-consecutive-clean-streak promotion gate has real teeth.
- **Tier 2**: zero data — none of these goals have ever run. Expect lower than Tier 1's already-mixed baseline by design; the first real Tier 2 run is what replaces this guess with ground truth.

**Once real runs start**: update this section (or better, let `check_plateau.py`'s per-tier streak output be the living version of this table) rather than trusting these numbers as static — they're a starting point for comparison, not a target to defend.

---

## Self-Review Notes

- **Spec coverage**: all problems raised are addressed — weak/absent validators (Task 1), mismatched flat timeouts (Task 2), tests sized without regard to progression (Task 3a/4), and no next-rung-after-Tier-1 (Task 3b) so the ladder doesn't dead-end the moment Tier 1 plateaus.
- **`SECURITY_AWARENESS` stays partially LLM-judged on purpose**: `validate_security`'s regex-ish check catches the crudest failures (no validation at all), but real security review still needs judgment a static check can't fully replace. This plan makes it *less* purely LLM-judged, not zero -- fully replacing it is future work, not in scope here.
- **Tier 1 is not a new invention**: it's today's existing 8 tests, just fixed (Tasks 1-2). No test content is thrown away, only reordered into when it's allowed to run.
- **Tier 2 goals are historical, not invented**: every `TIER_2_TESTS` goal (Task 3b) was pulled from a goal this swarm already attempted in real packet history during this project, several already scoring 0.6-0.9 -- so "harder" has the same evidence-based grounding as the 900s timeout does, not a guess at what the next rung should look like.
- **The ladder is intentionally open-ended**: `TIERS` is a plain list; a future Tier 3 needs only a new test-dict list appended to it. `determine_current_tier` and `check_plateau.py`'s `main()` already walk an arbitrary-length ladder, so no promotion-logic changes are needed when that day comes.
- **`DECOMPOSITION`'s `min_subtasks` mechanism was disproven, not just risky**: live packet history (5/5 sampled real tasks, from "hello world" to a Redis-cluster-with-persistence/auth/sentinel goal) confirmed this planner always spawns exactly one `code`-type packet regardless of goal complexity. `min_subtasks >= 2` could never pass at any tier. Fixed by replacing it with `validate_decomposition`, an artifact-content check, at all three tiers (Task 1, 3a, 3b) — this was found and fixed during planning, before any code was written, specifically to avoid repeating the "untested logic silently wrong in production" failure mode from earlier in this project.
- **Not yet implemented**: this plan is prepared but deliberately not executed yet -- no branch, code change, or deploy has happened. Execute via superpowers:subagent-driven-development or superpowers:executing-plans when ready, task by task, each on its own branch through CI before merge (per this repo's standing workflow discipline). Task 5's verification step waits for the next natural hourly CronJob run rather than triggering one manually, per this repo's standing "observe, don't ad-hoc trigger" rule.
