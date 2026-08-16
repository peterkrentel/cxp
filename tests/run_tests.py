#!/usr/bin/env python3
"""Self-improving test runner: submit → evaluate → trigger reflect → re-run."""

import json
import os
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


def wait_for_result(task_id: str, timeout=300) -> dict | None:
    """Poll API until code+verify packets both land for this task_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code_pkt = None
        best_score = 0.0
        for p in get_state().get("packets", []):
            if p.get("task_id") != task_id:
                continue
            if p.get("type") == "code" and p.get("status") == "done" and p.get("output"):
                code_pkt = p
            if p.get("type") == "verify" and p.get("status") == "done":
                best_score = max(best_score, p.get("score") or 0.0)
        if code_pkt:
            return {**code_pkt, "score": best_score}
        time.sleep(5)
    return None


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


def run_test(test: dict, attempt: int = 1) -> dict:
    label = test["label"]
    goal = test["goal"]
    print(f"\n{'='*60}")
    print(f"[{label}] attempt {attempt}")
    print(f"Goal: {goal}")
    print(f"{'='*60}")

    task_id = submit_task(goal)
    if not task_id:
        print("  ✗ Submission failed")
        return {"label": label, "status": "FAIL", "reason": "submit failed"}
    print(f"  ✓ Submitted: {task_id}")

    result = wait_for_result(task_id)
    if not result:
        return {"label": label, "status": "TIMEOUT", "task_id": task_id}

    score = result.get("score") or 0
    output = result.get("output", "")
    print(f"  Score: {score:.2f}  Output: {len(output)} chars")
    if output:
        print(f"  Preview: {output[:150]}...")

    valid, issues = test["validator"](output)
    passed = valid and score >= test["threshold"]

    if not passed and attempt == 1:
        print(f"  ✗ FAILED — triggering self-improvement: {issues}")
        trigger_improvement(label, issues)
        time.sleep(15)
        return run_test(test, attempt=2)

    return {
        "label": label,
        "status": "PASS" if passed else "WARN",
        "score": score,
        "attempt": attempt,
        "issues": issues,
    }


def main():
    print("[TRACE] Entering main()", flush=True)
    sys.stdout.flush()
    if not wait_for_ready():
        print("[TRACE] wait_for_ready() returned False, exiting", flush=True)
        sys.exit(1)
    print("[TRACE] wait_for_ready() succeeded", flush=True)

    results = []
    for test in TESTS:
        r = run_test(test)
        results.append(r)
        time.sleep(3)

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

    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(results_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(results_dir, f"run_{ts}.json")
    with open(out_file, "w") as f:
        json.dump({"timestamp": ts, "passed": passed, "total": len(results), "results": results}, f, indent=2, default=str)
    print(f"\nResults saved: tests/results/run_{ts}.json")


if __name__ == "__main__":
    main()


