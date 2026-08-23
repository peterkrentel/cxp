"""Pure promotion recommendations for staged skill candidates."""

from __future__ import annotations

from src.candidate_evaluation import evaluate_candidate, resolve_source_attempt, select_evaluable_candidate


def _result(label: str, status: str) -> dict:
    return {"label": label, "status": status}


def test_candidate_rejects_platform_unhealthy_source_attempt():
    report = evaluate_candidate(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": False},
        baseline_results=[_result("CODE_GENERATION", "WARN")],
        candidate_results=[_result("CODE_GENERATION", "PASS")],
    )

    assert report["recommendation"] == "reject_platform_unhealthy"
    assert report["eligible"] is False


def test_candidate_requires_held_out_results():
    report = evaluate_candidate(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        baseline_results=[],
        candidate_results=[],
    )

    assert report["recommendation"] == "insufficient_evidence"
    assert report["eligible"] is False


def test_candidate_rejects_regression_on_a_previously_passing_case():
    report = evaluate_candidate(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        baseline_results=[
            _result("CODE_GENERATION", "PASS"),
            _result("ERROR_HANDLING", "WARN"),
        ],
        candidate_results=[
            _result("CODE_GENERATION", "WARN"),
            _result("ERROR_HANDLING", "PASS"),
        ],
    )

    assert report["recommendation"] == "reject_regression"
    assert report["regressions"] == ["CODE_GENERATION"]


def test_candidate_recommends_promotion_for_held_out_improvement_without_regression():
    report = evaluate_candidate(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        baseline_results=[
            _result("CODE_GENERATION", "WARN"),
            _result("ERROR_HANDLING", "PASS"),
        ],
        candidate_results=[
            _result("CODE_GENERATION", "PASS"),
            _result("ERROR_HANDLING", "PASS"),
        ],
    )

    assert report["recommendation"] == "recommend_promotion"
    assert report["eligible"] is True
    assert report["baseline_pass_rate"] == 0.5
    assert report["candidate_pass_rate"] == 1.0


def test_select_evaluable_candidate_skips_unhealthy_and_already_reported_candidates():
    candidate = select_evaluable_candidate(
        candidates={
            "candidate-b": {"source_attempt_id": "attempt-b", "evidence_class": "deterministic-validator"},
            "candidate-a": {"source_attempt_id": "attempt-a", "target_role": "planner"},
            "candidate-c": {"source_attempt_id": "attempt-c", "target_role": "executor", "evidence_class": "deterministic-validator"},
        },
        attempts={
            "attempt-a": {"environment_healthy": False},
            "attempt-b": {"environment_healthy": True},
            "attempt-c": {"environment_healthy": True},
        },
        reports={"candidate-b": {"recommendation": "reject_regression"}},
    )

    assert candidate == ("candidate-c", {"source_attempt_id": "attempt-c", "target_role": "executor", "evidence_class": "deterministic-validator"})


def test_select_evaluable_candidate_returns_none_without_healthy_unevaluated_source():
    assert select_evaluable_candidate(
        candidates={"candidate-a": {"source_attempt_id": "attempt-a"}},
        attempts={"attempt-a": {"environment_healthy": False}},
        reports={},
    ) is None


def test_select_evaluable_candidate_skips_unsupported_roles():
    assert select_evaluable_candidate(
        candidates={"candidate-a": {"source_attempt_id": "attempt-a", "target_role": "planner"}},
        attempts={"attempt-a": {"environment_healthy": True}},
        reports={},
    ) is None


def test_select_evaluable_candidate_resolves_a_healthy_attempt_by_task_id():
    candidate = select_evaluable_candidate(
        candidates={"candidate-a": {
            "source_attempt_id": "task-1",
            "target_role": "executor",
            "evidence_class": "deterministic-validator",
        }},
        attempts={"attempt-1": {"task_id": "task-1", "environment_healthy": True}},
        reports={},
    )

    assert candidate is not None


def test_select_evaluable_candidate_skips_judgment_only_evidence():
    assert select_evaluable_candidate(
        candidates={"candidate-a": {
            "source_attempt_id": "attempt-1",
            "target_role": "executor",
            "evidence_class": "judgment",
        }},
        attempts={"attempt-1": {"environment_healthy": True}},
        reports={},
    ) is None


def test_resolve_source_attempt_supports_attempt_or_task_id():
    attempts = {
        "attempt-1": {"attempt_id": "attempt-1", "task_id": "task-1", "environment_healthy": True},
    }

    assert resolve_source_attempt(attempts, "attempt-1") == attempts["attempt-1"]
    assert resolve_source_attempt(attempts, "task-1") == attempts["attempt-1"]