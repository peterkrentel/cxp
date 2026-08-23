"""Contract boundaries for agent-produced output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts import (
    AssessmentResult,
    ArtifactResult,
    ContractParseError,
    PlanResult,
    VerificationResult,
    parse_contract,
)
from src.packet import CXPPacket, PacketType, Payload


def test_existing_packet_without_schema_version_uses_v1_default():
    packet = CXPPacket.model_validate({
        "type": "code",
        "capability": "code",
        "payload": {"goal": "write a function"},
    })

    assert packet.schema_version == "1.0"


def test_plan_contract_repairs_trailing_commas_and_validates_subtasks():
    result = parse_contract("plan", """[
        {
            "type": "code",
            "capability": "code",
            "goal": "write a function",
            "instructions": "return the artifact",
            "priority": 2,
        },
    ]""")

    assert isinstance(result, PlanResult)
    assert result.subtasks[0].capability == "code"
    assert result.subtasks[0].priority == 2


def test_plan_contract_rejects_malformed_model_output_with_producer_context():
    with pytest.raises(ContractParseError, match="plan"):
        parse_contract("plan", "not JSON")


def test_artifact_contract_unwraps_one_outer_fence_and_preserves_format():
    raw = "```yaml\napiVersion: v1\nkind: ConfigMap\n```"

    result = parse_contract("code", raw, expected_format="yaml")

    assert isinstance(result, ArtifactResult)
    assert result.format == "yaml"
    assert result.content == "apiVersion: v1\nkind: ConfigMap"


def test_artifact_contract_infers_format_from_outer_fence():
    result = parse_contract("code", "```python\nprint('hello')\n```")

    assert isinstance(result, ArtifactResult)
    assert result.format == "python"
    assert result.content == "print('hello')"


def test_artifact_contract_extracts_first_fence_before_trailing_prose():
    result = parse_contract("code", "```yaml\nkind: ConfigMap\n```\nHere is the manifest.")

    assert result.format == "yaml"
    assert result.content == "kind: ConfigMap"


def test_verification_contract_requires_score_in_unit_interval():
    valid = parse_contract("verify", """{
        "score": 0.8,
        "passed": true,
        "issues": [],
        "suggestion": "none"
    }""")
    assert isinstance(valid, VerificationResult)

    with pytest.raises(ContractParseError, match="verify"):
        parse_contract("verify", """{
            "score": 1.2,
            "passed": true,
            "issues": [],
            "suggestion": "none"
        }""")


def test_structured_contracts_tolerate_fences_and_literal_control_characters():
        verification = parse_contract("verify", """```json
{
    "score": 0.8,
    "passed": true,
    "issues": ["line one
line two"],
    "suggestion": "none"
}
```""")
        assessment = parse_contract("assess", """```json
{
    "labels": ["CODE_GENERATION"],
    "verdict": "ok",
    "strengths": [],
    "gaps": []
}
```""")

        assert verification.score == 0.8
        assert isinstance(assessment, AssessmentResult)


def test_contract_models_reject_invalid_direct_construction():
    with pytest.raises(ValidationError):
        VerificationResult(score=-0.1, passed=False, issues=[], suggestion="fix it")