"""Web task submission must preserve explicit evaluation inputs safely."""

from __future__ import annotations

import pytest

from src.web_dashboard import build_submission_packet


def test_submission_packet_keeps_candidate_id_in_payload_inputs():
    packet = build_submission_packet({
        "goal": "write a function",
        "inputs": {"candidate_id": "candidate-1"},
    })

    assert packet.capability == "plan"
    assert packet.payload.inputs == {"candidate_id": "candidate-1"}


def test_submission_packet_rejects_non_object_inputs():
    with pytest.raises(ValueError, match="inputs"):
        build_submission_packet({"goal": "write a function", "inputs": "candidate-1"})