"""deployer's YAML deploy path -- found live 2026-08-17: `kubectl apply`
succeeding only means the API server accepted the manifest, not that the
pods it creates ever actually run. Every deploy reported "deployed": true
even though the LLM's invented image references meant every single one
was permanently stuck in ImagePullBackOff, silently feeding a false
success signal into reflect/memory. These tests pin down the fix: an
applied Deployment must actually reach readyReplicas > 0 before the deploy
is reported as successful."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.agents import deployer as deployer_module
from src.agents.deployer import DeployerAgent

YAML_WITH_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  replicas: 1
"""

YAML_SERVICE_ONLY = """apiVersion: v1
kind: Service
metadata:
  name: my-svc
"""


def test_deployment_names_extracts_only_deployment_kind():
    names = DeployerAgent._deployment_names(YAML_WITH_DEPLOYMENT + "---\n" + YAML_SERVICE_ONLY)
    assert names == ["my-app"]


def test_deployment_names_handles_malformed_yaml_gracefully():
    assert DeployerAgent._deployment_names("not: valid: yaml: [[[") == []


def test_deployment_names_empty_for_non_deployment_manifest():
    assert DeployerAgent._deployment_names(YAML_SERVICE_ONLY) == []


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


async def test_deploy_reports_failure_when_deployment_never_becomes_ready(agent, monkeypatch):
    d = DeployerAgent()
    monkeypatch.setattr(deployer_module, "DEPLOY_READY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(deployer_module, "DEPLOY_READY_POLL_SECONDS", 0.01)

    calls = {"apply": 0, "get": 0}

    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply" and "--dry-run=server" in cmd:
            return _proc(0)
        if cmd[1] == "apply":
            calls["apply"] += 1
            return _proc(0, stdout="deployment.apps/my-app created")
        if cmd[1] == "get":
            calls["get"] += 1
            return _proc(0, stdout=json.dumps({"status": {"readyReplicas": 0}}))
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(deployer_module.subprocess, "run", fake_run)

    result = await d._deploy_yaml(YAML_WITH_DEPLOYMENT)

    assert result["deployed"] is False
    assert result["outcome"] == "applied but never became healthy"
    assert result["unhealthy_deployments"] == ["my-app"]
    assert calls["get"] > 0  # actually checked, not just trusted the apply


async def test_deploy_reports_success_once_deployment_becomes_ready(agent, monkeypatch):
    d = DeployerAgent()
    monkeypatch.setattr(deployer_module, "DEPLOY_READY_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(deployer_module, "DEPLOY_READY_POLL_SECONDS", 0.01)

    get_calls = {"count": 0}

    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply" and "--dry-run=server" in cmd:
            return _proc(0)
        if cmd[1] == "apply":
            return _proc(0, stdout="deployment.apps/my-app created")
        if cmd[1] == "get":
            get_calls["count"] += 1
            ready = 1 if get_calls["count"] >= 2 else 0
            return _proc(0, stdout=json.dumps({"status": {"readyReplicas": ready}}))
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(deployer_module.subprocess, "run", fake_run)

    result = await d._deploy_yaml(YAML_WITH_DEPLOYMENT)

    assert result["deployed"] is True
    assert result["outcome"] == "applied"
    assert get_calls["count"] >= 2  # actually polled until ready, not just once


async def test_deploy_skips_health_polling_for_non_deployment_manifests(agent, monkeypatch):
    d = DeployerAgent()

    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply" and "--dry-run=server" in cmd:
            return _proc(0)
        if cmd[1] == "apply":
            return _proc(0, stdout="service/my-svc created")
        raise AssertionError(f"unexpected command (no Deployment to poll): {cmd}")

    monkeypatch.setattr(deployer_module.subprocess, "run", fake_run)

    result = await d._deploy_yaml(YAML_SERVICE_ONLY)

    assert result["deployed"] is True
