"""assessor's `labels` output has been observed echoing the system prompt's own
placeholder tokens (LABEL1/LABEL2) instead of real capability names -- see
issue #87. sanitize_labels() is a deterministic safety net: whatever the raw
LLM output contains, only values that are actually members of the real
taxonomy ever survive into a stored assessment. It cannot know whether a real
label is contextually correct for a given artifact (that's a model-quality
limit, not something a static filter can fix) -- it only guarantees the
result is never literal placeholder/garbage text.
"""

from src.agents.assessor import VALID_LABELS, sanitize_labels


def test_sanitize_labels_drops_placeholder_tokens():
    assert sanitize_labels(["LABEL1", "LABEL2"]) == []


def test_sanitize_labels_keeps_real_valid_labels():
    assert sanitize_labels(["CODE_GENERATION", "TESTING"]) == ["CODE_GENERATION", "TESTING"]


def test_sanitize_labels_keeps_real_labels_even_if_contextually_wrong():
    # Can't be fixed by a deterministic filter alone -- see issue #87's second
    # symptom (real-but-wrong label hallucination). This just documents the
    # boundary: sanitize_labels only guarantees "not garbage," not "correct."
    assert sanitize_labels(["DECOMPOSITION", "INFRA_AS_CODE"]) == ["DECOMPOSITION", "INFRA_AS_CODE"]


def test_sanitize_labels_drops_mixed_valid_and_placeholder_entries():
    assert sanitize_labels(["CODE_GENERATION", "LABEL2"]) == ["CODE_GENERATION"]


def test_sanitize_labels_handles_empty_and_missing_input():
    assert sanitize_labels([]) == []
    assert sanitize_labels(None) == []


def test_valid_labels_matches_the_system_prompt_taxonomy():
    from src.agents.assessor import SYSTEM
    for label in VALID_LABELS:
        assert label in SYSTEM
