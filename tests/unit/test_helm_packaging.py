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