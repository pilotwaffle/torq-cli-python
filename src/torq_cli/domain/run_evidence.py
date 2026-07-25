"""Schema-v2 contracts for governed run, attempt, and action evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from torq_cli.domain.evidence_transitions import transition_authority_finding
from torq_cli.domain.run_plan import (
    PLAN_CONTRACT,
    initial_plan_body,
    plan_hash,
    revision_body,
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


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


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
        contract = payload.get("plan_contract")
        digest = payload.get("plan_hash")
        if (contract is None) != (digest is None):
            return "run_plan_contract_invalid"
        if contract is not None:
            if contract != PLAN_CONTRACT or not _sha256(digest):
                return "run_plan_contract_invalid"
            body = initial_plan_body(
                profile_id=str(payload.get("profile_id", "")),
                strategy_id=str(payload.get("strategy_id", "")),
                planned_roles=[str(role) for role in payload.get("planned_roles", ())],
                lane_catalog=[dict(row) for row in catalog if isinstance(row, Mapping)],
            )
            if digest != plan_hash(body):
                return "run_plan_hash_invalid"
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
    elif transition == "command_accepted":
        required = (
            "command_id",
            "command_type",
            "context_id",
            "target_role",
            "route",
            "artifact",
            "artifact_hash",
            "media_type",
        )
        if any(
            not isinstance(payload.get(field), str) or not payload.get(field)
            for field in required
        ):
            return "command_accept_invalid"
        if payload.get("command_type") not in {"context", "artifact"}:
            return "command_accept_invalid"
        extraction = payload.get("extraction")
        if payload.get("command_type") == "artifact" and (
            not isinstance(extraction, Mapping)
            or extraction.get("contract_version") != "1.0.0"
            or not isinstance(extraction.get("extractor"), str)
            or not _positive_int(extraction.get("source_bytes"))
            or not _positive_int(extraction.get("extracted_bytes"))
        ):
            return "command_extraction_invalid"
        if payload.get("command_type") == "context" and extraction is not None:
            return "command_extraction_invalid"
        if payload.get("target_role") not in {"lead", *LANE_ORDER}:
            return "command_target_invalid"
        if payload.get("route") not in {"lead_replan", "direct_lane"}:
            return "command_accept_invalid"
        if payload.get("direct_route_confirmed") is not True:
            return "command_accept_invalid"
        boundary = payload.get("earliest_eligible_attempt")
        if (
            not isinstance(boundary, Mapping)
            or boundary.get("kind") != "attempt_created_after_acknowledgement"
        ):
            return "command_boundary_invalid"
        if not _nonnegative_int(payload.get("content_bytes")):
            return "command_accept_invalid"
        if payload.get("provider_dispatch") is not False:
            return "command_accept_invalid"
    elif transition == "command_rejected":
        if (
            not isinstance(payload.get("command_id"), str)
            or not payload.get("command_id")
            or payload.get("command_type") not in {"context", "artifact"}
            or not isinstance(payload.get("finding"), str)
            or not payload.get("finding")
            or payload.get("earliest_eligible_attempt") is not None
            or payload.get("provider_dispatch") is not False
            or not _nonnegative_int(payload.get("content_bytes"))
        ):
            return "command_rejection_invalid"
        target = payload.get("target_role")
        if target is not None and not isinstance(target, str):
            return "command_rejection_invalid"
    elif transition == "context_injected" and "command_id" in payload:
        required = (
            "command_id",
            "context_id",
            "target_role",
            "artifact",
            "artifact_hash",
            "media_type",
        )
        effective = payload.get("effective_attempt")
        if (
            any(
                not isinstance(payload.get(field), str) or not payload.get(field)
                for field in required
            )
            or not _positive_int(payload.get("accepted_sequence"))
            or not isinstance(effective, Mapping)
            or not isinstance(effective.get("role"), str)
            or not isinstance(effective.get("attempt_id"), str)
            or not _positive_int(effective.get("attempt_ordinal"))
            or not _positive_int(effective.get("attempt_created_sequence"))
            or payload.get("provider_dispatch") is not False
        ):
            return "command_effective_attempt_invalid"
    elif transition == "command_unapplied":
        if (
            not isinstance(payload.get("command_id"), str)
            or not payload.get("command_id")
            or not _positive_int(payload.get("accepted_sequence"))
            or payload.get("reason") not in {
                "no_eligible_future_attempt",
                "run_terminating",
            }
            or not _positive_int(payload.get("last_covered_sequence"))
            or payload.get("provider_dispatch") is not False
        ):
            return "command_unapplied_invalid"
    elif transition == "run_replanned":
        affected = payload.get("affected_future_attempts")
        if (
            not isinstance(payload.get("command_id"), str)
            or not payload.get("command_id")
            or payload.get("plan_contract") != PLAN_CONTRACT
            or not _positive_int(payload.get("plan_revision"))
            or payload.get("previous_replan_sequence") is not None
            and not _positive_int(payload.get("previous_replan_sequence"))
            or not _positive_int(payload.get("accepted_sequence"))
            or payload.get("reason") != "operator_context"
            or not _sha256(payload.get("old_plan_hash"))
            or not _sha256(payload.get("new_plan_hash"))
            or payload.get("old_plan_hash") == payload.get("new_plan_hash")
            or not isinstance(affected, Sequence)
            or isinstance(affected, (str, bytes))
            or len(affected) != 1
            or payload.get("provider_dispatch") is not False
        ):
            return "run_replanned_invalid"
        candidate = affected[0]
        if (
            not isinstance(candidate, Mapping)
            or candidate.get("role") not in LANE_ORDER
            or not isinstance(candidate.get("attempt_id"), str)
            or not _positive_int(candidate.get("attempt_ordinal"))
            or not _positive_int(candidate.get("attempt_created_sequence"))
        ):
            return "run_replanned_invalid"
        body = revision_body(
            plan_revision=int(payload["plan_revision"]),
            previous_plan_hash=str(payload["old_plan_hash"]),
            command_id=str(payload["command_id"]),
            accepted_sequence=int(payload["accepted_sequence"]),
            affected_future_attempts=[candidate],
        )
        if payload.get("new_plan_hash") != plan_hash(body):
            return "run_replanned_hash_invalid"
    elif transition == "action_resolved":
        required = ("action_id", "resolution", "resolver_identity")
        if any(not isinstance(payload.get(field), str) for field in required):
            return "action_resolved_invalid"
        if not _positive_int(payload.get("opened_sequence")):
            return "action_resolved_invalid"
    elif transition == "run_decision" and writer_role == "supervisor":
        if payload.get("status") != "workflow_failed":
            return "supervisor_decision_invalid"
        if not _positive_int(payload.get("interruption_sequence")):
            return "supervisor_decision_invalid"
    elif transition == "run_decision" and writer_role == "operator_gateway":
        if payload.get("status") != "workflow_closed":
            return "operator_decision_invalid"
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
    commands: dict[str, dict[str, Any]] = {}
    run_terminating = False
    resolved_actions: dict[int, str] = {}
    interruptions: set[int] = set()
    terminal_decision = False
    execution_complete_action_open = False
    waiting_on_operator = False
    saw_run_planned = False
    saw_catalog = False
    catalog_roles: set[str] = set()
    current_plan_hash: str | None = None
    plan_revision = 0
    last_replan_sequence: int | None = None
    replanned_attempts: dict[str, Mapping[str, Any]] = {}

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
        if run_terminating and transition not in {"command_unapplied", "run_decision"}:
            return "receipt_after_terminating_decision"
        if "evidence_basis" in receipt:
            authority_finding = transition_authority_finding(
                writer_role,
                transition,
                receipt.get("evidence_basis"),
                payload,
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
            plan_hash = payload.get("plan_hash")
            if plan_hash is not None:
                if not _sha256(plan_hash):
                    return "run_plan_hash_invalid"
                current_plan_hash = str(plan_hash)
        if transition == "repair_routed":
            attempt_id = str(payload["attempt_id"])
            if attempt_id in repairs:
                return "repair_route_attempt_duplicate"
            repairs[attempt_id] = (
                str(payload["target_role"]),
                int(payload["attempt_ordinal"]),
                int(payload["cycle"]),
            )
        if transition in {"command_accepted", "command_rejected"}:
            command_id = str(payload["command_id"])
            if command_id in commands:
                return "command_id_duplicate"
            commands[command_id] = {
                "transition": transition,
                "sequence": sequence_number,
                "target_role": payload.get("target_role"),
                "artifact": payload.get("artifact"),
                "artifact_hash": payload.get("artifact_hash"),
                "provenance": {
                    field: payload.get(field)
                    for field in (
                        "command_type",
                        "context_id",
                        "target_role",
                        "artifact",
                        "artifact_hash",
                        "media_type",
                        "source_name",
                        "content_bytes",
                        "redactions",
                        "extraction",
                        "direct_route_confirmed",
                    )
                },
                "finalized": None,
                "replanned": False,
            }
        if transition == "run_replanned":
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if (
                accepted is None
                or accepted["transition"] != "command_accepted"
                or accepted["target_role"] != "lead"
                or accepted["finalized"] is not None
                or accepted["replanned"]
                or current_plan_hash is None
                or payload["accepted_sequence"] != accepted["sequence"]
                or payload["plan_revision"] != plan_revision + 1
                or payload["previous_replan_sequence"] != last_replan_sequence
                or payload["old_plan_hash"] != current_plan_hash
            ):
                return "run_replanned_precondition_invalid"
            affected = payload["affected_future_attempts"]
            if not isinstance(affected, list) or len(affected) != 1:
                return "run_replanned_precondition_invalid"
            effective = affected[0]
            if not isinstance(effective, Mapping):
                return "run_replanned_precondition_invalid"
            attempt = attempts.get(str(effective.get("attempt_id")))
            if (
                attempt is None
                or attempt["role"] != effective.get("role")
                or attempt["ordinal"] != effective.get("attempt_ordinal")
                or attempt["created_sequence"]
                != effective.get("attempt_created_sequence")
                or attempt["created_sequence"] <= accepted["sequence"]
                or attempt["dispatched"]
                or attempt["terminal"] is not None
            ):
                return "run_replanned_attempt_invalid"
            accepted["replanned"] = True
            replanned_attempts[command_id] = effective
            current_plan_hash = str(payload["new_plan_hash"])
            plan_revision += 1
            last_replan_sequence = sequence_number
        if transition == "context_injected" and "command_id" in payload:
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if accepted is None or accepted["transition"] != "command_accepted":
                return "command_accept_missing"
            if accepted["finalized"] is not None:
                return "command_already_finalized"
            if payload.get("accepted_sequence") != accepted["sequence"]:
                return "command_accept_link_invalid"
            if (
                payload.get("artifact") != accepted["artifact"]
                or payload.get("artifact_hash") != accepted["artifact_hash"]
                or payload.get("target_role") != accepted["target_role"]
            ):
                return "command_accept_link_invalid"
            if any(
                payload.get(field) != value
                for field, value in accepted["provenance"].items()
            ):
                return "command_extraction_link_invalid"
            if accepted["target_role"] == "lead" and not accepted["replanned"]:
                return "command_replan_missing"
            if accepted["target_role"] == "lead" and replanned_attempts.get(
                command_id
            ) != effective:
                return "command_replan_attempt_mismatch"
            effective = payload["effective_attempt"]
            assert isinstance(effective, Mapping)
            attempt_id = str(effective["attempt_id"])
            attempt = attempts.get(attempt_id)
            if (
                attempt is None
                or attempt["role"] != effective["role"]
                or attempt["ordinal"] != effective["attempt_ordinal"]
                or attempt["created_sequence"]
                != effective["attempt_created_sequence"]
                or attempt["dispatched"]
            ):
                return "command_effective_attempt_invalid"
            if int(attempt["created_sequence"]) <= int(accepted["sequence"]):
                return "command_boundary_violated"
            if (
                accepted["target_role"] != "lead"
                and accepted["target_role"] != attempt["role"]
            ):
                return "command_effective_attempt_invalid"
            accepted["finalized"] = "context_injected"
        if transition == "command_unapplied":
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if accepted is None or accepted["transition"] != "command_accepted":
                return "command_accept_missing"
            if accepted["finalized"] is not None:
                return "command_already_finalized"
            if payload.get("accepted_sequence") != accepted["sequence"]:
                return "command_accept_link_invalid"
            if payload.get("last_covered_sequence") != sequence_number - 1:
                return "command_unapplied_precondition_invalid"
            if payload.get("reason") == "run_terminating" and not run_terminating:
                return "command_unapplied_precondition_invalid"
            accepted["finalized"] = "command_unapplied"
            accepted["unapplied_reason"] = payload.get("reason")
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
                    "created_sequence": sequence_number,
                }
                for command in commands.values():
                    if (
                        command.get("finalized") == "command_unapplied"
                        and command.get("target_role") in {"lead", role}
                    ):
                        return "command_unapplied_precondition_invalid"
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
            if status == "terminating":
                if run_terminating or open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                if not any(
                    command["transition"] == "command_accepted"
                    and command["finalized"] is None
                    for command in commands.values()
                ):
                    return "run_decision_precondition_invalid"
                run_terminating = True
            elif status == "execution_complete_action_open":
                if not open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                execution_complete_action_open = True
                waiting_on_operator = True
            elif status == "awaiting_approval":
                waiting_on_operator = True
            elif status == "workflow_closed":
                if open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
            elif status in {"blocked", "workflow_failed"}:
                if not terminal_attempts.intersection(
                    {"stage_blocked", "stage_failed", "stage_interrupted"}
                ):
                    return "run_decision_precondition_invalid"
            terminal_decision = payload.get("status") not in {
                "awaiting_approval",
                "execution_complete_action_open",
                "terminating",
            }
            if terminal_decision and any(
                command["transition"] == "command_accepted"
                and command["finalized"] is None
                for command in commands.values()
            ):
                return "command_pending_at_terminal"
            if terminal_decision:
                run_terminating = False
        elif transition == "run_abandoned":
            open_attempt_ids = {
                candidate_id
                for candidate_id, attempt in attempts.items()
                if attempt["terminal"] is None
            }
            if waiting_on_operator or open_actions:
                return "run_abandoned_operator_action_open"
            if terminal_decision or set(payload["attempt_ids"]) != open_attempt_ids:
                return "run_abandoned_attempts_invalid"
            if int(payload["last_covered_sequence"]) != sequence_number - 1:
                return "run_abandoned_coverage_invalid"
            for open_attempt_id in open_attempt_ids:
                attempts[open_attempt_id]["terminal"] = "run_abandoned"
            terminal_decision = True
            if any(
                command["transition"] == "command_accepted"
                and command["finalized"] is None
                for command in commands.values()
            ):
                return "command_pending_at_terminal"

    if saw_run_planned and not saw_catalog:
        return "lane_catalog_missing"
    if sealed and any(attempt["terminal"] is None for attempt in attempts.values()):
        return "attempt_terminal_missing"
    if sealed and run_terminating:
        return "run_terminating_incomplete"
    if sealed and any(
        command["transition"] == "command_accepted"
        and command["finalized"] is None
        for command in commands.values()
    ):
        return "command_pending_at_terminal"
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
