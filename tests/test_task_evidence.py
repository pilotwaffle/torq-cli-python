from __future__ import annotations

from copy import deepcopy

from torq_cli.domain.run_evidence import validate_v2_receipt_contract
from torq_cli.domain.task_evidence import TASK_CONTRACT, validate_task_audit_payload


HASH = "sha256:" + "1" * 64


def _body(event: str) -> dict[str, object]:
    common: dict[str, object] = {
        "task_contract": TASK_CONTRACT,
        "event": event,
        "task_id": "run-task-1",
        "provider_dispatch": False,
    }
    if event == "task_start_accepted":
        return common | {
            "request_digest": HASH,
            "plan_hash": HASH,
            "base_manifest_hash": HASH,
            "input_hash": HASH,
            "artifact": "artifacts/start.enc",
            "artifact_hash": HASH,
            "artifact_content_hash": HASH,
            "provider": "claude",
            "model": "sonnet",
        }
    if event == "candidate_generated":
        return common | {
            "provider_dispatch": True,
            "start_sequence": 1,
            "plan_hash": HASH,
            "input_hash": HASH,
            "candidate_manifest_hash": HASH,
            "raw_response_hash": HASH,
            "artifact": "artifacts/candidate.enc",
            "artifact_hash": HASH,
            "artifact_content_hash": HASH,
            "provider": "claude",
            "model": "sonnet",
            "termination": "confirmed_empty",
        }
    if event == "candidate_check_completed":
        return common | {
            "generated_sequence": 2,
            "candidate_manifest_hash": HASH,
            "check_profile_id": "structural-v1",
            "check_profile_version": "1.0.0",
            "helper_hash": HASH,
            "argv_hash": HASH,
            "exit_code": 0,
            "output_hash": HASH,
            "output_complete": True,
            "termination": "confirmed_empty",
            "artifact": "artifacts/check.enc",
            "artifact_hash": HASH,
            "artifact_content_hash": HASH,
        }
    return common | {
        "start_sequence": 1,
        "generated_sequence": 2,
        "check_sequence": 3,
        "plan_hash": HASH,
        "base_manifest_hash": HASH,
        "input_hash": HASH,
        "candidate_manifest_hash": HASH,
        "source_recheck_hash": HASH,
    }


def _receipt(sequence: int, body: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": "run-task-1",
        "sequence": sequence,
        "transition": "audit",
        "payload": body,
    }


def test_task_semantic_contract_accepts_linked_ready_lifecycle() -> None:
    rows = [
        _receipt(index, _body(event))
        for index, event in enumerate(
            (
                "task_start_accepted",
                "candidate_generated",
                "candidate_check_completed",
                "candidate_ready",
            ),
            1,
        )
    ]
    assert validate_v2_receipt_contract(rows, sealed=True) is None


def test_generic_audit_cannot_upgrade_to_task_evidence() -> None:
    row = _receipt(1, {"status": "candidate_ready", "provider_dispatch": False})
    assert validate_task_audit_payload(row["payload"]) is None  # type: ignore[arg-type]


def test_task_payload_is_closed_and_bound_to_signed_run() -> None:
    body = _body("task_start_accepted")
    body["extra"] = "smuggled"
    assert validate_task_audit_payload(body) == "task_payload_keys_invalid"
    row = _receipt(1, _body("task_start_accepted"))
    row["run_id"] = "run-other"
    assert validate_v2_receipt_contract([row], sealed=False) == "task_run_id_mismatch"


def test_task_provider_and_model_cannot_change_after_start() -> None:
    rows = [
        _receipt(1, _body("task_start_accepted")),
        _receipt(2, _body("candidate_generated")),
    ]
    changed = deepcopy(rows)
    changed[1]["payload"]["model"] = "other"  # type: ignore[index]
    assert validate_v2_receipt_contract(changed, sealed=False) == "task_generated_link_invalid"


def test_task_ready_rejects_failed_check_and_post_terminal_receipt() -> None:
    rows = [
        _receipt(1, _body("task_start_accepted")),
        _receipt(2, _body("candidate_generated")),
        _receipt(3, _body("candidate_check_completed")),
        _receipt(4, _body("candidate_ready")),
    ]
    failed = deepcopy(rows)
    failed[2]["payload"]["exit_code"] = 1  # type: ignore[index]
    assert validate_v2_receipt_contract(failed, sealed=True) == "task_ready_link_invalid"
    rows.append(_receipt(5, {"status": "later", "provider_dispatch": False}))
    assert validate_v2_receipt_contract(rows, sealed=True) == "task_receipt_after_terminal"


def test_task_cannot_seal_without_exact_terminal() -> None:
    rows = [_receipt(1, _body("task_start_accepted"))]
    assert validate_v2_receipt_contract(rows, sealed=True) == "task_terminal_missing"


def test_pre_dispatch_interruption_can_be_the_only_terminal_receipt() -> None:
    body = {
        "task_contract": TASK_CONTRACT,
        "event": "task_interrupted",
        "task_id": "run-task-1",
        "phase": "reserved",
        "reason_code": "task_coordinator_restarted",
        "caused_by_sequence": None,
        "provider_dispatch": False,
        "termination": "not_started",
    }
    assert validate_v2_receipt_contract([_receipt(1, body)], sealed=True) is None
