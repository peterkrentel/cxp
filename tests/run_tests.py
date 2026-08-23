#!/usr/bin/env python3
"""Self-improving test runner: submit → evaluate → trigger reflect → re-run."""

import json
import os
import random
import subprocess
import sys
import time
import urllib.request

from src.candidate_evaluation import evaluate_candidate

# The CronJob invokes this as a bare script (`python -u /app/tests/run_tests.py`),
# which puts only this script's OWN directory on sys.path, not its parent -- so
# select_active_tier()'s `from tests.check_plateau import ...` would fail to
# resolve `tests` as a package. Confirmed live: found while packaging
# check_plateau.py itself into the deployed app for the same reason. Fixing at
# the source rather than assuming the caller's invocation style, so this works
# regardless (same fix already applied in check_plateau.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = os.environ.get("CXP_WEB_API", "http://cxp-web:8080")
MEMORY_PATH = os.environ.get("CXP_MEMORY_PATH", "/data/memory.json")  # same PVC agents write to


def _http_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  GET {API}{path} failed: {type(e).__name__}: {e}")
        return {}


def _http_post(path: str, data: dict) -> dict:
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{API}{path}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  POST {API}{path} failed: {type(e).__name__}: {e}")
        return {}


def wait_for_ready(timeout=300):
    """Wait until the web API is responding."""
    print("⏳ Waiting for cluster to be ready...", flush=True)
    sys.stdout.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _http_get("/api/state")
        if state:
            print("✓ Cluster ready", flush=True)
            return True
        time.sleep(5)
    print("✗ Timeout waiting for cluster", flush=True)
    return False


def submit_task(goal: str, inputs: dict | None = None) -> str | None:
    data = {"goal": goal}
    if inputs is not None:
        data["inputs"] = inputs
    resp = _http_post("/api/submit", data)
    return resp.get("task_id")


def get_state() -> dict:
    return _http_get("/api/state")


def check_halted() -> dict | None:
    """Is the swarm currently halted? Checked before every submission —
    without this, a halt mid-run cascades into every remaining submission
    getting rejected with 409, which previously got reported as plain FAIL
    (looks like a capability regression) instead of "never got to run"."""
    return get_state().get("halt")


def run_candidate_comparison(
    *,
    candidate_id: str,
    source_attempt: dict,
    held_out_tests: list[dict],
) -> dict:
    """Run held-out tasks sequentially against active and staged skills.

    This deliberately returns a recommendation only. The caller remains
    responsible for publishing the report and a human remains responsible for
    promotion.
    """
    baseline_results = []
    candidate_results = []
    for test in held_out_tests:
        active_id = submit_task(test["goal"])
        active_raw = wait_for_results({active_id: test}, timeout=test["timeout"]) if active_id else {}
        baseline_results.append(evaluate(test, active_raw.get(active_id)))

        candidate_id_for_task = submit_task(test["goal"], inputs={"candidate_id": candidate_id})
        candidate_raw = (
            wait_for_results({candidate_id_for_task: test}, timeout=test["timeout"])
            if candidate_id_for_task else {}
        )
        candidate_results.append(evaluate(test, candidate_raw.get(candidate_id_for_task)))

    return evaluate_candidate(
        candidate_id=candidate_id,
        source_attempt=source_attempt,
        baseline_results=baseline_results,
        candidate_results=candidate_results,
    )


def wait_for_results(task_ids: dict, timeout=480) -> dict:
    """Poll until all tasks have code+verify done, or timeout. Returns {task_id: result}.

    Also tracks code_count (how many sub-tasks the planner actually spawned —
    for DECOMPOSITION) and verify_issues (accumulated issues text from every
    verify packet on the task — for SECURITY_AWARENESS, which cares whether
    the verifier *flagged* something, not just whether code compiled).

    Requires BOTH a done code packet AND at least one done verify packet
    before considering a task settled -- found live 2026-08-18: the exit
    condition used to check only `if code_pkt`, exiting the moment code
    finished and capturing whatever `best_score` happened to be at that
    instant (verify runs strictly after code, so this was reliably still
    0.0 -- not because anything scored badly, but because verify hadn't
    even started yet). Every prior "low score" result produced by this
    function is suspect for the same reason. `verify_seen` is tracked
    separately from `best_score` so a real, legitimate score of 0.0 from
    the verifier is never confused with "verify hasn't run yet".
    """
    deadline = time.time() + timeout
    results = {}
    pending = set(task_ids.keys())
    while time.time() < deadline and pending:
        packets = get_state().get("packets", [])
        for task_id in list(pending):
            code_pkt = None
            best_score = 0.0
            code_count = 0
            verify_seen = False
            verify_issues: list[str] = []
            plan_done_output = None
            for p in packets:
                if p.get("task_id") != task_id:
                    continue
                if p.get("type") == "plan" and p.get("status") == "done":
                    plan_done_output = p.get("output", "")
                if p.get("type") == "code":
                    code_count += 1
                    if p.get("status") == "done" and p.get("output"):
                        code_pkt = p
                if p.get("type") == "verify" and p.get("status") == "done":
                    verify_seen = True
                    best_score = max(best_score, p.get("score") or 0.0)
                    try:
                        verify_issues.extend(json.loads(p.get("output") or "{}").get("issues", []))
                    except Exception:
                        pass
            if code_pkt and verify_seen:
                results[task_id] = {**code_pkt, "score": best_score,
                                     "code_count": code_count, "verify_issues": verify_issues}
                pending.discard(task_id)
            elif plan_done_output is not None and (
                plan_done_output.startswith("Failed to decompose")
                or plan_done_output.startswith("Spawned 0 sub-packets")
            ):
                # Planner finished but spawned nothing -- e.g. a malformed/
                # truncated LLM decomposition response caught by planner.py's
                # JSONDecodeError handler. agent_shell.py still marks that
                # packet "done" and acks it (no exception was raised), so no
                # code/verify packet is EVER coming for this task_id --
                # waiting out the full timeout only hides why. Found live
                # 2026-08-20 (SECURITY_AWARENESS, task 563b0547): the LLM
                # answered, the plan packet completed, but its own output
                # already said "No sub-tasks spawned" the whole time this
                # was reported as a bare TIMEOUT. Settle now with the
                # planner's own explanation instead.
                #
                # Deliberately keyed off the plan packet's own output text,
                # NOT `code_count == 0` -- code_count only counts packets
                # that have themselves already *completed* (get_state()'s
                # packets only ever include a completion broadcast, see
                # agent_shell.py's _handle_message()). A freshly-spawned
                # child that hasn't been picked up yet is invisible to
                # code_count for a beat even on a perfectly healthy
                # decomposition -- found live 2026-08-20, minutes after an
                # earlier version of this fix (keyed off code_count == 0)
                # shipped: a real "Spawned 3 sub-packets" decomposition got
                # misreported as a failure anyway, purely from that race.
                results[task_id] = {
                    "task_id": task_id,
                    "decomposition_failed": True,
                    "output": plan_done_output,
                    "score": 0.0,
                    "code_count": 0,
                    "verify_issues": [],
                }
                pending.discard(task_id)
        if pending:
            time.sleep(5)
    return results


def trigger_improvement(label: str, issues: list[str]):
    """Submit a reflect task directly — bypass planner to avoid hallucinated subtasks."""
    goal = f"Test '{label}' failed: {'; '.join(issues[:2])}. Review executor skill and fix."
    resp = _http_post("/api/submit", {"goal": goal, "capability": "reflect"})
    print(f"  ↑ Improvement task submitted: {resp.get('task_id', '?')}")


def _strip_markdown(text: str) -> str:
    """Remove ```lang ... ``` fences so validators see raw code."""
    import re
    return re.sub(r"^```[\w]*\n", "", re.sub(r"\n```$", "", text.strip())).strip()


def validate_smoke(code: str) -> tuple[bool, list[str]]:
    """Tier 0: is the pipeline even working, at all — not "is the swarm
    good at X." Deliberately trivial so a failure here means something
    different (and more urgent) than a capability test failing."""
    code = _strip_markdown(code)
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    if "hello" not in code.lower():
        return False, ["Expected output doesn't mention 'hello'"]
    return True, []


def _code_capability_scores() -> list[float]:
    """All historical scores tagged capability='code' in episodic memory —
    the only durable record of skill_revision-vs-score over time. Coarse by
    necessity: entries are tagged by capability, not by which test produced
    them (reflect only maintains the executor skill today, so this is
    already exactly the score reflect's updates are meant to move)."""
    try:
        with open(MEMORY_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    return [e["score"] for e in data.get("episodic", [])
            if e.get("capability") == "code" and e.get("score") is not None]


def check_regression(baseline: list[float], this_run: list[float]) -> str | None:
    """Compare this run's average code-capability score against the recent
    historical average from before this run started. Returns a warning
    string if this looks like a real regression (not just a single bad
    sample), else None."""
    if len(baseline) < 5 or not this_run:
        return None  # not enough history to compare against yet
    baseline_avg = sum(baseline[-15:]) / len(baseline[-15:])
    this_avg = sum(this_run) / len(this_run)
    if this_avg < baseline_avg - 0.15:
        return f"this run avg {this_avg:.2f} vs recent history avg {baseline_avg:.2f} — looks like a regression, not noise"
    return None


def validate_python(code: str, require_type_hints: bool = False) -> tuple[bool, list[str]]:
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
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


def validate_yaml(text: str) -> tuple[bool, list[str]]:
    try:
        import yaml
        yaml.safe_load(_strip_markdown(text))
        return True, []
    except Exception as e:
        return False, [f"Invalid YAML: {e}"]


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
    """STRUCTURED_OUTPUT checks *valid* YAML; this checks *specific keys*
    exist, matching tests/examples.md's INFRA_AS_CODE expectations.
    Parameterized so each tier can require a different key set without
    running the full check and string-filtering issues out after the fact."""
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


def validate_always(_artifact: str) -> tuple[bool, list[str]]:
    """For tests whose real check isn't the artifact text itself — see
    required_issue_keywords (SECURITY_AWARENESS) in evaluate() below."""
    return True, []


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


def validate_has_docstring(code: str) -> tuple[bool, list[str]]:
    """DOCUMENTATION: is there a real docstring, not just a comment or
    nothing at all, and does it cover the basics a caller would need?"""
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    if '"""' not in code and "'''" not in code:
        return False, ["No docstring found"]
    lowered = code.lower()
    missing = [kw for kw in ("return", "example") if kw not in lowered]
    if missing:
        issues.append(f"Docstring missing expected sections: {missing}")
    return len(issues) == 0, issues


# SMOKE is a pipeline health check, always run first, never shuffled with the
# rest, and run unconditionally regardless of which difficulty tier (below)
# is active. A failure here means "the swarm is broken," a distinct and more
# urgent signal than "the swarm is bad at capability X." Not to be confused
# with TIER_0_TESTS -- "tier" here would mean "runs first," an unrelated,
# pre-existing use of the word.
SMOKE_TEST = {
    "label": "SMOKE",
    "goal": "write a Python one-liner that prints 'hello world'",
    "validator": validate_smoke,
    "threshold": 0.3,
    # 900s was sized for one hop's P90 latency (438s) x ~3 hops -- but SMOKE
    # gets no retry (one shot, unlike every capability test below), and real
    # data confirms 900s isn't enough under real conditions: both post-900s-
    # fix runs (2026-08-19, 13:20 and 14:13 UTC) still show SMOKE timing out,
    # even though individual Ollama calls in that window topped out around
    # 5m12s -- the ceiling with only 2 concurrent slots is queueing across 3
    # sequential hops, not any single call being unreasonably slow. 1800s
    # gives real margin for that queueing without touching Ollama's
    # concurrency architecture at all.
    "timeout": 1800,
}

# Tier 0 — genuinely minimal per-capability goals. Deliberately smaller than
# Tier 1 (and, where relevant, checked with a relaxed validator) so this tier
# is actually clearable on day one, instead of every capability starting at
# whatever scope its label naturally implies.
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

# Tier 1 — capability coverage, one per assessor label (8/9; SELF_IMPROVEMENT
# doesn't fit the pass/fail shape).
TIER_1_TESTS = [
    {
        "label": "CODE_GENERATION",
        "goal": "write a Python function that adds two numbers with type hints and docstring",
        "validator": lambda code: validate_python(code, require_type_hints=True),
        "threshold": 0.75,
        "timeout": 900,
    },
    {
        "label": "ERROR_HANDLING",
        "goal": "write a Python function that reads a JSON file and returns a dict, handling FileNotFoundError and JSONDecodeError",
        "validator": lambda code: validate_error_handling(code, ["FileNotFoundError", "JSONDecodeError"]),
        "threshold": 0.75,
        "timeout": 900,
    },
    {
        "label": "STRUCTURED_OUTPUT",
        "goal": "generate a Kubernetes Deployment manifest for a Node.js API with resource limits",
        "validator": validate_k8s_deployment,
        "threshold": 0.70,
        "timeout": 900,
    },
    {
        "label": "DECOMPOSITION",
        "goal": "scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, and README",
        # No "min_subtasks" any more -- confirmed via real packet history that
        # code_count never varies with goal complexity (this planner always
        # spawns exactly one code-type packet), so it never actually measured
        # this goal's difficulty. Checks the one returned artifact for
        # evidence of each named component instead.
        "validator": lambda text: validate_decomposition(
            text, required_pieces=("fastapi", "postgres", "docker-compose", "test", "readme")
        ),
        "threshold": 0.0,
        "timeout": 900,
    },
    {
        "label": "SECURITY_AWARENESS",
        "goal": "generate a Python web scraper that downloads URLs from user input and saves to disk",
        "validator": validate_security,
        "threshold": 0.0,   # pass = verifier correctly flagged a risk, not a high score
        # required_issue_keywords stays too -- this becomes a second, independent
        # signal on top of the new real check, not a replacement for it.
        "required_issue_keywords": ["url", "valid", "path", "travers", "rate limit", "sanitiz"],
        "timeout": 900,
    },
    {
        "label": "INFRA_AS_CODE",
        "goal": "generate a Helm values.yaml for a production Redis cluster with persistence, auth, sentinel, and resource limits",
        "validator": validate_infra_yaml,
        "threshold": 0.70,
        "timeout": 900,
    },
    {
        "label": "TESTING",
        "goal": "write a Python function that calculates the factorial of a number, plus unit tests covering zero, one, and a typical positive input",
        "validator": validate_has_tests,
        "threshold": 0.70,
        "timeout": 900,
    },
    {
        "label": "DOCUMENTATION",
        "goal": "write a Python function for binary search over a sorted list, with a comprehensive docstring covering parameters, return value, and an example usage",
        "validator": validate_has_docstring,
        "threshold": 0.70,
        "timeout": 900,
    },
]

# Tier 2 — harder than Tier 1. Every goal below is not invented -- each is a
# task this swarm was already observed attempting in real packet history
# during this project, several already scoring 0.6-0.9 -- the same
# data-derived principle Task 2's timeouts follow, applied to difficulty.
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
        # artifact, not by shrinking the scope. No min_subtasks -- confirmed
        # unwinnable at any tier (see Task 1).
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
        # filename -- path traversal).
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
        # parameterized required_keys -- plain validate_infra_yaml here would
        # silently fall back to its 4-key default and never check tls/backup.
        "goal": "generate a Helm values.yaml file for a production Redis cluster with persistence, auth, sentinel, resource limits, TLS between nodes, and a scheduled backup CronJob",
        "validator": lambda text: validate_infra_yaml(
            text, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup")
        ),
        "threshold": 0.65,
        "timeout": 900,
    },
    {
        "label": "TESTING",
        # Harder than Tier 1's factorial-plus-edge-case-tests by requiring a
        # wider test surface. min_asserts=5 -- plain validate_has_tests would
        # accept as few as 2 asserts, never checking the "5 distinct cases"
        # the goal text asks for.
        "goal": "write a Python function that validates password strength against multiple rules (minimum length, at least one uppercase letter, at least one digit, at least one symbol), plus unit tests covering at least 5 distinct pass/fail cases",
        "validator": lambda code: validate_has_tests(code, min_asserts=5),
        "threshold": 0.65,
        "timeout": 900,
    },
    {
        "label": "DOCUMENTATION",
        # validate_has_docstring checks for a Python triple-quoted docstring --
        # a README will never have one. validate_readme checks markdown
        # section structure instead: still a structural presence check,
        # content quality stays partially LLM-judged, same caveat as
        # SECURITY_AWARENESS.
        "goal": "create a README.md file with comprehensive documentation for a Python package, including installation, usage examples, and API reference",
        "validator": validate_readme,
        "threshold": 0.65,
        "timeout": 900,
    },
]

TIERS = [TIER_0_TESTS, TIER_1_TESTS, TIER_2_TESTS]  # append future tiers here only


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


def select_active_tier(results_dir: str | None) -> tuple[int, list[dict]]:
    """Pure and testable on its own -- no git, no network -- so a bug in
    picking the wrong tier's test list is caught by tests/test_tier_wiring.py,
    not discovered live on the cluster."""
    from tests.check_plateau import determine_current_tier
    current_tier = determine_current_tier(results_dir) if results_dir else 0
    return current_tier, TIERS[current_tier]


def evaluate(test: dict, result: dict | None, attempt: int = 1) -> dict:
    label = test["label"]
    if not result:
        return {"label": label, "status": "TIMEOUT"}
    if result.get("decomposition_failed"):
        # Distinct from TIMEOUT on purpose -- this is not "we don't know
        # what happened," it's "the planner told us exactly what happened"
        # (see wait_for_results()'s decomposition_failed branch). Keeping
        # the planner's own output as both `issues` and `reason` means the
        # POST-RUN ANALYSIS section (which reads `reason` for anything not
        # bucketed as TIMEOUT/wrong_format/low_score) surfaces the real
        # cause in the reflect task it submits, instead of "unknown failure".
        reason = result.get("output", "")
        return {
            "label": label,
            "status": "PLANNER_FAILED",
            "score": 0.0,
            "score_was_missing": True,
            "attempt": attempt,
            "issues": [f"Planner produced no sub-tasks: {reason}"],
            "reason": reason,
            "task_id": result.get("task_id"),
        }
    # `result.get("score") or 0` used to collapse "verifier genuinely scored
    # this 0.0" and "the score field was missing entirely" (e.g. an upstream
    # parsing failure) into the exact same value -- found live 2026-08-19
    # trying to explain a SMOKE result after the fact and discovering there
    # was no way to tell which had happened. Explicit None-check instead.
    raw_score = result.get("score")
    score_was_missing = raw_score is None
    score = raw_score if raw_score is not None else 0.0
    output = result.get("output", "")
    missing_note = " (score field missing -- defaulted, not a genuine 0.0)" if score_was_missing else ""
    print(f"  [{label}] score={score:.2f}{missing_note}  {len(output)} chars")
    valid, issues = test["validator"](output)

    if score_was_missing:
        issues = issues + ["No score returned by verifier (missing, not a genuine 0.0) -- likely an upstream parsing failure"]

    if "required_issue_keywords" in test:
        verify_issues_text = " ".join(result.get("verify_issues", [])).lower()
        if not any(kw in verify_issues_text for kw in test["required_issue_keywords"]):
            valid = False
            issues = issues + [f"Verifier didn't flag any of: {test['required_issue_keywords']}"]

    passed = valid and score >= test["threshold"]
    return {
        "label": label,
        "status": "PASS" if passed else "WARN",
        "score": score,
        "score_was_missing": score_was_missing,
        "attempt": attempt,
        "issues": issues,
        "task_id": result.get("task_id"),
    }


def main():
    print("[TRACE] Entering main()", flush=True)
    sys.stdout.flush()
    if not wait_for_ready():
        print("[TRACE] wait_for_ready() returned False, exiting", flush=True)
        sys.exit(1)
    print("[TRACE] wait_for_ready() succeeded", flush=True)

    halt = check_halted()
    if halt:
        print(f"\n⛔ Swarm is already halted ({halt.get('reason')}) — aborting before submitting anything. "
              f"A human needs to clear this before the suite can run.", flush=True)
        sys.exit(1)

    current_tier, active_tests = select_active_tier(_fetch_results_history())
    print(f"\n[TIER] Running Tier {current_tier} ({len(active_tests)} capability tests)", flush=True)

    # SMOKE: is the pipeline even working? Run before anything else, and
    # treat a failure as a distinct, more urgent signal than a capability
    # test failing — no point testing 8 capabilities if the plumbing's down.
    # Timeout is test-specific (SMOKE_TEST["timeout"], currently 1800s --
    # see the constant's own comment for why this is larger than the
    # capability tests' 900s). Smoke is always the FIRST request of every
    # run, so it's the one most likely to catch Ollama cold (model unloaded
    # since the last cycle) and gets no retry — a short timeout paired with
    # the worst timing made it the most fragile test, not the most
    # forgiving one, which is backwards for a health check.
    print("\nRunning smoke test (pipeline health check)...")
    smoke_task_id = submit_task(SMOKE_TEST["goal"])
    smoke_result = wait_for_results({smoke_task_id: SMOKE_TEST}, timeout=SMOKE_TEST["timeout"]) if smoke_task_id else {}
    smoke_eval = evaluate(SMOKE_TEST, smoke_result.get(smoke_task_id), attempt=1)
    if smoke_eval["status"] != "PASS":
        print(f"  ⚠ SMOKE FAILED ({smoke_eval['status']}) — the pipeline itself looks broken, "
              f"not just a specific capability. Running the rest of the suite anyway for more signal.")
        # evaluate() only returns an "issues" key when it actually ran the
        # validator (i.e. status != TIMEOUT) -- found live 2026-08-19: a
        # WARN result printed just a score and char count with no way to
        # tell, after the fact, whether validate_smoke rejected the code
        # (syntax error / missing "hello") or the verifier itself scored it
        # low, since the actual issues list was computed but never printed.
        if smoke_eval.get("issues"):
            print(f"    issues: {smoke_eval['issues']}")
    else:
        print("  ✓ SMOKE passed — pipeline is up")

    # Baseline for regression detection: recent 'code' capability scores from
    # BEFORE this run, so we can tell "this run avg" apart from history.
    baseline_scores = _code_capability_scores()

    results = [smoke_eval]
    shuffled = random.sample(active_tests, len(active_tests))
    halted_mid_run = False

    # Fully sequential: submit one test, wait for it to settle, only then
    # submit the next. A single test already cascades into several LLM calls
    # (planner decomposition, then each sub-task's executor/verifier/assess/
    # deploy), and there's only one Ollama instance behind everything with no
    # resource limits set — running tests concurrently piles up enough
    # simultaneous requests to blow past the per-call read timeout. This
    # trades total run time for not being a guaranteed source of that pile-up
    # (organic human-submitted load can still collide with a run, separately).
    print(f"\nRunning {len(shuffled)} tests sequentially...")
    task_map = {}    # task_id -> test
    result_map = {}  # task_id -> result, filled in as each test settles
    for i, test in enumerate(shuffled):
        halt = check_halted()
        if halt:
            skipped = shuffled[i:]
            print(f"\n⛔ Swarm halted mid-run ({halt.get('reason')}) — skipping remaining "
                  f"{len(skipped)} test(s) instead of submitting into a wall of 409s.")
            for remaining in skipped:
                results.append({"label": remaining["label"], "status": "SKIPPED",
                                 "reason": f"swarm halted: {halt.get('reason')}"})
            halted_mid_run = True
            break
        task_id = submit_task(test["goal"])
        if not task_id:
            results.append({"label": test["label"], "status": "FAIL", "reason": "submit failed"})
            continue
        task_map[task_id] = test
        print(f"  ✓ [{test['label']}] submitted: {task_id} — waiting for it to finish...")
        one_result = wait_for_results({task_id: test}, timeout=test["timeout"])
        if task_id in one_result:
            result_map.update(one_result)
            print(f"  … [{test['label']}] settled")
        else:
            print(f"  ⚠ [{test['label']}] timed out waiting — moving on to next test anyway")

    # Evaluate and check for first-attempt failures needing retry. Retries
    # are submitted and awaited one at a time too, same reasoning as above —
    # no concurrent Ollama load from the retry phase either. Skipped entirely
    # if the swarm is halted — a retry would just get 409'd too.
    for task_id, test in task_map.items():
        raw = result_map.get(task_id)
        r = evaluate(test, raw, attempt=1)
        if r["status"] == "PASS":
            results.append(r)
            continue

        halt = check_halted()
        if halt:
            halted_mid_run = True
            results.append({**r, "status": "SKIPPED", "reason": f"swarm halted before retry: {halt.get('reason')}"})
            continue

        if raw:
            print(f"  ✗ [{r['label']}] FAILED — triggering self-improvement: {r['issues']}")
            trigger_improvement(r["label"], r["issues"])
            retry_id = submit_task(test["goal"])
            if retry_id:
                print(f"  ✓ [{r['label']}] retry submitted: {retry_id} — waiting for it to finish...")
                retry_result = wait_for_results({retry_id: test}, timeout=test["timeout"])
                results.append(evaluate(test, retry_result.get(retry_id), attempt=2))
            else:
                # retry submission itself failed — don't silently drop the
                # first-attempt result, or it counts toward neither pass nor fail
                print(f"  ⚠️  [{r['label']}] retry submission failed — keeping first-attempt result")
                results.append(r)
        else:
            results.append(r)

    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    for r in results:
        icon = "✓" if r["status"] == "PASS" else "⊘" if r["status"] == "SKIPPED" else "✗"
        score_str = f" score={r['score']:.2f}" if "score" in r else ""
        attempt_str = f" (attempt {r['attempt']})" if r.get("attempt", 1) > 1 else ""
        print(f"{icon} {r['label']}: {r['status']}{score_str}{attempt_str}")

    skipped_count = sum(1 for r in results if r["status"] == "SKIPPED")
    if skipped_count:
        print(f"\n⛔ {skipped_count} test(s) skipped due to a mid-run halt — "
              f"these are NOT capability failures, the swarm just stopped taking work")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} passed")

    # Regression check: this run's average code-capability score vs. recent
    # history (episodic memory, same PVC agents write to) — a real drop
    # since the last reflect update is a materially different signal than
    # "didn't meet today's static threshold." Coarse-grained on purpose:
    # episodic entries are tagged by capability, not by test label, since
    # reflect only maintains the executor skill today.
    this_run_scores = _code_capability_scores()[len(baseline_scores):]
    regression = check_regression(baseline_scores, this_run_scores)
    if regression:
        print(f"\n⚠ REGRESSION: {regression}")
        if not halted_mid_run:
            trigger_improvement("REGRESSION", [regression])
    elif this_run_scores:
        print(f"\n✓ No regression detected ({len(this_run_scores)} new sample(s) this run)")

    # Post-run analysis: identify patterns and submit targeted reflect tasks.
    # Skipped entirely if halted — every trigger_improvement call below would
    # just get 409'd too, and SKIPPED entries aren't real capability failures.
    failed = [r for r in results if r["status"] not in ("PASS", "SKIPPED")]
    if failed and halted_mid_run:
        print(f"\n{'='*60}\nPOST-RUN ANALYSIS\n{'='*60}")
        print("⛔ Swarm halted mid-run — skipping reflect triggers for this run's failures "
              "(submissions would just 409). Once resumed, the next scheduled run picks this back up.")
    elif failed:
        print(f"\n{'='*60}")
        print("POST-RUN ANALYSIS")
        print(f"{'='*60}")

        # Group failures by type to identify systemic issues
        timeouts = [r for r in failed if r["status"] == "TIMEOUT"]
        low_score = [r for r in failed if r.get("score", 0) < 0.6 and r["status"] != "TIMEOUT"]
        wrong_format = [r for r in failed if any("No function" in i or "No type" in i or "Invalid YAML" in i for i in r.get("issues", []))]

        # Track which failures get a targeted trigger below via id(), so the
        # catch-all can fire for anything left over even when some *other*
        # category is non-empty — previously it only fired when ALL three
        # buckets were empty, so a genuinely uncategorized failure sitting
        # alongside a categorized one never reached trigger_improvement at all.
        handled_ids = set()

        if timeouts:
            labels = ", ".join(r["label"] for r in timeouts)
            print(f"  ⚠ Timeouts detected ({labels}) — agents may be overloaded or LLM slow")
            trigger_improvement("TIMEOUT", [f"Tests timed out: {labels}. Consider if planner is creating too many subtasks."])
            handled_ids.update(id(r) for r in timeouts)

        if wrong_format:
            labels = ", ".join(r["label"] for r in wrong_format)
            all_issues = [i for r in wrong_format for i in r.get("issues", [])]
            print(f"  ⚠ Format failures ({labels}): {all_issues[:3]}")
            trigger_improvement("FORMAT", all_issues[:3])
            handled_ids.update(id(r) for r in wrong_format)

        if low_score and not wrong_format:
            labels = ", ".join(r["label"] for r in low_score)
            print(f"  ⚠ Low quality scores ({labels}) — verifier found issues")
            issues = [i for r in low_score for i in r.get("issues", [])]
            trigger_improvement("QUALITY", issues[:3] or [f"Low scores on: {labels}"])
            handled_ids.update(id(r) for r in low_score)

        uncovered = [r for r in failed if id(r) not in handled_ids]
        for r in uncovered:
            print(f"  ⚠ {r['label']}: {r.get('reason', r['status'])}")
            trigger_improvement(r["label"], [r.get("reason", "unknown failure")])

        print(f"\n  → {len(failed)} reflect task(s) submitted — next run should improve")
    else:
        print("\n✓ All tests passed — no reflect tasks needed")

    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(results_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(results_dir, f"run_{ts}.json")
    with open(out_file, "w") as f:
        json.dump({"timestamp": ts, "tier": current_tier, "passed": passed, "total": len(results), "results": results}, f, indent=2, default=str)
    print(f"\nResults saved: tests/results/run_{ts}.json")

    # Job cleanup (kubectl delete on success) is handled by the wrapping shell
    # script in test-runner.yaml, *after* the git push step — deleting this
    # job from inside this same process raced its own trailing git push.
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()


