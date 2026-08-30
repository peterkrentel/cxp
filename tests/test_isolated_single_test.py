"""--only LABEL: trigger exactly one test for isolated, manual debugging
(e.g. tracing one request end-to-end via the web dashboard + Grafana
Tempo, see docs/otel-setup.md, with nothing else competing for the LLM).
No retry (already removed generally), no regression check, no candidate
evaluation, no write to tests/results/ -- a manual debug run shouldn't
perturb tier-promotion history or episodic-memory baselines.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import tests.run_tests as run_tests
from tests.run_tests import find_test_by_label, SMOKE_TEST, TIER_0_TESTS


def test_find_test_by_label_returns_smoke_test_case_insensitively():
    assert find_test_by_label("smoke", TIER_0_TESTS) is SMOKE_TEST
    assert find_test_by_label("SMOKE", TIER_0_TESTS) is SMOKE_TEST


def test_find_test_by_label_matches_a_capability_label_case_insensitively():
    result = find_test_by_label("code_generation", TIER_0_TESTS)
    assert result is not None
    assert result["label"] == "CODE_GENERATION"


def test_find_test_by_label_returns_none_for_unknown_label():
    assert find_test_by_label("NOT_A_REAL_LABEL", TIER_0_TESTS) is None


def test_run_isolated_test_submits_exactly_one_task_and_never_retries(monkeypatch):
    submitted = []
    triggered = []
    monkeypatch.setattr(run_tests, "check_halted", lambda: None)
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal) or "task-1")
    monkeypatch.setattr(run_tests, "wait_for_results",
                         lambda task_ids, timeout: {"task-1": {"output": "bad", "score": 0.0, "task_id": "task-1"}})
    monkeypatch.setattr(run_tests, "trigger_improvement", lambda label, issues, inputs=None: triggered.append(label))

    with pytest.raises(SystemExit):
        run_tests.run_isolated_test("SMOKE", TIER_0_TESTS)

    assert submitted == [SMOKE_TEST["goal"]]
    assert triggered == ["SMOKE"]  # a real failure still triggers reflect -- just never retries


def test_run_isolated_test_exits_cleanly_on_unknown_label(monkeypatch):
    submitted = []
    monkeypatch.setattr(run_tests, "submit_task", lambda goal, **kw: submitted.append(goal))

    with pytest.raises(SystemExit) as exc:
        run_tests.run_isolated_test("NOT_A_REAL_LABEL", TIER_0_TESTS)

    assert exc.value.code == 1
    assert submitted == []  # never submits anything for an unrecognized label
