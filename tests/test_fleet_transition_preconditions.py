from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    TransitionRule,
    transition_rule,
)
from torq_cli.domain.run_evidence import LANE_ORDER, validate_v2_receipt_contract
from torq_cli.domain.run_plan import PLAN_CONTRACT, plan_hash, revision_body


_HASH = "sha256:" + "0" * 64


def _row(
    sequence: int,
    transition: str,
    payload: Mapping[str, Any],
    *,
    writer_role: str = "orchestrator",
    decision: str | None = None,
) -> dict[str, Any]:
    rule = transition_rule(writer_role, transition, decision)
    if rule is None:
        assert writer_role == "orchestrator" and transition == "run_attested"
        evidence_basis = "observed"
    else:
        evidence_basis = rule.evidence_basis
    return {
        "sequence": sequence,
        "transition": transition,
        "writer_role": writer_role,
        "evidence_basis": evidence_basis,
        "payload": dict(payload),
    }


def _planned(sequence: int = 1) -> dict[str, Any]:
    return _row(
        sequence,
        "run_planned",
        {
            "mode": "live",
            "profile_id": "test",
            "strategy_id": "test",
            "planned_roles": list(LANE_ORDER),
            "lane_catalog": [
                {
                    "role": role,
                    "required": role in {"g1d", "g1r", "builder", "g2a"},
                }
                for role in LANE_ORDER
            ],
        },
    )


def _attempt(role: str = "g1d", ordinal: int = 1) -> dict[str, Any]:
    return {
        "role": role,
        "attempt_id": f"attempt-{role}-{ordinal}",
        "attempt_ordinal": ordinal,
        "repair_cycle": 0,
    }


def _action(*, caused_by_sequence: int = 1) -> dict[str, Any]:
    return {
        "action_id": "action-1",
        "type": "approval_required",
        "scope": "run",
        "target": "operator",
        "summary": "Approve the run",
        "allowed_resolutions": ["approved", "rejected"],
        "outcome_map": {"approved": "completed", "rejected": "blocked"},
        "caused_by_sequence": caused_by_sequence,
    }


def _command(command_id: str = "command-1") -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_type": "context",
        "context_id": "context-1",
        "target_role": "lead",
        "route": "lead_replan",
        "artifact": "artifacts/context-1.enc",
        "artifact_hash": _HASH,
        "media_type": "text/plain",
        "content_bytes": 1,
        "redactions": [],
        "direct_route_confirmed": True,
        "earliest_eligible_attempt": {
            "kind": "attempt_created_after_acknowledgement"
        },
        "provider_dispatch": False,
    }


def _rejected_command(command_id: str = "command-1") -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_type": "context",
        "target_role": "lead",
        "media_type": "text/plain",
        "content_bytes": 0,
        "finding": "context_empty",
        "earliest_eligible_attempt": None,
        "provider_dispatch": False,
    }


def _injected() -> dict[str, Any]:
    payload = _command()
    payload.pop("earliest_eligible_attempt")
    return {
        **payload,
        "accepted_sequence": 1,
        "effective_attempt": {
            "role": "g1d",
            "attempt_id": "attempt-g1d-1",
            "attempt_ordinal": 1,
            "attempt_created_sequence": 2,
        },
    }


def _replanned() -> dict[str, Any]:
    affected = [{
        "role": "g1d",
        "attempt_id": "attempt-g1d-1",
        "attempt_ordinal": 1,
        "attempt_created_sequence": 2,
    }]
    new_hash = plan_hash(
        revision_body(
            plan_revision=1,
            previous_plan_hash=_HASH,
            command_id="command-1",
            accepted_sequence=1,
            affected_future_attempts=affected,
        )
    )
    return {
        "command_id": "command-1",
        "plan_contract": PLAN_CONTRACT,
        "plan_revision": 1,
        "previous_replan_sequence": None,
        "accepted_sequence": 1,
        "reason": "operator_context",
        "old_plan_hash": _HASH,
        "new_plan_hash": new_hash,
        "affected_future_attempts": affected,
        "provider_dispatch": False,
    }


def _negative_case(rule: TransitionRule) -> tuple[list[dict[str, Any]], str]:
    precondition = rule.precondition
    attempt = _attempt()
    if precondition == "run_not_planned":
        return [_planned(1), _planned(2)], "lane_catalog_duplicate"
    if precondition == "lane_available":
        return [
            _planned(),
            _row(2, "stage_attempt_created", {**attempt, "provider_dispatch": False}),
            _row(
                3,
                "stage_attempt_created",
                {**_attempt(ordinal=2), "provider_dispatch": False},
            ),
        ], "attempt_lane_already_open"
    if precondition == "attempt_open":
        payload = {
            **attempt,
            "provider_dispatch": rule.transition
            in {"stage_dispatch_started", "stage_completed", "stage_rejected"},
        }
        if rule.transition == "stage_rejected":
            payload.update(
                role="g1r",
                attempt_id="attempt-g1r-1",
                verdict="reject",
                reason="design_rejected",
            )
        if rule.transition == "stage_interrupted":
            payload.update(
                provider_dispatch="unknown", observation_source="worker_exit"
            )
        return [
            _row(
                1,
                rule.transition,
                payload,
                writer_role=rule.writer_role,
            )
        ], "attempt_created_missing"
    if precondition == "attempt_open_undispatched":
        return [
            _row(1, "stage_attempt_created", {**attempt, "provider_dispatch": False}),
            _row(2, "stage_dispatch_started", {**attempt, "provider_dispatch": True}),
            _row(3, "stage_blocked", {**attempt, "provider_dispatch": False}),
        ], "attempt_blocked_after_dispatch"
    if precondition in {
        "attempt_open_dispatched",
        "attempt_open_dispatched_review_verdict_reject",
    }:
        payload = {**attempt, "provider_dispatch": True}
        if rule.transition == "stage_rejected":
            payload.update(
                role="g1r",
                attempt_id="attempt-g1r-1",
                verdict="reject",
                reason="design_rejected",
            )
            prefix = _row(
                1,
                "stage_attempt_created",
                {**payload, "provider_dispatch": False},
            )
        else:
            prefix = _row(
                1, "stage_attempt_created", {**attempt, "provider_dispatch": False}
            )
        return [prefix, _row(2, rule.transition, payload)], (
            "attempt_rejected_without_dispatch"
            if rule.transition == "stage_rejected"
            else "attempt_completed_without_dispatch"
        )
    if precondition == "qualifying_defect":
        return [
            _row(
                1,
                "repair_routed",
                {
                    "target_role": "refine_bug",
                    "cycle": 1,
                    "attempt_id": "attempt-refine_bug-1",
                    "attempt_ordinal": 1,
                    "targeted_reaudit": True,
                },
            )
        ], "repair_route_qualifying_defect_missing"
    if precondition == "action_new":
        return [
            _row(1, "run_attested", {"mode": "live"}),
            _row(2, "action_opened", _action()),
            _row(3, "action_opened", _action()),
        ], "action_id_duplicate"
    if precondition == "commands_pending":
        return [
            _row(
                1,
                "terminalization_started",
                {"reason": "pending_commands", "provider_dispatch": False},
            )
        ], "terminalization_started_precondition_invalid"
    if precondition.startswith("required_lane") or precondition == "required_lanes_completed":
        decision = rule.decision_value
        assert decision is not None
        expected = (
            "run_decision_required_lanes_incomplete"
            if decision == "completed"
            else "run_decision_precondition_invalid"
        )
        return [
            _planned(),
            _row(
                2,
                "run_decision",
                {"decision": decision},
                writer_role=rule.writer_role,
                decision=decision,
            ),
        ], expected
    if precondition == "action_open":
        if rule.transition == "action_resolved":
            return [
                _row(
                    1,
                    "action_resolved",
                    {
                        "action_id": "action-1",
                        "resolution": "approved",
                        "resolver_identity": "operator:local-session",
                        "opened_sequence": 1,
                    },
                    writer_role="operator_gateway",
                )
            ], "action_open_missing"
        return [
            _row(
                1,
                "run_decision",
                {"decision": "awaiting_approval"},
                decision="awaiting_approval",
            )
        ], "run_decision_precondition_invalid"
    if precondition == "interruption_linked":
        return [
            _row(
                1,
                "run_decision",
                {"decision": "failed", "interruption_sequence": 1},
                writer_role="supervisor",
                decision="failed",
            )
        ], "supervisor_decision_link_invalid"
    if precondition == "command_new":
        payload = _command() if rule.transition == "command_accepted" else _rejected_command()
        return [
            _row(1, rule.transition, payload, writer_role="operator_gateway"),
            _row(2, rule.transition, payload, writer_role="operator_gateway"),
        ], "command_id_duplicate"
    if precondition in {"run_open", "eligible_attempt_open"}:
        return [
            _row(
                1,
                "context_injected",
                _injected(),
                writer_role=rule.writer_role,
            )
        ], "command_accept_missing"
    if precondition == "run_terminating":
        return [
            _row(1, "command_accepted", _command(), writer_role="operator_gateway"),
            _row(
                2,
                "command_unapplied",
                {
                    "command_id": "command-1",
                    "accepted_sequence": 1,
                    "target_role": "lead",
                    "reason": "run_terminating",
                    "last_covered_sequence": 1,
                    "provider_dispatch": False,
                },
            ),
        ], "command_unapplied_precondition_invalid"
    if precondition == "accepted_lead_command_eligible":
        return [_row(1, "run_replanned", _replanned())], (
            "run_replanned_precondition_invalid"
        )
    if precondition == "last_action_resolved":
        decision = rule.decision_value
        assert decision is not None
        return [
            _row(
                1,
                "run_decision",
                {
                    "decision": decision,
                    "action_id": "action-1",
                    "action_resolved_sequence": 1,
                },
                writer_role="operator_gateway",
                decision=decision,
            )
        ], "operator_decision_link_invalid"
    if precondition == "open_attempts_enumerated":
        return [
            _row(1, "run_attested", {"mode": "live"}),
            _row(
                2,
                "run_abandoned",
                {
                    "attempt_ids": ["missing-attempt"],
                    "last_covered_sequence": 1,
                    "operator_assertion": "no_live_worker",
                },
                writer_role="recovery",
            ),
        ], "run_abandoned_attempts_invalid"
    raise AssertionError(f"missing precondition case: {precondition}")


@pytest.mark.parametrize(
    "rule",
    TRANSITION_RULES,
    ids=lambda rule: f"{rule.writer_role}:{rule.transition}:{rule.decision_value or '-'}",
)
def test_every_transition_precondition_has_a_negative_chain_fixture(
    rule: TransitionRule,
) -> None:
    receipts, expected = _negative_case(rule)
    assert validate_v2_receipt_contract(receipts, sealed=False) == expected
