"""Closed semantic contract for bounded goal-to-candidate task evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

TASK_CONTRACT = "torq-candidate-task-v1"
TASK_EVENTS = frozenset(
    {
        "task_start_accepted",
        "candidate_generated",
        "candidate_check_completed",
        "candidate_ready",
        "task_failed",
        "task_interrupted",
        "task_cancelled",
    }
)
TERMINAL_TASK_EVENTS = frozenset(
    {"candidate_ready", "task_failed", "task_interrupted", "task_cancelled"}
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\\._-]{0,255}")
_PHASES = frozenset({"reserved", "preparing", "generating", "checking"})
_TERMINATIONS = frozenset({"not_started", "confirmed_empty", "unknown"})

_COMMON = frozenset({"task_contract", "event", "task_id", "provider_dispatch"})
_EVENT_KEYS: Mapping[str, frozenset[str]] = {
    "task_start_accepted": _COMMON
    | frozenset(
        {
            "request_digest",
            "plan_hash",
            "base_manifest_hash",
            "input_hash",
            "artifact",
            "artifact_hash",
            "artifact_content_hash",
            "provider",
            "model",
        }
    ),
    "candidate_generated": _COMMON
    | frozenset(
        {
            "start_sequence",
            "plan_hash",
            "input_hash",
            "candidate_manifest_hash",
            "raw_response_hash",
            "artifact",
            "artifact_hash",
            "artifact_content_hash",
            "provider",
            "model",
            "termination",
        }
    ),
    "candidate_check_completed": _COMMON
    | frozenset(
        {
            "generated_sequence",
            "candidate_manifest_hash",
            "check_profile_id",
            "check_profile_version",
            "helper_hash",
            "argv_hash",
            "exit_code",
            "output_hash",
            "output_complete",
            "termination",
            "artifact",
            "artifact_hash",
            "artifact_content_hash",
        }
    ),
    "candidate_ready": _COMMON
    | frozenset(
        {
            "start_sequence",
            "generated_sequence",
            "check_sequence",
            "plan_hash",
            "base_manifest_hash",
            "input_hash",
            "candidate_manifest_hash",
            "source_recheck_hash",
        }
    ),
    "task_failed": _COMMON
    | frozenset({"phase", "reason_code", "caused_by_sequence", "termination"}),
    "task_interrupted": _COMMON
    | frozenset({"phase", "reason_code", "caused_by_sequence", "termination"}),
    "task_cancelled": _COMMON
    | frozenset({"phase", "reason_code", "caused_by_sequence", "termination"}),
}


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _artifact(value: object) -> bool:
    return (
        isinstance(value, str)
        and _ARTIFACT.fullmatch(value) is not None
        and ".." not in value.replace("\\", "/").split("/")
    )


def _positive_sequence(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_task_audit_payload(payload: Mapping[str, Any]) -> str | None:
    """Validate one discriminated audit body without upgrading generic audit."""

    if payload.get("task_contract") != TASK_CONTRACT:
        return None
    event = payload.get("event")
    if not isinstance(event, str) or event not in TASK_EVENTS:
        return "task_event_invalid"
    if set(payload) != _EVENT_KEYS[event]:
        return "task_payload_keys_invalid"
    if not _identifier(payload.get("task_id")):
        return "task_id_invalid"
    if not isinstance(payload.get("provider_dispatch"), bool):
        return "task_provider_dispatch_invalid"

    if event == "task_start_accepted":
        if payload["provider_dispatch"] is not False:
            return "task_provider_dispatch_invalid"
        if not all(
            _digest(payload.get(field))
            for field in (
                "request_digest",
                "plan_hash",
                "base_manifest_hash",
                "input_hash",
                "artifact_hash",
                "artifact_content_hash",
            )
        ):
            return "task_hash_invalid"
        if not _artifact(payload.get("artifact")):
            return "task_artifact_invalid"
        if not _identifier(payload.get("provider")) or not _identifier(payload.get("model")):
            return "task_provider_invalid"
    elif event == "candidate_generated":
        if payload["provider_dispatch"] is not True:
            return "task_provider_dispatch_invalid"
        if not _positive_sequence(payload.get("start_sequence")):
            return "task_sequence_link_invalid"
        if not all(
            _digest(payload.get(field))
            for field in (
                "plan_hash",
                "input_hash",
                "candidate_manifest_hash",
                "raw_response_hash",
                "artifact_hash",
                "artifact_content_hash",
            )
        ):
            return "task_hash_invalid"
        if not _artifact(payload.get("artifact")):
            return "task_artifact_invalid"
        if not _identifier(payload.get("provider")) or not _identifier(payload.get("model")):
            return "task_provider_invalid"
        if payload.get("termination") != "confirmed_empty":
            return "task_termination_invalid"
    elif event == "candidate_check_completed":
        if payload["provider_dispatch"] is not False:
            return "task_provider_dispatch_invalid"
        if not _positive_sequence(payload.get("generated_sequence")):
            return "task_sequence_link_invalid"
        if not all(
            _digest(payload.get(field))
            for field in (
                "candidate_manifest_hash",
                "helper_hash",
                "argv_hash",
                "output_hash",
                "artifact_hash",
                "artifact_content_hash",
            )
        ):
            return "task_hash_invalid"
        if not _artifact(payload.get("artifact")):
            return "task_artifact_invalid"
        if payload.get("check_profile_id") != "structural-v1":
            return "task_check_profile_invalid"
        if payload.get("check_profile_version") != "1.0.0":
            return "task_check_profile_invalid"
        exit_code = payload.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return "task_check_exit_invalid"
        if not isinstance(payload.get("output_complete"), bool):
            return "task_check_output_invalid"
        if payload.get("termination") != "confirmed_empty":
            return "task_termination_invalid"
    elif event == "candidate_ready":
        if payload["provider_dispatch"] is not False:
            return "task_provider_dispatch_invalid"
        if not all(
            _positive_sequence(payload.get(field))
            for field in ("start_sequence", "generated_sequence", "check_sequence")
        ):
            return "task_sequence_link_invalid"
        if not all(
            _digest(payload.get(field))
            for field in (
                "plan_hash",
                "base_manifest_hash",
                "input_hash",
                "candidate_manifest_hash",
                "source_recheck_hash",
            )
        ):
            return "task_hash_invalid"
    else:
        if payload.get("phase") not in _PHASES:
            return "task_phase_invalid"
        reason = payload.get("reason_code")
        if not isinstance(reason, str) or _CODE.fullmatch(reason) is None:
            return "task_reason_invalid"
        caused = payload.get("caused_by_sequence")
        if caused is not None and not _positive_sequence(caused):
            return "task_sequence_link_invalid"
        termination = payload.get("termination")
        if termination not in _TERMINATIONS:
            return "task_termination_invalid"
        if event == "task_cancelled" and not (
            termination == "confirmed_empty"
            or termination == "not_started" and payload["provider_dispatch"] is False
        ):
            return "task_cancel_termination_invalid"
    return None


def validate_task_receipt_contract(
    receipts: Sequence[Mapping[str, Any]], *, sealed: bool
) -> str | None:
    """Validate the ordered inner task state machine across signed receipts."""

    task_rows: list[tuple[int, Mapping[str, Any]]] = []
    terminal_sequence: int | None = None
    for receipt in receipts:
        payload = receipt.get("payload")
        sequence = receipt.get("sequence")
        if terminal_sequence is not None:
            return "task_receipt_after_terminal"
        if not isinstance(payload, Mapping) or payload.get("task_contract") != TASK_CONTRACT:
            continue
        if receipt.get("transition") != "audit":
            return "task_transition_invalid"
        if receipt.get("run_id") != payload.get("task_id"):
            return "task_run_id_mismatch"
        if not _positive_sequence(sequence):
            return "task_sequence_link_invalid"
        finding = validate_task_audit_payload(payload)
        if finding is not None:
            return finding
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        task_rows.append((sequence, payload))
        if payload.get("event") in TERMINAL_TASK_EVENTS:
            terminal_sequence = sequence

    if not task_rows:
        return None
    task_id = task_rows[0][1].get("task_id")
    if any(payload.get("task_id") != task_id for _, payload in task_rows):
        return "task_id_mismatch"
    events = [str(payload["event"]) for _, payload in task_rows]
    if len(task_rows) == 1 and events[0] in {
        "task_failed",
        "task_interrupted",
        "task_cancelled",
    }:
        early_terminal = task_rows[0][1]
        if (
            early_terminal.get("caused_by_sequence") is not None
            or early_terminal.get("phase") != "reserved"
            or early_terminal.get("provider_dispatch") is not False
            or early_terminal.get("termination") != "not_started"
        ):
            return "task_terminal_link_invalid"
        if sealed is False:
            return None
        return None
    if events[0] != "task_start_accepted" or events.count("task_start_accepted") != 1:
        return "task_start_order_invalid"
    if sum(event in TERMINAL_TASK_EVENTS for event in events) > 1:
        return "task_terminal_duplicate"

    start_sequence, start = task_rows[0]
    generated_row = next(
        ((sequence, body) for sequence, body in task_rows if body["event"] == "candidate_generated"),
        None,
    )
    check_row = next(
        (
            (sequence, body)
            for sequence, body in task_rows
            if body["event"] == "candidate_check_completed"
        ),
        None,
    )
    if events.count("candidate_generated") > 1 or events.count("candidate_check_completed") > 1:
        return "task_event_duplicate"
    if generated_row is not None:
        generated_sequence, generated = generated_row
        if (
            generated_sequence <= start_sequence
            or generated.get("start_sequence") != start_sequence
            or generated.get("plan_hash") != start.get("plan_hash")
            or generated.get("input_hash") != start.get("input_hash")
            or generated.get("provider") != start.get("provider")
            or generated.get("model") != start.get("model")
        ):
            return "task_generated_link_invalid"
    if check_row is not None:
        check_sequence, check = check_row
        if (
            generated_row is None
            or check_sequence <= generated_row[0]
            or check.get("generated_sequence") != generated_row[0]
            or check.get("candidate_manifest_hash")
            != generated_row[1].get("candidate_manifest_hash")
        ):
            return "task_check_link_invalid"

    terminal = task_rows[-1][1] if task_rows[-1][1]["event"] in TERMINAL_TASK_EVENTS else None
    if terminal is not None and terminal["event"] == "candidate_ready":
        if generated_row is None or check_row is None:
            return "task_ready_prerequisite_missing"
        if (
            check_row[1].get("exit_code") != 0
            or check_row[1].get("output_complete") is not True
            or check_row[1].get("termination") != "confirmed_empty"
            or terminal.get("start_sequence") != start_sequence
            or terminal.get("generated_sequence") != generated_row[0]
            or terminal.get("check_sequence") != check_row[0]
            or terminal.get("plan_hash") != start.get("plan_hash")
            or terminal.get("base_manifest_hash") != start.get("base_manifest_hash")
            or terminal.get("input_hash") != start.get("input_hash")
            or terminal.get("candidate_manifest_hash")
            != generated_row[1].get("candidate_manifest_hash")
            or terminal.get("source_recheck_hash") != start.get("base_manifest_hash")
        ):
            return "task_ready_link_invalid"
    if terminal is not None and terminal["event"] != "candidate_ready":
        preceding = task_rows[-2][0] if len(task_rows) > 1 else None
        if terminal.get("caused_by_sequence") != preceding:
            return "task_terminal_link_invalid"
    if sealed and terminal is None:
        return "task_terminal_missing"
    return None


__all__ = [
    "TASK_CONTRACT",
    "TASK_EVENTS",
    "TERMINAL_TASK_EVENTS",
    "validate_task_audit_payload",
    "validate_task_receipt_contract",
]
