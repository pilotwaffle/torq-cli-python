from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.safety import chat_evidence
from torq_cli.safety.chat_evidence import ChatEvidenceJournal, verify_chat_evidence
from torq_cli.safety.receipts import private_key_permissions_are_restricted


def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChatEvidenceJournal:
    root = tmp_path / "run-chat"
    root.mkdir()
    private = secrets.token_bytes(32)
    public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    monkeypatch.setattr(chat_evidence, "_certified_operator_key", lambda _root: public)
    return ChatEvidenceJournal(root, private)


def test_chat_evidence_is_signed_hash_chained_and_reopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    first = journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    second = journal.append("turn_started", {"turn_id": "turn-1", "worker_pid": 42})
    rows = verify_chat_evidence(journal.run_root)
    assert [row["sequence"] for row in rows] == [1, 2]
    assert second["previous_hash"] == first["hash"]


def test_chat_evidence_tampering_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    row = json.loads(journal.path.read_text(encoding="utf-8"))
    row["body"]["content"] = "forged"
    journal.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="chat_evidence_hash_invalid"):
        verify_chat_evidence(journal.run_root)


def test_wrong_operator_identity_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run-chat"
    root.mkdir()
    expected = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    monkeypatch.setattr(chat_evidence, "_certified_operator_key", lambda _root: expected)
    with pytest.raises(ValueError, match="chat_signing_identity_mismatch"):
        ChatEvidenceJournal(root, secrets.token_bytes(32))


def test_chat_evidence_rollback_is_rejected_by_external_signed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    first_bytes = journal.path.read_bytes()
    journal.append("turn_started", {"turn_id": "turn-1", "worker_pid": 42})
    journal.path.write_bytes(first_bytes)
    with pytest.raises(ValueError, match="chat_evidence_rollback_detected"):
        verify_chat_evidence(journal.run_root)


def test_deleting_complete_chat_journal_is_detected_by_external_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    journal.path.unlink()
    with pytest.raises(ValueError, match="chat_evidence_rollback_detected"):
        verify_chat_evidence(journal.run_root)


def test_one_row_head_lag_is_repaired_when_writer_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    first = journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    head_path = chat_evidence._head_path(journal.run_root)
    first_head = head_path.read_bytes()
    journal.append("turn_started", {"turn_id": "turn-1", "worker_pid": 42})
    head_path.write_bytes(first_head)
    reopened = ChatEvidenceJournal(
        journal.run_root,
        journal._private.private_bytes_raw(),
    )
    assert len(reopened.rows()) == 2
    assert first["sequence"] == 1


def test_false_confirmed_cancellation_observation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    journal.append("turn_started", {"turn_id": "turn-1", "worker_pid": 42})
    journal.append("turn_cancellation_requested", {"turn_id": "turn-1"})
    with pytest.raises(ValueError, match="chat_evidence_body_invalid"):
        journal.append(
            "turn_cancelled",
            {
                "turn_id": "turn-1",
                "returncode": "alive",
                "forced": "no",
                "containment_state": "unknown",
            },
        )


def test_two_journal_instances_cannot_fork_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _journal(tmp_path, monkeypatch)
    second = ChatEvidenceJournal(first.run_root, first._private.private_bytes_raw())
    first.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    with pytest.raises(ValueError, match="chat_evidence_external_change"):
        second.append(
            "turn_submitted",
            {"turn_id": "turn-2", "role": "user", "content": "hello", "attachments": []},
        )
    assert len(verify_chat_evidence(first.run_root)) == 1


def test_chat_journal_and_external_head_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    journal.append(
        "turn_submitted",
        {"turn_id": "turn-1", "role": "user", "content": "hello", "attachments": []},
    )
    assert private_key_permissions_are_restricted(journal.path)
    assert private_key_permissions_are_restricted(chat_evidence._head_path(journal.run_root))


def test_preexisting_hardlink_cannot_receive_signed_chat_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"")
    journal.path.hardlink_to(victim)

    with pytest.raises(ValueError, match="chat_evidence_path_unsafe"):
        journal.append(
            "turn_submitted",
            {"turn_id": "turn-1", "role": "user", "content": "secret", "attachments": []},
        )
    assert victim.read_bytes() == b""


def test_verified_attachment_schema_matches_runtime_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    invalid = {
        "attachment_id": "attachment-1",
        "name": "payload.svg",
        "media_type": "image/svg+xml",
        "size_bytes": 1,
        "sha256": "f" * 64,
    }
    with pytest.raises(ValueError, match="chat_evidence_attachment_invalid"):
        journal.append(
            "turn_submitted",
            {"turn_id": "turn-1", "role": "user", "content": "inspect", "attachments": [invalid]},
        )
