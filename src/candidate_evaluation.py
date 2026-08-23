"""Deterministic promotion recommendations for staged skill candidates."""

from __future__ import annotations

from typing import Any


def _pass_rate(results: list[dict[str, Any]]) -> float:
    return sum(result.get("status") == "PASS" for result in results) / len(results)


def evaluate_candidate(
    *,
    candidate_id: str,
    source_attempt: dict[str, Any],
    baseline_results: list[dict[str, Any]],
    candidate_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recommend promotion only for healthy, held-out improvements.

    This function deliberately returns a recommendation rather than applying a
    skill. Promotion remains a human decision even after a candidate wins.
    """
    report: dict[str, Any] = {
        "candidate_id": candidate_id,
        "eligible": False,
        "recommendation": "insufficient_evidence",
        "baseline_pass_rate": None,
        "candidate_pass_rate": None,
        "regressions": [],
    }
    if not source_attempt.get("environment_healthy", True):
        report["recommendation"] = "reject_platform_unhealthy"
        return report
    if not baseline_results or not candidate_results:
        return report

    baseline_by_label = {result.get("label"): result for result in baseline_results}
    candidate_by_label = {result.get("label"): result for result in candidate_results}
    regressions = sorted(
        label for label, baseline in baseline_by_label.items()
        if baseline.get("status") == "PASS"
        and candidate_by_label.get(label, {}).get("status") != "PASS"
    )
    baseline_pass_rate = _pass_rate(baseline_results)
    candidate_pass_rate = _pass_rate(candidate_results)
    report.update({
        "baseline_pass_rate": baseline_pass_rate,
        "candidate_pass_rate": candidate_pass_rate,
        "regressions": regressions,
    })
    if regressions:
        report["recommendation"] = "reject_regression"
        return report
    if candidate_pass_rate <= baseline_pass_rate:
        report["recommendation"] = "reject_no_improvement"
        return report

    report["eligible"] = True
    report["recommendation"] = "recommend_promotion"
    return report