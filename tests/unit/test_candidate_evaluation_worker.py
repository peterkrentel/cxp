"""One conservative candidate evaluation worker run."""

from __future__ import annotations

import json

from tests.evaluate_candidate import save_evaluation_report, run_evaluation, _read_json_bucket


async def test_read_json_bucket_returns_empty_on_a_bucket_with_zero_keys():
    from nats.js.errors import NoKeysError

    # get_or_create_kv only fixes a missing *bucket* -- a bucket that exists
    # but has never had anything staged in it yet (the real state of a fresh
    # cluster's cxp-skill-candidates bucket, confirmed live 2026-08-23) makes
    # kv.keys() raise NoKeysError rather than return [], crashing this
    # CronJob every hour exactly like the bucket-missing case this was
    # supposed to already be fixed against.
    class EmptyKV:
        async def keys(self):
            raise NoKeysError()

    entries = await _read_json_bucket(EmptyKV())

    assert entries == {}


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