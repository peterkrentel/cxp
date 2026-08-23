"""Source modules required at runtime must be listed in the Helm ConfigMap."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_app_code_configmap_packages_contracts_module():
    template = (ROOT / "helm/cxp/templates/app-code.yaml").read_text()

    assert 'contracts.py: |' in template
    assert '.Files.Get "app/src/contracts.py"' in template


def test_app_code_configmap_packages_candidate_evaluator_module():
    template = (ROOT / "helm/cxp/templates/app-code.yaml").read_text()

    assert 'candidate_evaluation.py: |' in template
    assert '.Files.Get "app/src/candidate_evaluation.py"' in template


def test_app_code_configmap_packages_candidate_evaluation_worker():
    template = (ROOT / "helm/cxp/templates/app-code.yaml").read_text()

    assert 'evaluate_candidate.py: |' in template
    assert '.Files.Get "app/tests/evaluate_candidate.py"' in template


def test_test_runner_assembles_and_runs_candidate_evaluation_worker():
    template = (ROOT / "helm/cxp/templates/test-runner.yaml").read_text()

    assert "cp /cm/root/evaluate_candidate.py /app/tests/evaluate_candidate.py" in template
    assert "python -u /app/tests/evaluate_candidate.py" in template