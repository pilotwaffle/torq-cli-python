from __future__ import annotations

from typing import Any

from torq_cli.domain.candidate_decision_evidence import (
    APPLY_CONTRACT,
    validate_decision_receipt_contract,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _candidate() -> dict[str, Any]:
    return {
        "task_id": "run-task-" + "1" * 24,
        "ready_sequence": 4,
        "ready_receipt_hash": _digest("1"),
        "terminal_manifest_hash": _digest("2"),
        "plan_hash": _digest("3"),
        "base_scope_manifest_hash": _digest("4"),
        "input_hash": _digest("5"),
        "candidate_scope_manifest_hash": _digest("6"),
        "check_sequence": 3,
        "check_receipt_hash": _digest("7"),
        "review_hash": _digest("8"),
    }


def _common() -> dict[str, Any]:
    return {
        "apply_contract": APPLY_CONTRACT,
        "application_id": "application-" + "a" * 24,
        "request_digest": _digest("9"),
        "candidate": _candidate(),
        "acceptance": {
            "decision_id": "decision-" + "b" * 24,
            "decision_sequence": 1,
            "decision_receipt_hash": _digest("a"),
            "terminal_manifest_hash": _digest("b"),
        },
        "actor": {"subject_id": "session-one", "assurance": "local_operator_session"},
    }


def _row(sequence: int, event: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": "application-" + "a" * 24,
        "sequence": sequence,
        "transition": "audit",
        "receipt_hash": _digest(format(sequence, "x")),
        "payload": {**_common(), "event": event, "detail": detail},
    }


def test_prepared_application_can_seal_as_rejected_before_start() -> None:
    rows = [
        _row(1, "apply_authority_created", {"installation_id": "installation-one"}),
        _row(2, "apply_prepared", {
            "journal_artifact": {
                "artifact": "artifacts/primary-journal.enc",
                "artifact_hash": _digest("c"), "content_hash": _digest("d"),
            },
            "journal_hash": _digest("d"), "primary_identity_hash": _digest("e"),
            "operation_count": 1,
        }),
        _row(3, "apply_rejected", {"reason_code": "task_apply_recovery_aborted"}),
    ]
    assert validate_decision_receipt_contract(rows, sealed=True) is None


def test_staged_and_written_progress_must_crosslink_exact_receipt() -> None:
    rows = [
        _row(1, "apply_authority_created", {"installation_id": "installation-one"}),
        _row(2, "apply_prepared", {
            "journal_artifact": {
                "artifact": "artifacts/primary-journal.enc",
                "artifact_hash": _digest("c"), "content_hash": _digest("d"),
            },
            "journal_hash": _digest("d"), "primary_identity_hash": _digest("e"),
            "operation_count": 1,
        }),
        _row(3, "apply_started", {
            "prepared_sequence": 2, "prepared_receipt_hash": _digest("2"),
        }),
        _row(4, "apply_file_staged", {
            "started_sequence": 3, "operation_index": 0, "path": "a.py",
            "result_hash": _digest("f"), "staged_identity": {"volume": 1, "file_id": 2},
        }),
        _row(5, "apply_file_written", {
            "started_sequence": 3, "staged_sequence": 4,
            "staged_receipt_hash": _digest("4"), "operation_index": 0,
            "path": "a.py", "result_hash": _digest("f"),
        }),
        _row(6, "candidate_applied", {
            "started_sequence": 3, "journal_hash": _digest("d"),
            "result_scope_manifest_hash": _digest("6"), "operation_count": 1,
        }),
    ]
    assert validate_decision_receipt_contract(rows, sealed=True) is None
    rows[4]["payload"]["detail"]["staged_receipt_hash"] = _digest("0")
    assert (
        validate_decision_receipt_contract(rows, sealed=True)
        == "candidate_apply_progress_link_invalid"
    )


def test_rollback_progress_is_reverse_order_and_requires_restored_crosslinks() -> None:
    rows = [
        _row(1, "apply_authority_created", {"installation_id": "installation-one"}),
        _row(2, "apply_prepared", {
            "journal_artifact": {
                "artifact": "artifacts/primary-journal.enc",
                "artifact_hash": _digest("c"), "content_hash": _digest("d"),
            },
            "journal_hash": _digest("d"), "primary_identity_hash": _digest("e"),
            "operation_count": 1,
        }),
        _row(3, "apply_started", {
            "prepared_sequence": 2, "prepared_receipt_hash": _digest("2"),
        }),
        _row(4, "apply_file_staged", {
            "started_sequence": 3, "operation_index": 0, "path": "a.py",
            "result_hash": _digest("f"), "staged_identity": {"volume": 1, "file_id": 2},
        }),
        _row(5, "apply_file_written", {
            "started_sequence": 3, "staged_sequence": 4,
            "staged_receipt_hash": _digest("4"), "operation_index": 0,
            "path": "a.py", "result_hash": _digest("f"),
        }),
        _row(6, "apply_rollback_started", {
            "caused_by_sequence": 5, "reason_code": "task_apply_result_mismatch",
        }),
        _row(7, "apply_rollback_file_staged", {
            "rollback_started_sequence": 6, "operation_index": 0, "path": "a.py",
            "base_hash": _digest("0"), "staged_identity": {"volume": 1, "file_id": 3},
        }),
        _row(8, "apply_rollback_file_restored", {
            "rollback_started_sequence": 6, "operation_index": 0, "path": "a.py",
            "base_hash": _digest("0"), "staged_sequence": 7,
            "staged_receipt_hash": _digest("7"),
        }),
        _row(9, "apply_rolled_back", {
            "prepared_sequence": 2, "journal_hash": _digest("d"),
            "restored_scope_manifest_hash": _digest("4"),
        }),
    ]
    assert validate_decision_receipt_contract(rows, sealed=True) is None
    rows.pop(7)
    assert (
        validate_decision_receipt_contract(rows, sealed=True)
        == "candidate_apply_terminal_link_invalid"
    )
