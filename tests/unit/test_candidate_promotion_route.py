"""HTTP boundary for human-approved candidate promotion."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src import web_dashboard


async def test_promote_candidate_route_returns_applied_revision(monkeypatch):
    monkeypatch.setattr(
        web_dashboard,
        "promote_candidate",
        AsyncMock(return_value={"candidate_id": "candidate-1", "target_role": "executor", "revision": 4}),
    )

    response = await web_dashboard.promote_candidate_route("candidate-1")

    assert response.status_code == 200
    assert b'"revision":4' in response.body


async def test_promote_candidate_route_returns_conflict_for_unrecommended_candidate(monkeypatch):
    monkeypatch.setattr(
        web_dashboard,
        "promote_candidate",
        AsyncMock(side_effect=ValueError("candidate candidate-1 is not recommended for promotion")),
    )

    response = await web_dashboard.promote_candidate_route("candidate-1")

    assert response.status_code == 409
    assert b"not recommended" in response.body