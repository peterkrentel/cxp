#!/usr/bin/env python3
"""Check whether the fixed test suite has plateaued: N consecutive runs
where every Tier-1 capability test passed. Reads tests/results/*.json —
the git-tracked run history, not something any single CronJob run can see
on its own (each pod starts from a fresh emptyDir with no prior results).

Usage: python3 check_plateau.py [results_dir]  (defaults to tests/results)
"""
import json
import os
import sys

STREAK_TARGET = 10  # consecutive clean runs before calling it "exhausted"
CAPABILITY_LABELS = {
    "CODE_GENERATION", "ERROR_HANDLING", "STRUCTURED_OUTPUT", "DECOMPOSITION",
    "SECURITY_AWARENESS", "INFRA_AS_CODE", "TESTING", "DOCUMENTATION",
}


def is_clean_run(run: dict) -> bool | None:
    """True if every Tier-1 capability test passed. None if this run
    predates the current 8-test suite (missing labels) or was aborted
    mid-run (e.g. SKIPPED entries from a halt) — either way, not a
    meaningful data point either for or against the streak."""
    by_label = {r["label"]: r for r in run.get("results", [])}
    if not CAPABILITY_LABELS.issubset(by_label.keys()):
        return None
    return all(by_label[label].get("status") == "PASS" for label in CAPABILITY_LABELS)


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "tests/results"
    if not os.path.isdir(results_dir):
        print(f"No results directory at {results_dir} — nothing to check yet.")
        return
    files = sorted(f for f in os.listdir(results_dir) if f.startswith("run_") and f.endswith(".json"))
    if not files:
        print("No run history found — nothing to check yet.")
        return

    streak = 0
    for fname in reversed(files):  # most recent first
        with open(os.path.join(results_dir, fname)) as f:
            run = json.load(f)
        clean = is_clean_run(run)
        if clean is None:
            print(f"  (skipping {fname} — not a full 8/8-labeled clean run, e.g. halted or pre-dates current suite)")
            continue
        if clean:
            streak += 1
        else:
            break

    print(f"\nConsecutive clean (8/8 pass) runs: {streak} / {STREAK_TARGET}")
    if streak >= STREAK_TARGET:
        print("✓ FINISH LINE REACHED — the fixed suite has plateaued. Time to consider tier 3 "
              "(harder tests) instead of the CronJob continuing to just confirm the same thing.")
    else:
        print(f"Not there yet — {STREAK_TARGET - streak} more consecutive clean run(s) needed.")


if __name__ == "__main__":
    main()
