"""AgentShell.llm()'s Ollama chat request body -- specifically json_mode,
which sets Ollama's `format: "json"` param to constrain token sampling to
syntactically valid JSON.

Found live 2026-08-23: SMOKE's planner call produced a JSON array where one
subtask's value used Python triple-quoted string syntax instead of a
properly escaped JSON string -- confirmed via the raw_response stored in
durable attempt evidence, a complete/well-formed response overall, not the
NDJSON stream-reassembly bug this file's sibling test module already
covers. json.loads() correctly rejected it ("Expecting ',' delimiter").
Ollama's json_mode constrains sampling so this exact class of syntax error
becomes structurally impossible, catching it at generation time instead of
only ever catching it after the fact via parse_contract().

Scoped to only the capabilities whose contract actually expects JSON back
(plan/verify/assess/diagnose) -- executor's raw code/YAML output and
reflect's raw skill-file text must never be JSON-constrained.
"""

from __future__ import annotations

from src.agent_shell import _build_ollama_chat_request


def test_json_mode_off_by_default_omits_format_field():
    request = _build_ollama_chat_request("SYSTEM", "USER")

    assert "format" not in request
    assert request["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]


def test_json_mode_true_sets_ollama_format_json():
    request = _build_ollama_chat_request("SYSTEM", "USER", json_mode=True)

    assert request["format"] == "json"


def test_json_mode_false_explicitly_still_omits_format_field():
    request = _build_ollama_chat_request("SYSTEM", "USER", json_mode=False)

    assert "format" not in request
