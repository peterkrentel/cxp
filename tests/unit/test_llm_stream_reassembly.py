"""AgentShell.llm()'s NDJSON stream reassembly.

Found live 2026-08-20: _stream() parsed each line from Ollama's streamed
response independently, and on any json.loads() failure silently
discarded it via a bare `except Exception: continue` -- no log, no trace.
If a single JSON object ever arrives split across two transport-level
lines (plausible under the real CPU contention this cluster runs
Ollama under), that line's content is just gone. The reconstructed
response then has a gap in it -- which is exactly what an "Unterminated
string" / "Expecting ',' delimiter" downstream JSON error, or a Python
"unterminated string literal" SyntaxError in generated code, looks like.
Several capability-test failures this session were read as the model
being unreliable at structured output; this is a real, fixable client-
side data-loss bug, not a model capability ceiling.

_NDJSONReassembler.feed() buffers across a parse failure and retries
with the next line appended, instead of dropping it -- so a split
object is recovered instead of silently corrupted."""

from __future__ import annotations

import json

from src.agent_shell import _NDJSONReassembler


def _line(content: str) -> str:
    return json.dumps({"message": {"content": content}})


def test_feed_returns_token_content_for_a_complete_line():
    r = _NDJSONReassembler()
    assert r.feed(_line("hello")) == "hello"
    assert r.leftover == ""


def test_feed_ignores_blank_lines():
    r = _NDJSONReassembler()
    assert r.feed("") is None
    assert r.leftover == ""


def test_feed_recovers_content_split_across_two_transport_lines():
    """The core regression: a single JSON object arriving split across
    two aiter_lines() yields must not lose its content -- the old code
    (each line parsed alone, failure silently discarded) would drop this
    entirely."""
    r = _NDJSONReassembler()
    whole = _line('some "quoted" content, with a comma')
    split_at = len(whole) // 2
    first_half, second_half = whole[:split_at], whole[split_at:]

    # First half alone is not valid JSON -- must buffer, not discard.
    assert r.feed(first_half) is None
    assert r.leftover == first_half

    # Second half completes it -- full content recovered, not lost.
    token = r.feed(second_half)
    assert token == 'some "quoted" content, with a comma'
    assert r.leftover == ""


def test_feed_recovers_content_split_across_three_transport_lines():
    """Not just a two-way split -- an arbitrarily fragmented line must
    still fully recover once all its pieces have arrived."""
    r = _NDJSONReassembler()
    whole = _line("a longer piece of streamed content")
    third = len(whole) // 3
    parts = [whole[:third], whole[third:2 * third], whole[2 * third:]]

    assert r.feed(parts[0]) is None
    assert r.feed(parts[1]) is None
    token = r.feed(parts[2])
    assert token == "a longer piece of streamed content"


def test_leftover_reports_unparsed_content_when_stream_ends_mid_object():
    """If the stream genuinely ends with an incomplete object (a real
    dead connection, not just a benign split), leftover surfaces it for
    logging instead of it vanishing without a trace."""
    r = _NDJSONReassembler()
    whole = _line("never completes")
    r.feed(whole[: len(whole) // 2])
    assert r.leftover != ""


def test_feed_handles_a_token_with_no_content_key():
    r = _NDJSONReassembler()
    assert r.feed(json.dumps({"message": {}})) == ""
    assert r.feed(json.dumps({"done": True})) == ""
