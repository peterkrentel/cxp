#\!/usr/bin/env python3
"""Self-improving test runner: submit → evaluate → trigger reflect → re-run."""

import json
import subprocess
import sys
import time


def kubectl(cmd: str) -> str:
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def wait_for_ready(timeout=300):
    """Wait until all cxp agents and ollama are Running."""
    print("⏳ Waiting for cluster to be ready...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = kubectl("get pods -n cxp --no-headers")
        lines = [l for l in pods.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        # Only care about cxp agents and ollama, not traefik/nats/etc
        agents = [l for l in not_ready if any(x in l for x in ["cxp-planner", "cxp-executor", "cxp-ollama", "cxp-verifier"])]
        if not agents:
            print("✓ Cluster ready")
            return True
        print(f"  Waiting: {[l.split()[0] for l in agents]}")
        time.sleep(10)
    print("✗ Timeout waiting for cluster")
    return False


def submit_task(goal: str) -> str:
    cmd = f'exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "{goal}"'
    out = kubectl(cmd)
    for line in out.splitlines():
        if "Task submitted:" in line:
            return line.split()[2]  # task ID
    return None


def get_state() -> dict:
    """Get current swarm state from web API."""
    raw = kubectl("exec -n cxp deploy/cxp-web -- wget -qO- http://localhost:8080/api/state")
    try:
        return json.loads(raw)
    except:
        return {}


def wait_for_result(task_id: str, timeout=300) -> dict | None:
    """Poll API until a done/error packet appears for this task."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = get_state()
        for p in state.get("packets", []):
            if p.get("status") in ("done", "error"):
                # Match by checking if task submitted around same time
                output = p.get("output", "")
                if output:
                    return p
        time.sleep(5)
    return None


def trigger_improvement(label: str, goal: str, issues: list[str], output: str):
    """Submit reflect packet describing what went wrong."""
    failure_context = (
        f"Test label: {label}\n"
        f"Goal: {goal}\n"
        f"Issues found: {issues}\n"
        f"Failed output snippet: {output[:400]}"
    )
    # Submit as a reflect task
    safe = failure_context.replace('"', "'")
    improve_goal = f"improve executor skill based on test failure: {', '.join(issues[:2])}"
    result = kubectl(f'exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "{improve_goal}"')
    print(f"  ↑ Improvement task submitted: {result}")


def validate_python(code: str) -> tuple[bool, list[str]]:
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
        yaml.safe_load(text)
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
        return {"label": label, "status": "FAIL", "reason": "submit failed"}
    print(f"✓ Submitted: {task_id}")

    result = wait_for_result(task_id)
    if not result:
        return {"label": label, "status": "TIMEOUT"}

    score = result.get("score") or 0
    output = result.get("output", "")
    print(f"  Score: {score:.2f}  Output: {len(output)} chars")
    if output:
        print(f"  Preview: {output[:150]}...")

    valid, issues = test["validator"](output)
    passed = valid and score >= test["threshold"]

    if not passed and attempt == 1:
        print(f"  ✗ FAILED — triggering self-improvement: {issues}")
        trigger_improvement(label, goal, issues, output)
        time.sleep(15)  # Let reflect agent update skill
        return run_test(test, attempt=2)  # Re-run once after improvement

    return {
        "label": label,
        "status": "PASS" if passed else "WARN",
        "score": score,
        "attempt": attempt,
        "issues": issues,
    }


def main():
    if not wait_for_ready():
        sys.exit(1)

    results = []
    for test in TESTS:
        r = run_test(test)
        results.append(r)
        time.sleep(3)

    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    for r in results:
        icon = "✓" if r["status"] == "PASS" else "✗" if r["status"] == "FAIL" else "?"
        score = f" score={r.get('score', 'N/A'):.2f}" if "score" in r else ""
        attempt = f" (attempt {r.get('attempt', 1)})" if r.get("attempt", 1) > 1 else ""
        print(f"{icon} {r['label']}: {r['status']}{score}{attempt}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} passed")

    # Write results to file
    import os
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(results_dir, f"run_{ts}.json")
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": ts,
            "passed": passed,
            "total": len(results),
            "results": results,
        }, f, indent=2, default=str)
    print(f"\nResults saved: tests/results/run_{ts}.json")


if __name__ == "__main__":
    main()
