"""Every self.llm(...) call site must forward the packet's full lineage
(task_id, parent_packet_id) alongside packet_id -- otherwise OTel spans
can be looked up by individual packet but a Tempo/Grafana dashboard can't
group by task or reconstruct parent-child chains. Confirmed live
2026-08-23 while building the OTel dashboard: only packet.id was ever
being stamped onto llm.call spans.

A source-text check rather than a mocked httpx integration test -- llm()'s
actual streaming/slot-acquisition mechanics are already covered elsewhere
(test_llm_stream_reassembly.py, test_ollama_slots.py) and deliberately not
re-mocked here; this only pins down that each call site passes the right
keyword arguments through.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

AGENT_FILES = [
    "src/agents/assessor.py",
    "src/agents/diagnostician.py",
    "src/agents/executor.py",
    "src/agents/planner.py",
    "src/agents/reflect.py",
    "src/agents/verifier.py",
]


def test_every_llm_call_site_forwards_task_id_and_parent_packet_id():
    missing = []
    for rel_path in AGENT_FILES:
        source = (ROOT / rel_path).read_text()
        for match in re.finditer(r"\.llm\(([^)]*)\)", source, re.DOTALL):
            call_args = match.group(1)
            if "packet_id=packet.id" not in call_args:
                continue  # not a packet-scoped llm() call
            if "task_id=packet.task_id" not in call_args or "parent_packet_id=packet.parent_packet_id" not in call_args:
                missing.append(rel_path)

    assert missing == []


# Only capabilities whose contract actually expects JSON back may request
# Ollama's format="json" mode -- executor's raw code/YAML output and
# reflect's raw skill-file text must never be JSON-constrained.
JSON_MODE_EXPECTED = {
    "src/agents/assessor.py": True,
    "src/agents/diagnostician.py": True,
    "src/agents/executor.py": False,
    "src/agents/planner.py": True,
    "src/agents/reflect.py": False,
    "src/agents/verifier.py": True,
}


def test_only_json_expecting_capabilities_request_ollama_json_mode():
    wrong = []
    for rel_path, expects_json_mode in JSON_MODE_EXPECTED.items():
        source = (ROOT / rel_path).read_text()
        match = re.search(r"\.llm\(([^)]*)\)", source, re.DOTALL)
        assert match, f"no llm() call found in {rel_path}"
        has_json_mode = "json_mode=True" in match.group(1)
        if has_json_mode != expects_json_mode:
            wrong.append(rel_path)

    assert wrong == []
