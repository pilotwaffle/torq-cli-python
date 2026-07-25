"""Schema-v2 contracts for governed run, attempt, and action evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from torq_cli.domain.evidence_transitions import (
    transition_authority_finding,
    transition_rule,
)


ATTEMPT_TRANSITIONS = frozenset(
    {
        "stage_attempt_created",
        "stage_blocked",
        "stage_dispatch_started",
        "stage_completed",
        "stage_failed",
        "stage_interrupted",
    }
)
TERMINAL_ATTEMPT_TRANSITIONS = frozenset(
    {"stage_blocked", "stage_completed", "stage_failed", "stage_interrupted"}
)
CORE_LANES = ("g1d", "g1r", "builder", "g2a")
CONDITIONAL_LANES = ("refine_bug", "refine_ui")
LANE_ORDER = CORE_LANES + CONDITIONAL_LANES


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_receipt_payload(
    transition: str,
    payload: Mapping[str, Any],
    *,
    writer_role: str,
) -> str | None:
    """Validate the local shape that can be checked before append."""
    if transition in ATTEMPT_TRANSITIONS:
        if not isinstance(payload.get("role"), str):
            return "attempt_role_invalid"
        attempt_id = payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return "attempt_id_invalid"
        if not _positive_int(payload.get("attempt_ordinal")):
            return "attempt_ordinal_invalid"
        if not _nonnegative_int(payload.get("repair_cycle")):
            return "attempt_repair_cycle_invalid"
        dispatch = payload.get("provider_dispatch")
        if transition == "stage_attempt_created" and dispatch is not False:
            return "attempt_dispatch_invalid"
        if transition == "stage_blocked" and dispatch is not False:
            return "attempt_dispatch_invalid"
        if transition in {"stage_dispatch_started", "stage_completed"}:
            if dispatch is not True:
                return "attempt_dispatch_invalid"
        if transition == "stage_failed" and not isinstance(dispatch, bool):
            return "attempt_dispatch_invalid"
        if transition == "stage_interrupted" and dispatch not in {
            False,
            True,
            "unknown",
        }:
            return "attempt_dispatch_invalid"
    elif transition == "run_planned":
        catalog = payload.get("lane_catalog")
        if not isinstance(catalog, list) or len(catalog) != len(LANE_ORDER):
            return "lane_catalog_invalid"
        roles = [lane.get("role") for lane in catalog if isinstance(lane, Mapping)]
        if tuple(roles) != LANE_ORDER:
            return "lane_catalog_order_invalid"
    elif transition == "repair_routed":
        if payload.get("target_role") not in CONDITIONAL_LANES:
            return "repair_route_invalid"
        if not isinstance(payload.get("attempt_id"), str):
            return "repair_route_attempt_invalid"
        if not _positive_int(payload.get("attempt_ordinal")):
            return "repair_route_attempt_invalid"
        if not _positive_int(payload.get("cycle")):
            return "repair_route_cycle_invalid"
    elif transition == "action_opened":
        required: tuple[str, ...] = (
            "action_id",
            "type",
            "scope",
            "target",
            "summary",
        )
        if any(not isinstance(payload.get(field), str) for field in required):
            return "action_opened_invalid"
        if not _positive_int(payload.get("caused_by_sequence")):
            return "action_opened_invalid"
    elif transition == "action_resolved":
        required = ("action_id", "resolution", "resolver_identity")
        if any(not isinstance(payload.get(field), str) for field in required):
            return "action_resolved_invalid"
        if not _positive_int(payload.get("opened_sequence")):
            return "action_resolved_invalid"
    elif transition == "run_decision":
        rule = transition_rule(writer_role, transition)
        if rule is None or payload.get("status") not in rule.statuses:
            return "run_decision_status_invalid"
        if writer_role == "supervisor":
            if not _positive_int(payload.get("interruption_sequence")):
                return "supervisor_decision_invalid"
        elif writer_role == "operator_gateway":
            if not isinstance(payload.get("action_id"), str):
                return "operator_decision_invalid"
            if not _positive_int(payload.get("action_resolved_sequence")):
                return "operator_decision_invalid"
    elif transition == "run_abandoned" and writer_role == "recovery":
        attempt_ids = payload.get("attempt_ids")
        if (
            not isinstance(attempt_ids, list)
            or not attempt_ids
            or any(not isinstance(value, str) or not value for value in attempt_ids)
            or len(set(attempt_ids)) != len(attempt_ids)
            or not _positive_int(payload.get("last_covered_sequence"))
            or payload.get("operator_assertion") != "no_live_worker"
        ):
            return "run_abandoned_invalid"
    return None


def validate_v2_receipt_contract(
    receipts: Sequence[Mapping[str, Any]],
    *,
    sealed: bool,
) -> str | None:
    """Validate cross-receipt lifecycle invariants after crypto verification."""
    attempts: dict[str, dict[str, Any]] = {}
    ordinals: dict[str, int] = {}
    repairs: dict[str, tuple[str, int, int]] = {}
    open_actions: dict[str, int] = {}
    resolved_actions: dict[int, str] = {}
    interruptions: set[int] = set()
    terminal_decision = False
    execution_complete_action_open = False
    awaiting_approval = False
    saw_run_planned = False
    saw_catalog = False
    catalog_roles: set[str] = set()

    for receipt in receipts:
        transition = str(receipt.get("transition", ""))
        sequence = receipt.get("sequence")
        payload = receipt.get("payload")
        writer_role = str(receipt.get("writer_role", ""))
        if not isinstance(payload, Mapping) or not _positive_int(sequence):
            return "receipt_payload_invalid"
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        sequence_number = sequence
        if terminal_decision:
            return "receipt_after_terminal_decision"
        if "evidence_basis" in receipt:
            authority_finding = transition_authority_finding(
                writer_role,
                transition,
                receipt.get("evidence_basis"),
            )
            if authority_finding is not None:
                return authority_finding
        finding = validate_receipt_payload(
            transition,
            payload,
            writer_role=writer_role,
        )
        if finding is not None:
            return finding

        if transition == "run_planned":
            saw_run_planned = True
            if saw_catalog:
                return "lane_catalog_duplicate"
            saw_catalog = True
            catalog = payload.get("lane_catalog", [])
            catalog_roles = {
                str(row["role"])
                for row in catalog
                if isinstance(row, Mapping) and isinstance(row.get("role"), str)
            }
        if transition == "repair_routed":
            attempt_id = str(payload["attempt_id"])
            if attempt_id in repairs:
                return "repair_route_attempt_duplicate"
            repairs[attempt_id] = (
                str(payload["target_role"]),
                int(payload["attempt_ordinal"]),
                int(payload["cycle"]),
            )
        if transition in ATTEMPT_TRANSITIONS:
            attempt_id = str(payload["attempt_id"])
            role = str(payload["role"])
            ordinal = int(payload["attempt_ordinal"])
            cycle = int(payload["repair_cycle"])
            if transition == "stage_attempt_created":
                if attempt_id in attempts:
                    return "attempt_id_duplicate"
                if catalog_roles and role not in catalog_roles:
                    return "attempt_lane_not_cataloged"
                if any(
                    attempt["role"] == role and attempt["terminal"] is None
                    for attempt in attempts.values()
                ):
                    return "attempt_lane_already_open"
                expected = ordinals.get(role, 0) + 1
                if ordinal != expected:
                    return "attempt_ordinal_discontinuity"
                ordinals[role] = ordinal
                attempts[attempt_id] = {
                    "role": role,
                    "ordinal": ordinal,
                    "cycle": cycle,
                    "dispatched": False,
                    "terminal": None,
                }
                routed = repairs.get(attempt_id)
                if role in CONDITIONAL_LANES and routed != (role, ordinal, cycle):
                    return "repair_route_attempt_mismatch"
                continue
            attempt = attempts.get(attempt_id)
            if attempt is None:
                return "attempt_created_missing"
            if (
                attempt["role"] != role
                or attempt["ordinal"] != ordinal
                or attempt["cycle"] != cycle
            ):
                return "attempt_identity_mismatch"
            if attempt["terminal"] is not None:
                return "attempt_transition_after_terminal"
            if transition == "stage_dispatch_started":
                if attempt["dispatched"]:
                    return "attempt_dispatch_duplicate"
                attempt["dispatched"] = True
            elif transition in TERMINAL_ATTEMPT_TRANSITIONS:
                if transition == "stage_blocked" and attempt["dispatched"]:
                    return "attempt_blocked_after_dispatch"
                if transition == "stage_completed" and not attempt["dispatched"]:
                    return "attempt_completed_without_dispatch"
                if payload["provider_dispatch"] is True and not attempt["dispatched"]:
                    return "attempt_dispatch_evidence_missing"
                attempt["terminal"] = transition
                if transition == "stage_interrupted":
                    interruptions.add(sequence_number)
        elif transition == "action_opened":
            action_id = str(payload["action_id"])
            if action_id in open_actions or action_id in resolved_actions.values():
                return "action_id_duplicate"
            if int(payload["caused_by_sequence"]) >= sequence_number:
                return "action_cause_invalid"
            open_actions[action_id] = sequence_number
        elif transition == "action_resolved":
            action_id = str(payload["action_id"])
            opened_sequence = open_actions.get(action_id)
            if opened_sequence is None:
                return "action_open_missing"
            if payload["opened_sequence"] != opened_sequence:
                return "action_open_link_invalid"
            del open_actions[action_id]
            resolved_actions[sequence_number] = action_id
        elif transition == "run_decision" and writer_role == "supervisor":
            if payload["interruption_sequence"] not in interruptions:
                return "supervisor_decision_link_invalid"
            terminal_decision = True
        elif transition == "run_decision" and writer_role == "operator_gateway":
            resolved_sequence = int(payload["action_resolved_sequence"])
            if resolved_actions.get(resolved_sequence) != payload["action_id"]:
                return "operator_decision_link_invalid"
            if open_actions or not execution_complete_action_open:
                return "operator_decision_actions_open"
            terminal_decision = True
        elif transition == "run_decision":
            status = payload.get("status")
            open_attempts = [
                attempt
                for attempt in attempts.values()
                if attempt["terminal"] is None
            ]
            terminal_attempts = {
                str(attempt["terminal"])
                for attempt in attempts.values()
                if attempt["terminal"] is not None
            }
            if status == "execution_complete_action_open":
                if not open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                execution_complete_action_open = True
            elif status == "workflow_closed":
                if open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
            elif status in {"blocked", "workflow_failed"}:
                if not terminal_attempts.intersection(
                    {"stage_blocked", "stage_failed", "stage_interrupted"}
                ):
                    return "run_decision_precondition_invalid"
            awaiting_approval = status == "awaiting_approval"
            terminal_decision = payload.get("status") not in {
                "awaiting_approval",
                "execution_complete_action_open",
            }
        elif transition == "run_abandoned":
            open_attempt_ids = {
                candidate_id
                for candidate_id, attempt in attempts.items()
                if attempt["terminal"] is None
            }
            if terminal_decision or set(payload["attempt_ids"]) != open_attempt_ids:
                return "run_abandoned_attempts_invalid"
            # A run waiting on a person is not abandonable: `awaiting_approval`
            # and an open action both leave `terminal_decision` false, so the
            # check above alone would let recovery close a run mid-decision.
            if awaiting_approval:
                return "run_abandoned_awaiting_approval"
            if open_actions:
                return "run_abandoned_actions_open"
            if int(payload["last_covered_sequence"]) != sequence_number - 1:
                return "run_abandoned_coverage_invalid"
            for open_attempt_id in open_attempt_ids:
                attempts[open_attempt_id]["terminal"] = "run_abandoned"
            terminal_decision = True

    if saw_run_planned and not saw_catalog:
        return "lane_catalog_missing"
    if sealed and any(attempt["terminal"] is None for attempt in attempts.values()):
        return "attempt_terminal_missing"
    return None


__all__ = [
    "ATTEMPT_TRANSITIONS",
    "CONDITIONAL_LANES",
    "CORE_LANES",
    "LANE_ORDER",
    "TERMINAL_ATTEMPT_TRANSITIONS",
    "validate_receipt_payload",
    "validate_v2_receipt_contract",
]
