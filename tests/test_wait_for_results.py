"""wait_for_results() -- found live 2026-08-18: its exit condition only
checked `if code_pkt`, never whether verify had actually run. Since verify
always runs strictly after code, this meant the function reliably exited
the instant code finished, capturing best_score=0.0 not because anything
scored badly but because verify hadn't started yet. A smoke test with
perfectly valid code ("print('hello world')") failed this way live --
score=0.00 despite the artifact being fine."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import wait_for_results


def test_does_not_settle_on_code_alone_before_verify_exists(monkeypatch):
    """Regression test for the exact live bug: code done, verify not yet
    started, must NOT be treated as settled."""
    import tests.run_tests as rt

    call_count = {"n": 0}

    def fake_get_state():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First poll: only code has finished. This is exactly the
            # moment the old code incorrectly returned a result.
            return {"packets": [
                {"task_id": "t1", "type": "code", "status": "done", "output": "print('hello world')"},
            ]}
        # Second poll: verify has now also finished.
        return {"packets": [
            {"task_id": "t1", "type": "code", "status": "done", "output": "print('hello world')"},
            {"task_id": "t1", "type": "verify", "status": "done", "score": 0.9, "output": "{}"},
        ]}

    monkeypatch.setattr(rt, "get_state", fake_get_state)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    results = wait_for_results({"t1": {}}, timeout=10)

    assert "t1" in results
    assert results["t1"]["score"] == 0.9
    # Proves it didn't settle on the first (code-only) poll -- it had to
    # poll again and see verify before returning.
    assert call_count["n"] >= 2


def test_a_genuine_zero_score_is_not_confused_with_verify_never_ran(monkeypatch):
    """The other half of the fix: verify_seen is tracked separately from
    best_score, so a real score of 0.0 from the verifier still settles
    immediately instead of waiting for the full timeout."""
    import tests.run_tests as rt

    def fake_get_state():
        return {"packets": [
            {"task_id": "t1", "type": "code", "status": "done", "output": "garbage"},
            {"task_id": "t1", "type": "verify", "status": "done", "score": 0.0, "output": "{}"},
        ]}

    monkeypatch.setattr(rt, "get_state", fake_get_state)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    results = wait_for_results({"t1": {}}, timeout=10)

    assert "t1" in results
    assert results["t1"]["score"] == 0.0
