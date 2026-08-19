#!/usr/bin/env python3
"""Check whether the currently-active tier has plateaued: N consecutive runs
where every capability test at that tier passed. Reads tests/results/*.json —
the git-tracked run history, not something any single CronJob run can see
on its own (each pod starts from a fresh emptyDir with no prior results).

Usage: python3 check_plateau.py [results_dir]  (defaults to tests/results)
"""
import json
import os
import sys

# The CronJob invokes this as a bare script from the repo root
# (`python3 tests/check_plateau.py tests/results`, wrapped in `|| true`).
# In that mode Python puts only this script's OWN directory on sys.path,
# not the repo root, so `import tests.run_tests` fails and -- because of the
# `|| true` -- would fail *silently* on every run. Same failure shape as the
# git-push bug this project already hit once. Fixing at the source rather
# than assuming the caller's invocation style, so this works regardless.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# TIERS is imported from tests.run_tests: TIERS = [TIER_0_TESTS, TIER_1_TESTS, TIER_2_TESTS]
# All 8 labels are the same at every tier -- only goal difficulty changes -- so the
# label set can be read from tier 0 rather than repeated per tier.
from tests.run_tests import TIERS

STREAK_TARGET = 10  # consecutive clean runs before calling it "exhausted"
TIER_LABELS = {label["label"] for label in TIERS[0]}


def is_clean_run(run: dict, tier: int) -> bool | None:
    """True if every capability test at `tier` passed. None if this run is
    from a different tier, or predates the current 8-test suite (missing
    labels), or was aborted mid-run (e.g. SKIPPED entries from a halt) --
    either way, not a meaningful data point either for or against the streak."""
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
    defined tier) even if it also plateaus -- adding a harder next tier
    at that point is a deliberate decision, not something this function
    should silently invent."""
    if not os.path.isdir(results_dir):
        return 0
    tier = 0
    while tier < len(TIERS) - 1 and streak_for_tier(results_dir, tier) >= STREAK_TARGET:
        tier += 1
    return tier


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
