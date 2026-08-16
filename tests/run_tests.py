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
    """Poll until all tasks have code+verify done, or timeout. Returns {task_id: result}."""
    deadline = time.time() + timeout
    results = {}
    pending = set(task_ids.keys())
    while time.time() < deadline and pending:
        packets = get_state().get("packets", [])
        for task_id in list(pending):
            code_pkt = None
            best_score = 0.0
            for p in packets:
                if p.get("task_id") != task_id:
                    continue
                if p.get("type") == "code" and p.get("status") == "done" and p.get("output"):
                    code_pkt = p
                if p.get("type") == "verify" and p.get("status") == "done":
                    best_score = max(best_score, p.get("score") or 0.0)
            if code_pkt:
                results[task_id] = {**code_pkt, "score": best_score}
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
]


def evaluate(test: dict, result: dict | None, attempt: int = 1) -> dict:
    label = test["label"]
    if not result:
        return {"label": label, "status": "TIMEOUT"}
    score = result.get("score") or 0
    output = result.get("output", "")
    print(f"  [{label}] score={score:.2f}  {len(output)} chars")
    valid, issues = test["validator"](output)
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

    # Submit all tests upfront so agents work in parallel
    print("\nSubmitting all tests...")
    task_map = {}  # task_id -> test
    for test in shuffled:
        task_id = submit_task(test["goal"])
        if task_id:
            task_map[task_id] = test
            print(f"  ✓ [{test['label']}] submitted: {task_id}")
        else:
            results.append({"label": test["label"], "status": "FAIL", "reason": "submit failed"})

    # Wait for all results together
    print(f"\n⏳ Waiting for {len(task_map)} task(s)...")
    result_map = wait_for_results(task_map, timeout=480)

    # Evaluate and check for first-attempt failures needing retry
    retry_map = {}
    for task_id, test in task_map.items():
        raw = result_map.get(task_id)
        r = evaluate(test, raw, attempt=1)
        if r["status"] != "PASS" and raw:
            print(f"  ✗ [{r['label']}] FAILED — triggering self-improvement: {r['issues']}")
            trigger_improvement(r["label"], r["issues"])
            retry_id = submit_task(test["goal"])
            if retry_id:
                retry_map[retry_id] = (test, r)
            else:
                # retry submission itself failed — don't silently drop the
                # first-attempt result, or it counts toward neither pass nor fail
                print(f"  ⚠️  [{r['label']}] retry submission failed — keeping first-attempt result")
                results.append(r)
        else:
            results.append(r)

    # Wait for retries together
    if retry_map:
        print(f"\n⏳ Waiting for {len(retry_map)} retry(s)...")
        time.sleep(10)
        retry_results = wait_for_results({tid: t for tid, (t, _) in retry_map.items()}, timeout=480)
        for retry_id, (test, first_result) in retry_map.items():
            raw = retry_results.get(retry_id)
            r = evaluate(test, raw, attempt=2)
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


