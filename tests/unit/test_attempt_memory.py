"""Durable, bounded attempt evidence for future learning decisions."""

from __future__ import annotations

from src import memory


def _configure_memory_path(monkeypatch, tmp_path):
    path = tmp_path / "memory.json"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(path))
    monkeypatch.setattr(memory, "LOCK_PATH", f"{path}.lock")
    return path


async def test_attempt_evidence_persists_raw_and_normalized_output(monkeypatch, tmp_path):
    _configure_memory_path(monkeypatch, tmp_path)
    store = memory.MemoryStore()

    store.add_attempt({
        "attempt_id": "attempt-1",
        "packet_id": "packet-1",
        "role": "executor",
        "capability": "code",
        "raw_response": "```yaml\nkind: ConfigMap\n```",
        "normalized_response": "kind: ConfigMap",
        "validation_status": "valid",
        "environment_healthy": True,
    })
    await store.save()

    restored = memory.MemoryStore.load()
    assert restored.attempts[0]["attempt_id"] == "attempt-1"
    assert restored.attempts[0]["raw_response"].startswith("```")
    assert restored.attempts[0]["normalized_response"] == "kind: ConfigMap"
    assert restored.attempts[0]["environment_healthy"] is True
    assert restored.attempts[0]["ts"]


async def test_attempt_evidence_uses_a_bounded_retention_window(monkeypatch, tmp_path):
    _configure_memory_path(monkeypatch, tmp_path)
    monkeypatch.setattr(memory, "MAX_ATTEMPTS", 2)
    store = memory.MemoryStore()

    for index in range(3):
        store.add_attempt({"attempt_id": f"attempt-{index}"})
    await store.save()

    restored = memory.MemoryStore.load()
    assert [attempt["attempt_id"] for attempt in restored.attempts] == ["attempt-1", "attempt-2"]