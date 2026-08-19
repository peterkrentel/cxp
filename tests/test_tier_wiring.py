import sys, os, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import select_active_tier, _fetch_results_history, TIER_0_TESTS, TIER_1_TESTS, TIERS


def test_select_active_tier_defaults_to_tier_0_with_no_history():
    tier, tests = select_active_tier(None)
    assert tier == 0
    assert tests is TIER_0_TESTS


def test_select_active_tier_picks_the_right_tests_list_for_each_tier(tmp_path):
    from tests.check_plateau import STREAK_TARGET, TIER_LABELS
    # 10 clean tier-0 runs on disk -> should select tier 1's actual test list
    run = {"tier": 0, "results": [{"label": l, "status": "PASS"} for l in TIER_LABELS]}
    for i in range(STREAK_TARGET):
        (tmp_path / f"run_{i:04d}.json").write_text(json.dumps(run))
    tier, tests = select_active_tier(str(tmp_path))
    assert tier == 1
    assert tests is TIER_1_TESTS


def test_fetch_results_history_returns_none_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert _fetch_results_history() is None


def test_fetch_results_history_builds_the_correct_clone_command(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("fake-token-value")
    (tmp_path / "repo").write_text("someorg/somerepo")
    real_open = open  # capture before patching -- the replacement below must
    # not call the (now-patched) builtins.open, or it recurses into itself
    monkeypatch.setattr("os.path.exists", lambda p: p in ("/git-creds/token", "/git-creds/repo"))
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a: real_open(str(tmp_path / "token") if "token" in p else str(tmp_path / "repo")),
    )

    captured = {}
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr("subprocess.run", fake_run)

    _fetch_results_history()
    assert "cmd" in captured, "git clone was never invoked"
    assert "--depth=50" in captured["cmd"]
    assert "bot/test-results" in captured["cmd"]
    assert "fake-token-value" in captured["cmd"][-2]  # URL contains the token
    assert "someorg/somerepo" in captured["cmd"][-2]


def test_fetch_results_history_returns_none_on_clone_failure(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("t")
    (tmp_path / "repo").write_text("o/r")
    real_open = open  # capture before patching, same reason as above
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda p, *a: real_open(str(tmp_path / "token")))
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert _fetch_results_history() is None
