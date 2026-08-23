"""Paired held-out runs for staged executor candidates."""

from __future__ import annotations

from tests import run_tests


def test_submit_task_includes_optional_evaluation_inputs(monkeypatch):
    captured = {}

    def fake_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"task_id": "task-1"}

    monkeypatch.setattr(run_tests, "_http_post", fake_post)

    assert run_tests.submit_task("write code", inputs={"candidate_id": "candidate-1"}) == "task-1"
    assert captured == {
        "path": "/api/submit",
        "data": {"goal": "write code", "inputs": {"candidate_id": "candidate-1"}},
    }


def test_trigger_improvement_includes_optional_candidate_evidence_inputs(monkeypatch):
    captured = {}

    def fake_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"task_id": "reflect-1"}

    monkeypatch.setattr(run_tests, "_http_post", fake_post)

    run_tests.trigger_improvement(
        "STRUCTURED_OUTPUT",
        ["Invalid YAML"],
        inputs={
            "target_role": "executor",
            "source_attempt_id": "task-1",
            "evidence_class": "deterministic-validator",
        },
    )

    assert captured["path"] == "/api/submit"
    assert captured["data"]["inputs"]["evidence_class"] == "deterministic-validator"


def test_planner_contract_failure_targets_planner_candidate():
    inputs = run_tests.improvement_inputs_for_result({
        "status": "PLANNER_FAILED",
        "task_id": "task-1",
        "evidence_class": "contract",
    })

    assert inputs == {
        "target_role": "planner",
        "source_attempt_id": "task-1",
        "evidence_class": "contract",
    }


def test_candidate_comparison_runs_active_and_candidate_versions_sequentially(monkeypatch):
    test_case = {
        "label": "CODE_GENERATION",
        "goal": "write a function",
        "validator": lambda _output: (True, []),
        "threshold": 0.5,
        "timeout": 10,
    }
    submitted = []
    results = iter([
        {"active": {"output": "active", "score": 0.2}},
        {"candidate": {"output": "candidate", "score": 0.9}},
    ])

    def fake_submit(goal, inputs=None):
        submitted.append((goal, inputs))
        return "active" if inputs is None else "candidate"

    monkeypatch.setattr(run_tests, "submit_task", fake_submit)
    monkeypatch.setattr(run_tests, "wait_for_results", lambda *_args, **_kwargs: next(results))

    report = run_tests.run_candidate_comparison(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        held_out_tests=[test_case],
    )

    assert submitted == [
        ("write a function", None),
        ("write a function", {"candidate_id": "candidate-1"}),
    ]
    assert report["recommendation"] == "recommend_promotion"
    assert report["candidate_id"] == "candidate-1"