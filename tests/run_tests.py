#!/usr/bin/env python3
"""Self-improving test runner: submit → evaluate → trigger reflect → re-run."""

import json
import os
import random
import sys
import time
import urllib.request

API = os.environ.get("CXP_WEB_API", "http://cxp-web:8080")


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


def wait_for_results(task_ids: dict, timeout=480) -> dict:
    """Poll until all tasks have code+verify done, or timeout. Returns {task_id: result}.

    Also tracks code_count (how many sub-tasks the planner actually spawned —
    for DECOMPOSITION) and verify_issues (accumulated issues text from every
    verify packet on the task — for SECURITY_AWARENESS, which cares whether
    the verifier *flagged* something, not just whether code compiled).
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
            verify_issues: list[str] = []
            for p in packets:
                if p.get("task_id") != task_id:
                    continue
                if p.get("type") == "code":
                    code_count += 1
                    if p.get("status") == "done" and p.get("output"):
                        code_pkt = p
                if p.get("type") == "verify" and p.get("status") == "done":
                    best_score = max(best_score, p.get("score") or 0.0)
                    try:
                        verify_issues.extend(json.loads(p.get("output") or "{}").get("issues", []))
                    except Exception:
                        pass
            if code_pkt:
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


def validate_python(code: str) -> tuple[bool, list[str]]:
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        issues.append(f"SyntaxError: {e}")
        return False, issues
    if "def " not in code:
        issues.append("No function definition found")
    if ":" not in code:
        issues.append("No type hints found")
    return len(issues) == 0, issues


def validate_yaml(text: str) -> tuple[bool, list[str]]:
    try:
        import yaml
        yaml.safe_load(_strip_markdown(text))
        return True, []
    except Exception as e:
        return False, [f"Invalid YAML: {e}"]


def validate_infra_yaml(text: str) -> tuple[bool, list[str]]:
    """STRUCTURED_OUTPUT checks *valid* YAML; this checks *specific keys*
    exist, matching tests/examples.md's INFRA_AS_CODE expectations."""
    try:
        import yaml
        doc = yaml.safe_load(_strip_markdown(text))
    except Exception as e:
        return False, [f"Invalid YAML: {e}"]
    if not isinstance(doc, dict):
        return False, ["YAML did not parse to a mapping"]
    flat = str(doc).lower()
    issues = [f"Missing '{key}' config" for key in ("persistence", "auth", "sentinel", "resources") if key not in flat]
    return len(issues) == 0, issues


def validate_always(_artifact: str) -> tuple[bool, list[str]]:
    """For tests whose real check isn't the artifact text itself — see
    min_subtasks (DECOMPOSITION) and required_issue_keywords (SECURITY_AWARENESS)
    in evaluate() below."""
    return True, []


def validate_has_tests(code: str) -> tuple[bool, list[str]]:
    """TESTING: does the artifact include actual test code, not just the
    function it's meant to test?"""
    code = _strip_markdown(code)
    issues = []
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    if "def test_" not in code and code.count("assert ") < 2:
        issues.append("No dedicated test function or assertions found")
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


TESTS = [
    {
        "label": "CODE_GENERATION",
        "goal": "write a Python function that adds two numbers with type hints and docstring",
        "validator": validate_python,
        "threshold": 0.75,
    },
    {
        "label": "ERROR_HANDLING",
        "goal": "write a Python function that reads a JSON file and returns a dict, handling FileNotFoundError and JSONDecodeError",
        "validator": validate_python,
        "threshold": 0.75,
    },
    {
        "label": "STRUCTURED_OUTPUT",
        "goal": "generate a Kubernetes Deployment manifest for a Node.js API with resource limits",
        "validator": validate_yaml,
        "threshold": 0.70,
    },
    {
        "label": "DECOMPOSITION",
        "goal": "scaffold a complete Python microservice with FastAPI, Postgres, Docker Compose, tests, and README",
        "validator": validate_always,
        "threshold": 0.0,   # score isn't the point — sub-task count is
        "min_subtasks": 3,  # examples.md says 5+; relaxed for the small local planner model
    },
    {
        "label": "SECURITY_AWARENESS",
        "goal": "generate a Python web scraper that downloads URLs from user input and saves to disk",
        "validator": validate_always,
        "threshold": 0.0,   # pass = verifier correctly flagged a risk, not a high score
        "required_issue_keywords": ["url", "valid", "path", "travers", "rate limit", "sanitiz"],
    },
    {
        "label": "INFRA_AS_CODE",
        "goal": "generate a Helm values.yaml for a production Redis cluster with persistence, auth, sentinel, and resource limits",
        "validator": validate_infra_yaml,
        "threshold": 0.70,
    },
    {
        "label": "TESTING",
        "goal": "write a Python function that calculates the factorial of a number, plus unit tests covering zero, one, and a typical positive input",
        "validator": validate_has_tests,
        "threshold": 0.70,
    },
    {
        "label": "DOCUMENTATION",
        "goal": "write a Python function for binary search over a sorted list, with a comprehensive docstring covering parameters, return value, and an example usage",
        "validator": validate_has_docstring,
        "threshold": 0.70,
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

    if "min_subtasks" in test:
        code_count = result.get("code_count", 0)
        if code_count < test["min_subtasks"]:
            valid = False
            issues = issues + [f"Only {code_count} sub-task(s) spawned, expected >= {test['min_subtasks']}"]

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

    results = []
    shuffled = random.sample(TESTS, len(TESTS))

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
    for test in shuffled:
        task_id = submit_task(test["goal"])
        if not task_id:
            results.append({"label": test["label"], "status": "FAIL", "reason": "submit failed"})
            continue
        task_map[task_id] = test
        print(f"  ✓ [{test['label']}] submitted: {task_id} — waiting for it to finish...")
        one_result = wait_for_results({task_id: test}, timeout=480)
        if task_id in one_result:
            result_map.update(one_result)
            print(f"  … [{test['label']}] settled")
        else:
            print(f"  ⚠ [{test['label']}] timed out waiting — moving on to next test anyway")

    # Evaluate and check for first-attempt failures needing retry. Retries
    # are submitted and awaited one at a time too, same reasoning as above —
    # no concurrent Ollama load from the retry phase either.
    for task_id, test in task_map.items():
        raw = result_map.get(task_id)
        r = evaluate(test, raw, attempt=1)
        if r["status"] != "PASS" and raw:
            print(f"  ✗ [{r['label']}] FAILED — triggering self-improvement: {r['issues']}")
            trigger_improvement(r["label"], r["issues"])
            retry_id = submit_task(test["goal"])
            if retry_id:
                print(f"  ✓ [{r['label']}] retry submitted: {retry_id} — waiting for it to finish...")
                retry_result = wait_for_results({retry_id: test}, timeout=480)
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
        icon = "✓" if r["status"] == "PASS" else "✗"
        score_str = f" score={r['score']:.2f}" if "score" in r else ""
        attempt_str = f" (attempt {r['attempt']})" if r.get("attempt", 1) > 1 else ""
        print(f"{icon} {r['label']}: {r['status']}{score_str}{attempt_str}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} passed")

    # Post-run analysis: identify patterns and submit targeted reflect tasks
    failed = [r for r in results if r["status"] not in ("PASS",)]
    if failed:
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


