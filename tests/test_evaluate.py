"""evaluate() -- zero test coverage until now. Found live 2026-08-19: a SMOKE
result printed 'score=0.00' with no way to tell, after the fact, whether the
verifier genuinely scored the artifact 0.0 or the score field was simply
missing (e.g. an upstream parsing failure) -- `result.get("score") or 0`
collapses both cases into the same printed value and the same returned
dict, silently."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import evaluate


def _test(threshold=0.5):
    return {"label": "EXAMPLE", "validator": lambda output: (True, []), "threshold": threshold}


def test_evaluate_returns_timeout_when_no_result():
    r = evaluate(_test(), None)
    assert r["status"] == "TIMEOUT"


def test_evaluate_flags_a_missing_score_distinctly_from_a_genuine_zero():
    missing = evaluate(_test(), {"output": "x"})  # no "score" key at all
    genuine_zero = evaluate(_test(), {"output": "x", "score": 0.0})

    assert missing["score_was_missing"] is True
    assert genuine_zero["score_was_missing"] is False
    # both still report score=0.0 for threshold purposes, but the *reason*
    # is now distinguishable via score_was_missing and the issues list
    assert missing["score"] == 0.0
    assert genuine_zero["score"] == 0.0
    assert any("missing" in i.lower() for i in missing["issues"])
    assert not any("missing" in i.lower() for i in genuine_zero["issues"])


def test_evaluate_passes_with_a_genuine_score_at_or_above_threshold():
    r = evaluate(_test(threshold=0.5), {"output": "x", "score": 0.75})
    assert r["status"] == "PASS"
    assert r["score_was_missing"] is False


def test_evaluate_fails_below_threshold_with_a_genuine_score():
    r = evaluate(_test(threshold=0.5), {"output": "x", "score": 0.2})
    assert r["status"] == "WARN"


def test_evaluate_reports_a_distinct_planner_failed_status_not_a_bare_timeout():
    # wait_for_results() now settles a task immediately (instead of running
    # out the full timeout) when the planner completed but spawned zero
    # sub-tasks -- see its decomposition_failed branch. evaluate() must
    # surface that as a named, explained status, not fold it into the
    # generic "no result at all" TIMEOUT case.
    result = {
        "task_id": "t1",
        "decomposition_failed": True,
        "output": "Failed to decompose task t1: malformed JSON from model (...). No sub-tasks spawned.",
        "score": 0.0,
        "code_count": 0,
        "verify_issues": [],
    }
    r = evaluate(_test(), result)
    assert r["status"] == "PLANNER_FAILED"
    assert "malformed JSON" in r["reason"]
    assert any("malformed JSON" in i for i in r["issues"])


def test_evaluate_required_issue_keywords_still_works_alongside_the_fix():
    test = {
        "label": "EXAMPLE", "validator": lambda output: (True, []), "threshold": 0.0,
        "required_issue_keywords": ["path traversal"],
    }
    r = evaluate(test, {"output": "x", "score": 1.0, "verify_issues": ["looks fine"]})
    assert r["status"] == "WARN"
    assert any("didn't flag" in i for i in r["issues"])
