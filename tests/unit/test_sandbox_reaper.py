"""sandbox_reaper.py's keep/delete decision for one cxp-sandbox Deployment,
extracted as a pure function so it's testable without a live cluster.
"""

from __future__ import annotations

from scripts.sandbox_reaper import MAX_HEALTHY_AGE_SECONDS, MIN_AGE_SECONDS, _decide


def test_keeps_a_recent_unhealthy_deployment_within_the_grace_period():
    should_delete, reason = _decide("hello-world", age_seconds=100, ready=0)

    assert should_delete is False
    assert "grace period" in reason


def test_removes_an_unhealthy_deployment_past_the_grace_period():
    should_delete, reason = _decide("hello-world", age_seconds=MIN_AGE_SECONDS + 1, ready=0)

    assert should_delete is True
    assert "never became ready" in reason


def test_keeps_a_healthy_deployment_below_the_max_sandbox_lifetime():
    should_delete, reason = _decide("hello-world", age_seconds=100, ready=2)

    assert should_delete is False
    assert "ready replica" in reason


def test_removes_a_healthy_deployment_past_the_max_sandbox_lifetime():
    # The sandbox namespace is meant to be ephemeral proof that an artifact
    # worked, not a place for the swarm's own test deployments to live
    # forever -- a Deployment that succeeded still has to go once it's had
    # long enough to be observed.
    should_delete, reason = _decide("hello-world", age_seconds=MAX_HEALTHY_AGE_SECONDS + 1, ready=2)

    assert should_delete is True
    assert "max sandbox lifetime" in reason


def test_max_healthy_age_is_longer_than_the_unhealthy_grace_period():
    # A healthy deployment must always get at least as much time to live as
    # an unhealthy one -- otherwise this would delete working deployments
    # sooner than broken ones, which defeats the reaper's own purpose.
    assert MAX_HEALTHY_AGE_SECONDS > MIN_AGE_SECONDS
