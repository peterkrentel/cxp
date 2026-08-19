import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.check_plateau import is_clean_run, streak_for_tier, determine_current_tier, STREAK_TARGET, TIER_LABELS


def _run(tier, statuses: dict):
    """statuses: {label: "PASS"|"FAIL"|"TIMEOUT", ...} for a subset or all of TIER_LABELS."""
    return {"tier": tier, "results": [{"label": label, "status": status} for label, status in statuses.items()]}


def _all_pass(tier):
    return _run(tier, {label: "PASS" for label in TIER_LABELS})


def _all_pass_but_one_fail(tier):
    labels = list(TIER_LABELS)
    statuses = {label: "PASS" for label in labels}
    statuses[labels[0]] = "FAIL"
    return _run(tier, statuses)


def test_run_from_a_different_tier_is_not_evidence_either_way():
    run = _all_pass(tier=1)
    assert is_clean_run(run, tier=0) is None


def test_run_missing_a_label_is_not_evidence_either_way():
    labels = list(TIER_LABELS)
    partial = _run(tier=0, statuses={l: "PASS" for l in labels[:-1]})  # one label missing
    assert is_clean_run(partial, tier=0) is None


def test_all_labels_passing_is_clean():
    assert is_clean_run(_all_pass(tier=0), tier=0) is True


def test_any_label_failing_is_not_clean():
    assert is_clean_run(_all_pass_but_one_fail(tier=0), tier=0) is False


def test_streak_stops_walking_backward_at_first_non_clean_run(tmp_path):
    # oldest -> newest: clean, clean, FAIL, clean, clean -- streak should be 2, not 4
    runs = [_all_pass(0), _all_pass(0), _all_pass_but_one_fail(0), _all_pass(0), _all_pass(0)]
    for i, run in enumerate(runs):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    assert streak_for_tier(str(tmp_path), tier=0) == 2


def test_streak_skips_interleaved_runs_from_other_tiers_without_breaking(tmp_path):
    # a tier-1 run sitting in between two clean tier-0 runs shouldn't break the tier-0 streak
    runs = [_all_pass(0), _all_pass(1), _all_pass(0)]
    for i, run in enumerate(runs):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    assert streak_for_tier(str(tmp_path), tier=0) == 2


def test_determine_current_tier_is_0_with_no_results_dir():
    assert determine_current_tier("/nonexistent/path") == 0


def test_determine_current_tier_promotes_once_streak_target_is_met(tmp_path):
    for i in range(STREAK_TARGET):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(_all_pass(0)))
    assert determine_current_tier(str(tmp_path)) == 1


def test_determine_current_tier_never_exceeds_the_top_of_the_ladder(tmp_path):
    # every tier maxed out -- must not walk past the last index in TIERS
    from tests.check_plateau import TIERS
    top = len(TIERS) - 1
    i = 0
    for tier in range(top + 1):
        for _ in range(STREAK_TARGET):
            (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(_all_pass(tier)))
            i += 1
    assert determine_current_tier(str(tmp_path)) == top


def test_build_tier_status_payload_with_no_history(tmp_path):
    from tests.check_plateau import build_tier_status_payload, TIERS
    payload = build_tier_status_payload(str(tmp_path / "nonexistent"))
    assert payload["active_tier"] == 0
    assert payload["top_tier"] == len(TIERS) - 1
    assert payload["streak_target"] == STREAK_TARGET
    assert payload["streaks"] == {str(t): 0 for t in range(len(TIERS))}


def test_build_tier_status_payload_reflects_real_streaks(tmp_path):
    from tests.check_plateau import build_tier_status_payload
    for i in range(3):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(_all_pass(0)))
    payload = build_tier_status_payload(str(tmp_path))
    assert payload["streaks"]["0"] == 3
    assert payload["streaks"]["1"] == 0
    assert payload["active_tier"] == 0
