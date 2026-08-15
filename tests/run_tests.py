#!/usr/bin/env python3
"""Test runner for CXP — submit tasks, collect results, validate outputs."""

import json
import subprocess
import sys
import time
from pathlib import Path


def run_cmd(cmd: str) -> str:
    """Execute kubectl command, return output."""
    full_cmd = f"kubectl {cmd}"
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return ""
    return result.stdout.strip()


def submit_task(goal: str) -> str:
    """Submit a task, return task_id."""
    cmd = f'exec -n cxp deploy/cxp-dashboard -- python /app/main.py submit "{goal}"'
    output = run_cmd(cmd)
    # Extract task ID from "Task submitted: <id>"
    for line in output.split("\n"):
        if "Task submitted:" in line:
            return line.split()[-1]
    return None


def get_memory() -> dict:
    """Fetch memory.json from cluster."""
    cmd = "exec -n cxp deploy/cxp-dashboard -- cat /data/memory.json"
    output = run_cmd(cmd)
    try:
        return json.loads(output)
    except:
        return {}


def get_task_result(task_id: str, memory: dict) -> dict:
    """Get result packet for a task."""
    for packet in memory.get("packets", []):
        if packet.get("task_id") == task_id:
            return packet
    return None


def validate_python_code(code: str) -> tuple[bool, str]:
    """Check if code is valid Python."""
    try:
        compile(code, "<string>", "exec")
        return True, "Valid Python"
    except SyntaxError as e:
        return False, f"Syntax error: {e}"


def validate_yaml(yaml_str: str) -> tuple[bool, str]:
    """Check if string is valid YAML."""
    try:
        import yaml
        yaml.safe_load(yaml_str)
        return True, "Valid YAML"
    except Exception as e:
        return False, f"YAML error: {e}"


def run_test(name: str, goal: str, validator) -> dict:
    """Submit task, wait for result, validate."""
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"GOAL: {goal}")
    print(f"{'='*60}")
    
    # Submit
    task_id = submit_task(goal)
    if not task_id:
        return {"name": name, "status": "FAIL", "reason": "Submit failed"}
    
    print(f"✓ Task submitted: {task_id}")
    
    # Wait for completion
    for attempt in range(30):  # 5 min timeout
        time.sleep(10)
        memory = get_memory()
        result = get_task_result(task_id, memory)
        
        if result and result.get("status") == "done":
            score = result.get("payload", {}).get("score", 0)
            output = result.get("payload", {}).get("output", "")
            
            print(f"✓ Task completed, score: {score:.2f}")
            
            # Validate
            if validator:
                valid, msg = validator(output)
                print(f"  Validation: {msg}")
                return {
                    "name": name,
                    "status": "PASS" if valid and score > 0.7 else "WARN",
                    "score": score,
                    "valid": valid,
                    "message": msg,
                }
            else:
                return {
                    "name": name,
                    "status": "PASS" if score > 0.7 else "WARN",
                    "score": score,
                }
    
    return {"name": name, "status": "TIMEOUT", "reason": "No result after 5 min"}


def main():
    tests = [
        ("Simple Python Function", 
         "generate a Python function that adds two numbers",
         validate_python_code),
        
        ("Python with Error Handling",
         "generate a Python function for reading JSON files with error handling",
         validate_python_code),
        
        ("Kubernetes YAML",
         "generate a Kubernetes Deployment for a simple web server",
         validate_yaml),
    ]
    
    results = []
    for name, goal, validator in tests:
        result = run_test(name, goal, validator)
        results.append(result)
        time.sleep(5)  # Stagger requests
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        status = r["status"]
        icon = "✓" if status == "PASS" else "✗" if status == "FAIL" else "?"
        score = f" (score: {r.get('score', 'N/A'):.2f})" if "score" in r else ""
        print(f"{icon} {r['name']}: {status}{score}")
    
    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\nPassed: {passed}/{len(results)}")


if __name__ == "__main__":
    main()
