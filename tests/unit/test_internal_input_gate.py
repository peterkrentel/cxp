"""Only the trusted in-cluster test-runner may set internal-only /api/submit
inputs (candidate_id, evaluation_run) -- everyone else gets them stripped.

Without this gate, any caller of the public, unauthenticated /api/submit
route could supply candidate_id to run an unvetted staged skill candidate
against a real task, or evaluation_run to hide a real production score from
the episodic-memory regression baseline.
"""

from __future__ import annotations

from src.web_dashboard import sanitize_untrusted_inputs


def test_public_caller_without_a_token_has_internal_only_fields_stripped(monkeypatch):
    monkeypatch.setenv("CXP_INTERNAL_TOKEN", "secret-token")
    inputs = {"candidate_id": "candidate-1", "evaluation_run": True, "goal_detail": "keep me"}

    sanitized = sanitize_untrusted_inputs(inputs, internal_token_header=None)

    assert sanitized == {"goal_detail": "keep me"}


def test_public_caller_with_a_wrong_token_has_internal_only_fields_stripped(monkeypatch):
    monkeypatch.setenv("CXP_INTERNAL_TOKEN", "secret-token")
    inputs = {"candidate_id": "candidate-1"}

    sanitized = sanitize_untrusted_inputs(inputs, internal_token_header="wrong")

    assert sanitized == {}


def test_trusted_caller_with_the_matching_token_keeps_both_fields(monkeypatch):
    monkeypatch.setenv("CXP_INTERNAL_TOKEN", "secret-token")
    inputs = {"candidate_id": "candidate-1", "evaluation_run": True}

    sanitized = sanitize_untrusted_inputs(inputs, internal_token_header="secret-token")

    assert sanitized == inputs


def test_when_no_internal_token_is_configured_every_caller_is_treated_as_untrusted(monkeypatch):
    monkeypatch.delenv("CXP_INTERNAL_TOKEN", raising=False)
    inputs = {"candidate_id": "candidate-1"}

    # Fail closed: a deployment that never wired the Secret must not
    # silently trust an empty/missing header as "no token configured, so
    # anything goes."
    sanitized = sanitize_untrusted_inputs(inputs, internal_token_header=None)

    assert sanitized == {}


def test_unrelated_input_keys_are_never_touched(monkeypatch):
    monkeypatch.setenv("CXP_INTERNAL_TOKEN", "secret-token")
    inputs = {"target_role": "executor", "source_attempt_id": "task-1"}

    sanitized = sanitize_untrusted_inputs(inputs, internal_token_header=None)

    assert sanitized == inputs
