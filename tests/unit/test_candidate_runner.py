"""Paired held-out runs for staged executor candidates."""

from __future__ import annotations

import urllib.request

from tests import run_tests


def test_http_post_attaches_the_internal_token_header_when_configured(monkeypatch):
    monkeypatch.setenv("CXP_INTERNAL_TOKEN", "secret-token")
    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=10):
        captured["token"] = req.get_header("X-cxp-internal-token")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # The web dashboard strips candidate_id/evaluation_run from any caller
    # that doesn't present this header -- run_candidate_comparison()'s own
    # submissions must carry it or they'd silently lose their own inputs.
    run_tests._http_post("/api/submit", {"goal": "x"})

    assert captured["token"] == "secret-token"


def test_http_post_omits_the_header_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("CXP_INTERNAL_TOKEN", raising=False)
    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=10):
        captured["token"] = req.get_header("X-cxp-internal-token")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    run_tests._http_post("/api/submit", {"goal": "x"})

    assert captured["token"] is None


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
        ("write a function", {"evaluation_run": True}),
        ("write a function", {"candidate_id": "candidate-1", "evaluation_run": True}),
    ]
    assert report["recommendation"] == "recommend_promotion"
    assert report["candidate_id"] == "candidate-1"


def test_candidate_comparison_marks_both_submissions_as_evaluation_runs(monkeypatch):
    test_case = {
        "label": "CODE_GENERATION",
        "goal": "write a function",
        "validator": lambda _output: (True, []),
        "threshold": 0.5,
        "timeout": 10,
    }
    submitted_inputs = []

    def fake_submit(goal, inputs=None):
        submitted_inputs.append(inputs)
        return "task-id"

    monkeypatch.setattr(run_tests, "submit_task", fake_submit)
    monkeypatch.setattr(run_tests, "wait_for_results", lambda *_args, **_kwargs: {})

    run_tests.run_candidate_comparison(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        held_out_tests=[test_case],
    )

    # Both the baseline and candidate submissions run through the exact same
    # live pipeline as a real task -- without this flag, verifier has no way
    # to tell a deliberate candidate comparison apart from real production
    # traffic before writing to episodic memory.
    assert all(inputs is not None and inputs.get("evaluation_run") is True for inputs in submitted_inputs)


def test_candidate_comparison_skips_held_out_runs_while_the_swarm_is_halted(monkeypatch):
    test_case = {
        "label": "CODE_GENERATION",
        "goal": "write a function",
        "validator": lambda _output: (True, []),
        "threshold": 0.5,
        "timeout": 10,
    }
    submit_called = False

    def fake_submit(goal, inputs=None):
        nonlocal submit_called
        submit_called = True
        return "task-id"

    monkeypatch.setattr(run_tests, "check_halted", lambda: {"reason": "boom"})
    monkeypatch.setattr(run_tests, "submit_task", fake_submit)

    # A halted swarm rejects submissions with 409 -- without this check, a
    # candidate comparison would submit into that wall of rejections and
    # report a spurious recommendation instead of skipping cleanly.
    report = run_tests.run_candidate_comparison(
        candidate_id="candidate-1",
        source_attempt={"environment_healthy": True},
        held_out_tests=[test_case],
    )

    assert submit_called is False
    assert report["recommendation"] == "insufficient_evidence"