#!/usr/bin/env python3
"""Evaluate one staged executor skill candidate against held-out tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent_shell import KV_CANDIDATE_EVALUATIONS, KV_SKILL_CANDIDATES
from src.candidate_evaluation import select_evaluable_candidate
from src.memory import get_store
from tests.run_tests import TIER_0_TESTS, run_candidate_comparison


# Keep only deterministic validator-backed cases in the initial promotion gate.
CANDIDATE_HELD_OUT_LABELS = {
    "CODE_GENERATION",
    "ERROR_HANDLING",
    "STRUCTURED_OUTPUT",
    "TESTING",
}
CANDIDATE_HELD_OUT_TESTS = [
    test for test in TIER_0_TESTS if test["label"] in CANDIDATE_HELD_OUT_LABELS
]


def run_evaluation(
    *,
    candidate_id: str,
    candidate: dict[str, Any],
    source_attempt: dict[str, Any],
    compare: Callable[..., dict[str, Any]] = run_candidate_comparison,
    publish: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Compare one selected candidate and publish its human-review report."""
    report = compare(
        candidate_id=candidate_id,
        source_attempt=source_attempt,
        held_out_tests=CANDIDATE_HELD_OUT_TESTS,
    )
    report.update({
        "target_role": candidate["target_role"],
        "source_attempt_id": candidate["source_attempt_id"],
        "held_out_labels": sorted(CANDIDATE_HELD_OUT_LABELS),
    })
    publish(candidate_id, report)
    return report


async def _read_json_bucket(kv) -> dict[str, dict[str, Any]]:
    entries = {}
    for key in await kv.keys():
        entries[key] = json.loads((await kv.get(key)).value.decode())
    return entries


async def _load_pending_candidate() -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    import nats

    nc = await nats.connect(os.environ.get("NATS_URL", "nats://cxp-nats:4222"))
    try:
        js = nc.jetstream()
        candidates = await _read_json_bucket(await js.key_value(KV_SKILL_CANDIDATES))
        reports = await _read_json_bucket(await js.key_value(KV_CANDIDATE_EVALUATIONS))
        attempts = {attempt.get("attempt_id"): attempt for attempt in get_store().attempts}
        selected = select_evaluable_candidate(candidates=candidates, attempts=attempts, reports=reports)
        if selected is None:
            return None
        candidate_id, candidate = selected
        return candidate_id, candidate, attempts[candidate["source_attempt_id"]]
    finally:
        await nc.drain()


async def _publish_report(candidate_id: str, report: dict[str, Any]) -> None:
    import nats

    nc = await nats.connect(os.environ.get("NATS_URL", "nats://cxp-nats:4222"))
    try:
        kv = await nc.jetstream().key_value(KV_CANDIDATE_EVALUATIONS)
        await kv.put(candidate_id, json.dumps(report).encode())
    finally:
        await nc.drain()


def main() -> int:
    pending = asyncio.run(_load_pending_candidate())
    if pending is None:
        print("No healthy unevaluated skill candidate found.")
        return 0
    candidate_id, candidate, source_attempt = pending
    report = run_evaluation(
        candidate_id=candidate_id,
        candidate=candidate,
        source_attempt=source_attempt,
        publish=lambda candidate_key, value: asyncio.run(_publish_report(candidate_key, value)),
    )
    print(f"Candidate {candidate_id}: {report['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())