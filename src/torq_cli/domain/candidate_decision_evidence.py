"""Closed review and application audit contracts for candidate decisions."""

from __future__ import annotations

import re
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

REVIEW_CONTRACT = "torq-candidate-review-v1"
APPLY_CONTRACT = "torq-candidate-apply-v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HASH = re.compile(r"sha256:[a-f0-9]{64}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,255}")

CANDIDATE_KEYS = {
    "task_id", "ready_sequence", "ready_receipt_hash", "terminal_manifest_hash",
    "plan_hash", "base_scope_manifest_hash", "input_hash", "candidate_scope_manifest_hash",
    "check_sequence", "check_receipt_hash", "review_hash",
}
DECISION_KEYS = {"decision_id", "decision_sequence", "decision_receipt_hash", "terminal_manifest_hash"}
ACTOR_KEYS = {"subject_id", "assurance"}
COMMON_REVIEW = {"review_contract", "event", "decision_id", "request_digest", "candidate", "actor", "detail"}
COMMON_APPLY = {"apply_contract", "event", "application_id", "request_digest", "candidate", "acceptance", "actor", "detail"}


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _artifact(value: object) -> bool:
    return isinstance(value, str) and _ARTIFACT.fullmatch(value) is not None and ".." not in value.split("/")


def validate_candidate_ref(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == CANDIDATE_KEYS
        and _identifier(value.get("task_id"))
        and _positive(value.get("ready_sequence"))
        and _positive(value.get("check_sequence"))
        and all(_digest(value.get(key)) for key in CANDIDATE_KEYS - {"task_id", "ready_sequence", "check_sequence"})
    )


def _receipt_hash(row: Mapping[str, Any]) -> str:
    existing = row.get("receipt_hash")
    if isinstance(existing, str):
        return existing
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_decision_ref(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == DECISION_KEYS
        and _identifier(value.get("decision_id"))
        and _positive(value.get("decision_sequence"))
        and _digest(value.get("decision_receipt_hash"))
        and _digest(value.get("terminal_manifest_hash"))
    )


def _actor(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == ACTOR_KEYS
        and _identifier(value.get("subject_id"))
        and value.get("assurance") == "local_operator_session"
    )


def _artifact_ref(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"artifact", "artifact_hash", "content_hash"}
        and _artifact(value.get("artifact"))
        and _digest(value.get("artifact_hash"))
        and _digest(value.get("content_hash"))
    )


def validate_review_payload(payload: Mapping[str, Any]) -> str | None:
    if payload.get("review_contract") != REVIEW_CONTRACT:
        return None
    if set(payload) != COMMON_REVIEW or not _identifier(payload.get("decision_id")):
        return "candidate_review_payload_invalid"
    if not _digest(payload.get("request_digest")) or not validate_candidate_ref(payload.get("candidate")) or not _actor(payload.get("actor")):
        return "candidate_review_identity_invalid"
    detail = payload.get("detail")
    if not isinstance(detail, Mapping):
        return "candidate_review_detail_invalid"
    event = payload.get("event")
    if event == "candidate_accepted":
        if set(detail) != {"review_artifact", "source_scope_hash"} or not _artifact_ref(detail.get("review_artifact")) or not _digest(detail.get("source_scope_hash")):
            return "candidate_accept_detail_invalid"
    elif event == "correction_requested":
        if (
            set(detail) != {"correction_revision", "correction_hash", "child_plan_hash"}
            or not _positive(detail.get("correction_revision"))
            or not _digest(detail.get("correction_hash"))
            or not _digest(detail.get("child_plan_hash"))
        ):
            return "candidate_correction_detail_invalid"
    else:
        return "candidate_review_event_invalid"
    return None


_APPLY_DETAILS = {
    "apply_authority_created": {"installation_id"},
    "apply_rejected": {"reason_code"},
    "apply_prepared": {"journal_artifact", "journal_hash", "primary_identity_hash", "operation_count"},
    "apply_started": {"prepared_sequence", "prepared_receipt_hash"},
    "apply_file_staged": {"started_sequence", "operation_index", "path", "result_hash", "staged_identity"},
    "apply_file_written": {"started_sequence", "staged_sequence", "staged_receipt_hash", "operation_index", "path", "result_hash"},
    "apply_rollback_started": {"caused_by_sequence", "reason_code"},
    "apply_rollback_file_staged": {
        "rollback_started_sequence", "operation_index", "path", "base_hash",
        "staged_identity",
    },
    "apply_rollback_file_restored": {
        "rollback_started_sequence", "operation_index", "path", "base_hash",
        "staged_sequence", "staged_receipt_hash",
    },
    "apply_recovery_required": {"caused_by_sequence", "reason_code"},
    "candidate_applied": {"started_sequence", "journal_hash", "result_scope_manifest_hash", "operation_count"},
    "apply_rolled_back": {"prepared_sequence", "journal_hash", "restored_scope_manifest_hash"},
}


def validate_apply_payload(payload: Mapping[str, Any]) -> str | None:
    if payload.get("apply_contract") != APPLY_CONTRACT:
        return None
    if set(payload) != COMMON_APPLY or not _identifier(payload.get("application_id")):
        return "candidate_apply_payload_invalid"
    if (
        not _digest(payload.get("request_digest"))
        or not validate_candidate_ref(payload.get("candidate"))
        or not validate_decision_ref(payload.get("acceptance"))
        or not _actor(payload.get("actor"))
    ):
        return "candidate_apply_identity_invalid"
    event = payload.get("event")
    detail = payload.get("detail")
    if not isinstance(event, str) or event not in _APPLY_DETAILS or not isinstance(detail, Mapping) or set(detail) != _APPLY_DETAILS[event]:
        return "candidate_apply_detail_invalid"
    if event == "apply_authority_created" and not _identifier(detail.get("installation_id")):
        return "candidate_apply_detail_invalid"
    if event in {"apply_rejected", "apply_rollback_started", "apply_recovery_required"} and not (
        isinstance(detail.get("reason_code"), str) and _CODE.fullmatch(str(detail["reason_code"]))
    ):
        return "candidate_apply_detail_invalid"
    if event == "apply_prepared" and not (
        _artifact_ref(detail.get("journal_artifact"))
        and _digest(detail.get("journal_hash"))
        and _digest(detail.get("primary_identity_hash"))
        and _positive(detail.get("operation_count"))
    ):
        return "candidate_apply_detail_invalid"
    for key in ("prepared_sequence", "started_sequence", "caused_by_sequence"):
        if key in detail and not _positive(detail.get(key)):
            return "candidate_apply_detail_invalid"
    if "prepared_receipt_hash" in detail and not _digest(detail.get("prepared_receipt_hash")):
        return "candidate_apply_detail_invalid"
    if "journal_hash" in detail and not _digest(detail.get("journal_hash")):
        return "candidate_apply_detail_invalid"
    if "result_scope_manifest_hash" in detail and not _digest(detail.get("result_scope_manifest_hash")):
        return "candidate_apply_detail_invalid"
    if "restored_scope_manifest_hash" in detail and not _digest(detail.get("restored_scope_manifest_hash")):
        return "candidate_apply_detail_invalid"
    if event in {
        "apply_file_staged", "apply_file_written", "apply_rollback_file_staged",
        "apply_rollback_file_restored",
    } and not (
        isinstance(detail.get("operation_index"), int)
        and not isinstance(detail.get("operation_index"), bool)
        and detail["operation_index"] >= 0
        and isinstance(detail.get("path"), str)
    ):
        return "candidate_apply_detail_invalid"
    if event in {"apply_file_staged", "apply_file_written"} and not _digest(
        detail.get("result_hash")
    ):
        return "candidate_apply_detail_invalid"
    if event in {"apply_rollback_file_staged", "apply_rollback_file_restored"} and not (
        _positive(detail.get("rollback_started_sequence"))
        and (detail.get("base_hash") is None or _digest(detail.get("base_hash")))
    ):
        return "candidate_apply_detail_invalid"
    for identity_key in ("staged_identity",):
        if identity_key in detail:
            identity = detail.get(identity_key)
            if (
                not isinstance(identity, Mapping)
                or set(identity) != {"volume", "file_id"}
                or not all(isinstance(identity.get(key), int) and not isinstance(identity.get(key), bool) and identity[key] >= 0 for key in ("volume", "file_id"))
            ):
                return "candidate_apply_detail_invalid"
    if event in {"apply_file_written", "apply_rollback_file_restored"} and not (
        _positive(detail.get("staged_sequence")) and _digest(detail.get("staged_receipt_hash"))
    ):
        return "candidate_apply_detail_invalid"
    if "operation_count" in detail and not _positive(detail.get("operation_count")):
        return "candidate_apply_detail_invalid"
    return None


def validate_decision_receipt_contract(receipts: Sequence[Mapping[str, Any]], *, sealed: bool) -> str | None:
    review: list[tuple[int, Mapping[str, Any]]] = []
    apply: list[tuple[int, Mapping[str, Any]]] = []
    for row in receipts:
        payload = row.get("payload")
        sequence = row.get("sequence")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("review_contract") == REVIEW_CONTRACT:
            if row.get("transition") != "audit" or not _positive(sequence):
                return "candidate_review_receipt_invalid"
            finding = validate_review_payload(payload)
            if finding:
                return finding
            assert isinstance(sequence, int) and not isinstance(sequence, bool)
            review.append((int(sequence), payload))
        if payload.get("apply_contract") == APPLY_CONTRACT:
            if row.get("transition") != "audit" or not _positive(sequence):
                return "candidate_apply_receipt_invalid"
            finding = validate_apply_payload(payload)
            if finding:
                return finding
            assert isinstance(sequence, int) and not isinstance(sequence, bool)
            apply.append((int(sequence), payload))
    if review:
        if len(review) != 1:
            return "candidate_review_lifecycle_invalid"
        payload = review[0][1]
        if row_run_id(receipts, review[0][0]) != payload.get("decision_id"):
            return "candidate_review_run_id_mismatch"
    if apply:
        common = {key: apply[0][1][key] for key in COMMON_APPLY - {"event", "detail"}}
        if any({key: payload[key] for key in common} != common for _, payload in apply):
            return "candidate_apply_context_changed"
        events = [str(payload["event"]) for _, payload in apply]
        if events[0] != "apply_authority_created" or events.count("apply_authority_created") != 1:
            return "candidate_apply_begin_invalid"
        terminal = {"apply_rejected", "candidate_applied", "apply_rolled_back"}
        if any(event in terminal for event in events[:-1]):
            return "candidate_apply_after_terminal"
        if sealed and events[-1] not in terminal:
            return "candidate_apply_terminal_invalid"
        if row_run_id(receipts, apply[0][0]) != apply[0][1].get("application_id"):
            return "candidate_apply_run_id_mismatch"
        if any(row_run_id(receipts, sequence) != apply[0][1].get("application_id") for sequence, _ in apply):
            return "candidate_apply_run_id_mismatch"
        if events == ["apply_authority_created"]:
            return "candidate_apply_terminal_invalid" if sealed else None
        if events[1] == "apply_rejected":
            return None if len(events) == 2 else "candidate_apply_after_terminal"
        if events[1] != "apply_prepared" or events.count("apply_prepared") != 1:
            return "candidate_apply_prepared_order_invalid"
        prepared_sequence, prepared = apply[1]
        prepared_hash = next(
            (_receipt_hash(row) for row in receipts if row.get("sequence") == prepared_sequence),
            None,
        )
        operation_count = prepared["detail"]["operation_count"]
        journal_hash = prepared["detail"]["journal_hash"]
        started_rows = [(sequence, body) for sequence, body in apply if body["event"] == "apply_started"]
        if events[-1] == "apply_rejected":
            return None if len(events) == 3 else "candidate_apply_after_terminal"
        if len(started_rows) > 1:
            return "candidate_apply_started_order_invalid"
        started_sequence: int | None = None
        if started_rows:
            started_sequence, started_body = started_rows[0]
            if (
                events.index("apply_started") != 2
                or started_body["detail"].get("prepared_sequence") != prepared_sequence
                or started_body["detail"].get("prepared_receipt_hash") != prepared_hash
            ):
                return "candidate_apply_started_order_invalid"
        staged_rows = [(sequence, body) for sequence, body in apply if body["event"] == "apply_file_staged"]
        written_rows = [(sequence, body) for sequence, body in apply if body["event"] == "apply_file_written"]
        if (
            [body["detail"]["operation_index"] for _, body in staged_rows]
            != list(range(len(staged_rows)))
            or len(staged_rows) > operation_count
            or staged_rows and started_sequence is None
            or started_sequence is not None
            and any(body["detail"].get("started_sequence") != started_sequence for _, body in staged_rows)
            or len({body["detail"].get("path") for _, body in staged_rows}) != len(staged_rows)
        ):
            return "candidate_apply_progress_order_invalid"
        if written_rows:
            if started_sequence is None:
                return "candidate_apply_progress_order_invalid"
            if [body["detail"]["operation_index"] for _, body in written_rows] != list(range(len(written_rows))):
                return "candidate_apply_progress_order_invalid"
            if any(body["detail"].get("started_sequence") != started_sequence for _, body in written_rows):
                return "candidate_apply_progress_order_invalid"
            paths = [body["detail"].get("path") for _, body in written_rows]
            if len(paths) != len(set(paths)) or len(written_rows) > operation_count:
                return "candidate_apply_progress_order_invalid"
            staged_by_index = {body["detail"]["operation_index"]: (sequence, body) for sequence, body in staged_rows}
            for _, body in written_rows:
                staged = staged_by_index.get(body["detail"]["operation_index"])
                if (
                    staged is None
                    or body["detail"].get("staged_sequence") != staged[0]
                    or body["detail"].get("staged_receipt_hash")
                    != next((_receipt_hash(row) for row in receipts if row.get("sequence") == staged[0]), None)
                    or body["detail"].get("result_hash") != staged[1]["detail"].get("result_hash")
                ):
                    return "candidate_apply_progress_link_invalid"
        rollback_rows = [(sequence, body) for sequence, body in apply if body["event"] == "apply_rollback_started"]
        rollback_staged: list[tuple[int, Mapping[str, Any]]] = []
        rollback_restored: list[tuple[int, Mapping[str, Any]]] = []
        if len(rollback_rows) > 1:
            return "candidate_apply_rollback_order_invalid"
        if rollback_rows:
            rollback_index = events.index("apply_rollback_started")
            if rollback_rows[0][1]["detail"].get("caused_by_sequence") != apply[rollback_index - 1][0]:
                return "candidate_apply_rollback_order_invalid"
            allowed_rollback_tail = {
                "apply_rollback_file_staged", "apply_rollback_file_restored",
                "apply_recovery_required", "apply_rolled_back",
            }
            if any(event not in allowed_rollback_tail for event in events[rollback_index + 1 :]):
                return "candidate_apply_rollback_order_invalid"
            rollback_started_sequence = rollback_rows[0][0]
            rollback_staged = [
                (sequence, body) for sequence, body in apply
                if body["event"] == "apply_rollback_file_staged"
            ]
            rollback_restored = [
                (sequence, body) for sequence, body in apply
                if body["event"] == "apply_rollback_file_restored"
            ]
            indices = [body["detail"]["operation_index"] for _, body in rollback_staged]
            if (
                len(indices) != len(set(indices))
                or any(
                    body["detail"].get("rollback_started_sequence")
                    != rollback_started_sequence
                    for _, body in rollback_staged + rollback_restored
                )
                or indices != sorted(indices, reverse=True)
            ):
                return "candidate_apply_rollback_progress_invalid"
            staged_by_index = {
                body["detail"]["operation_index"]: (sequence, body)
                for sequence, body in rollback_staged
            }
            restored_indices: list[int] = []
            for _, body in rollback_restored:
                detail = body["detail"]
                index = detail["operation_index"]
                staged = staged_by_index.get(index)
                if (
                    staged is None
                    or index in restored_indices
                    or detail.get("staged_sequence") != staged[0]
                    or detail.get("staged_receipt_hash")
                    != next(
                        (_receipt_hash(row) for row in receipts if row.get("sequence") == staged[0]),
                        None,
                    )
                    or detail.get("path") != staged[1]["detail"].get("path")
                    or detail.get("base_hash") != staged[1]["detail"].get("base_hash")
                ):
                    return "candidate_apply_rollback_progress_invalid"
                restored_indices.append(index)
            if restored_indices != indices[: len(restored_indices)]:
                return "candidate_apply_rollback_progress_invalid"
        for index, (sequence, body) in enumerate(apply):
            del sequence
            if body["event"] == "apply_recovery_required" and (
                index == 0 or body["detail"].get("caused_by_sequence") != apply[index - 1][0]
            ):
                return "candidate_apply_recovery_link_invalid"
        if events[-1] == "candidate_applied":
            detail = apply[-1][1]["detail"]
            if (
                started_sequence is None
                or detail.get("started_sequence") != started_sequence
                or detail.get("journal_hash") != journal_hash
                or detail.get("operation_count") != operation_count
                or len(written_rows) != operation_count
                or detail.get("result_scope_manifest_hash")
                != apply[0][1]["candidate"].get("candidate_scope_manifest_hash")
            ):
                return "candidate_apply_terminal_link_invalid"
        elif events[-1] == "apply_rolled_back":
            detail = apply[-1][1]["detail"]
            if (
                not rollback_rows
                or detail.get("prepared_sequence") != prepared_sequence
                or detail.get("journal_hash") != journal_hash
                or len(rollback_restored) != len(rollback_staged)
                or detail.get("restored_scope_manifest_hash")
                != apply[0][1]["candidate"].get("base_scope_manifest_hash")
            ):
                return "candidate_apply_terminal_link_invalid"
        elif events[-1] not in {
            "apply_recovery_required", "apply_prepared", "apply_started", "apply_file_staged",
            "apply_file_written", "apply_rollback_started", "apply_rollback_file_staged",
            "apply_rollback_file_restored",
        }:
            return "candidate_apply_lifecycle_invalid"
    return None


def row_run_id(receipts: Sequence[Mapping[str, Any]], sequence: int) -> object:
    return next((row.get("run_id") for row in receipts if row.get("sequence") == sequence), None)


__all__ = [
    "APPLY_CONTRACT", "CANDIDATE_KEYS", "DECISION_KEYS", "REVIEW_CONTRACT",
    "validate_apply_payload", "validate_candidate_ref", "validate_decision_receipt_contract",
    "validate_decision_ref", "validate_review_payload",
]
