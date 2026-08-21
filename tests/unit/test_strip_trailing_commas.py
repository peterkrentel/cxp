"""_strip_trailing_commas() -- found live 2026-08-21 via a full OTel span
capture (packet dcb5043e, see docs/otel-setup.md): a genuinely complete,
well-formed decomposition response failed to parse purely because of a
trailing comma after the last property in an object, and again before an
array's closing bracket -- legal in Python dict/list literals, invalid in
strict JSON. Before this span existed, this was indistinguishable from
"the response is malformed/truncated" -- with the full text finally
visible, it's neither: the model wrote a small, common JSON habit, not a
capability failure."""

from __future__ import annotations

import json

from src.agent_shell import _strip_trailing_commas


def test_strip_trailing_commas_removes_comma_before_closing_bracket():
    assert _strip_trailing_commas("[1, 2, 3,]") == "[1, 2, 3]"


def test_strip_trailing_commas_removes_comma_before_closing_brace():
    assert _strip_trailing_commas('{"a": 1,}') == '{"a": 1}'


def test_strip_trailing_commas_handles_whitespace_and_newlines_before_bracket():
    assert _strip_trailing_commas('{\n  "a": 1,\n}') == '{\n  "a": 1\n}'


def test_strip_trailing_commas_is_a_noop_on_already_valid_json():
    text = '{"a": 1, "b": 2}'
    assert _strip_trailing_commas(text) == text


def test_strip_trailing_commas_fixes_the_real_live_capture():
    """The exact response captured live 2026-08-21 (trace
    5885c0864aeb006f8fbf5f3e46ae9e1e, packet dcb5043e) -- a trailing
    comma after each object's last property, plus one before the
    array's closing bracket."""
    raw = """[
    {
        "type": "code",
        "priority": 3,
    },
    {
        "type": "verify",
        "priority": 2,
    },
]"""
    cleaned = _strip_trailing_commas(raw)
    result = json.loads(cleaned)
    assert len(result) == 2
    assert result[0]["priority"] == 3
    assert result[1]["priority"] == 2
