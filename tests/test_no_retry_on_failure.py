"""A failed test must never trigger a blind retry of the same goal.

reflect only ever stages a KV candidate -- it never rewrites the active
skill live (see tests/unit/test_skill_candidates.py: "Candidate skill
revisions must be isolated from active skills"). An immediate retry
submitted right after trigger_improvement() therefore runs against the
exact same, unchanged skill as the first attempt -- it cannot possibly
reflect any improvement, and only adds a second full plan->code->verify
chain of LLM calls for zero signal. handle_test_outcome() must still
trigger reflect (the legitimate self-improvement path), just never retry.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.run_tests as run_tests
from tests.run_tests import handle_test_outcome


def _test(threshold=0.5):
    return {
        "label": "EXAMPLE",
        "goal": "do the thing",
        "validator": lambda output: (False, ["broken"]),
        "threshold": threshold,
        "timeout": 10,
    }


def test_failed_test_does_not_retry_but_still_triggers_reflect(monkeypatch):
    submitted, triggered = [], []
    monkeypatch.setattr(run_tests, "check_halted", lambda: None)
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal))
    monkeypatch.setattr(run_tests, "trigger_improvement", lambda label, issues, inputs=None: triggered.append(label))

    raw = {"output": "bad", "score": 0.9, "task_id": "task-1"}
    result = handle_test_outcome("task-1", _test(), raw)

    assert submitted == [], "retry must never be submitted -- reflect only stages a candidate, can't fix this attempt"
    assert triggered == ["EXAMPLE"]
    assert result["status"] == "WARN"
    assert result.get("attempt", 1) == 1


def test_timeout_does_not_trigger_reflect_or_retry(monkeypatch):
    submitted, triggered = [], []
    monkeypatch.setattr(run_tests, "check_halted", lambda: None)
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal))
    monkeypatch.setattr(run_tests, "trigger_improvement", lambda label, issues, inputs=None: triggered.append(label))

    result = handle_test_outcome("task-1", _test(), None)

    assert triggered == []
    assert submitted == []
    assert result["status"] == "TIMEOUT"


def test_halt_marks_skipped_without_retry_or_reflect(monkeypatch):
    submitted, triggered = [], []
    monkeypatch.setattr(run_tests, "check_halted", lambda: {"reason": "boom"})
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal))
    monkeypatch.setattr(run_tests, "trigger_improvement", lambda label, issues, inputs=None: triggered.append(label))

    raw = {"output": "bad", "score": 0.9, "task_id": "task-1"}
    result = handle_test_outcome("task-1", _test(), raw)

    assert result["status"] == "SKIPPED"
    assert triggered == []
    assert submitted == []


def test_passing_test_does_not_trigger_reflect(monkeypatch):
    submitted, triggered = [], []
    monkeypatch.setattr(run_tests, "check_halted", lambda: None)
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal))
    monkeypatch.setattr(run_tests, "trigger_improvement", lambda label, issues, inputs=None: triggered.append(label))

    test = {**_test(), "validator": lambda output: (True, [])}
    raw = {"output": "good", "score": 0.9, "task_id": "task-1"}
    result = handle_test_outcome("task-1", test, raw)

    assert result["status"] == "PASS"
    assert triggered == []
    assert submitted == []
