"""One conservative candidate evaluation worker run."""

from __future__ import annotations

import json

from tests.evaluate_candidate import save_evaluation_report, run_evaluation


def test_worker_compares_candidate_and_publishes_annotated_report():
    published = []

    def compare(**kwargs):
        assert kwargs["candidate_id"] == "candidate-1"
        assert kwargs["source_attempt"] == {"attempt_id": "attempt-1", "environment_healthy": True}
        return {"candidate_id": "candidate-1", "recommendation": "recommend_promotion"}

    def publish(candidate_id, report):
        published.append((candidate_id, report))

    report = run_evaluation(
        candidate_id="candidate-1",
        candidate={"source_attempt_id": "attempt-1", "target_role": "executor"},
        source_attempt={"attempt_id": "attempt-1", "environment_healthy": True},
        compare=compare,
        publish=publish,
    )

    assert report["target_role"] == "executor"
    assert report["source_attempt_id"] == "attempt-1"
    assert published == [("candidate-1", report)]


def test_worker_saves_report_to_results_history(tmp_path):
    report = {"candidate_id": "candidate-1", "recommendation": "recommend_promotion"}

    path = save_evaluation_report(report, results_dir=tmp_path, timestamp="20260823_120000")

    assert path.name == "candidate_candidate-1_20260823_120000.json"
    assert json.loads(path.read_text()) == report