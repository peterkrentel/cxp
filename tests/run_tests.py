#!/usr/bin/env python3
"""Self-improving test runner: submit → evaluate → trigger reflect → re-run."""

import json
import os
import random
import sys
import time
import urllib.request

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


def submit_task(goal: str) -> str | None:
    resp = _http_post("/api/submit", {"goal": goal})
    return resp.get("task_id")


def get_state() -> dict:
    return _http_get("/api/state")


def check_halted() -> dict | None:
    """Is the swarm currently halted? Checked before every submission —
    without this, a halt mid-run cascades into every remaining submission
    getting rejected with 409, which previously got reported as plain FAIL
    (looks like a capability regression) instead of "never got to run"."""
    return get_state().get("halt")


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
            for p in packets:
                if p.get("task_id") != task_id:
                    continue
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


# Tier 0 — pipeline health check, always run first, never shuffled with the
# rest. A failure here means "the swarm is broken," a distinct and more
# urgent signal than "the swarm is bad at capability X."
SMOKE_TEST = {
    "label": "SMOKE",
    "goal": "write a Python one-liner that prints 'hello world'",
    "validator": validate_smoke,
    "threshold": 0.3,
    "timeout": 900,
}

# Tier 1 — capability coverage, one per assessor label (8/9; SELF_IMPROVEMENT
# doesn't fit the pass/fail shape).
TESTS = [
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


def evaluate(test: dict, result: dict | None, attempt: int = 1) -> dict:
    label = test["label"]
    if not result:
        return {"label": label, "status": "TIMEOUT"}
    score = result.get("score") or 0
    output = result.get("output", "")
    print(f"  [{label}] score={score:.2f}  {len(output)} chars")
    valid, issues = test["validator"](output)

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

    # Tier 0: is the pipeline even working? Run before anything else, and
    # treat a failure as a distinct, more urgent signal than a capability
    # test failing — no point testing 8 capabilities if the plumbing's down.
    # Timeout is test-specific (SMOKE_TEST["timeout"], currently 900s),
    # derived from measured pipeline latency (120s median / 438s P90 across
    # 75 real hop-to-hop transitions) rather than a guessed flat value. Smoke
    # is always the FIRST request of every run, so it's the one most likely
    # to catch Ollama cold (model unloaded since the last cycle) — a short
    # timeout paired with the worst timing made it the most fragile test,
    # not the most forgiving one, which is backwards for a health check.
    print("\nRunning smoke test (pipeline health check)...")
    smoke_task_id = submit_task(SMOKE_TEST["goal"])
    smoke_result = wait_for_results({smoke_task_id: SMOKE_TEST}, timeout=SMOKE_TEST["timeout"]) if smoke_task_id else {}
    smoke_eval = evaluate(SMOKE_TEST, smoke_result.get(smoke_task_id), attempt=1)
    if smoke_eval["status"] != "PASS":
        print(f"  ⚠ SMOKE FAILED ({smoke_eval['status']}) — the pipeline itself looks broken, "
              f"not just a specific capability. Running the rest of the suite anyway for more signal.")
    else:
        print("  ✓ SMOKE passed — pipeline is up")

    # Baseline for regression detection: recent 'code' capability scores from
    # BEFORE this run, so we can tell "this run avg" apart from history.
    baseline_scores = _code_capability_scores()

    results = [smoke_eval]
    shuffled = random.sample(TESTS, len(TESTS))
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
        json.dump({"timestamp": ts, "passed": passed, "total": len(results), "results": results}, f, indent=2, default=str)
    print(f"\nResults saved: tests/results/run_{ts}.json")

    # Job cleanup (kubectl delete on success) is handled by the wrapping shell
    # script in test-runner.yaml, *after* the git push step — deleting this
    # job from inside this same process raced its own trailing git push.
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()


