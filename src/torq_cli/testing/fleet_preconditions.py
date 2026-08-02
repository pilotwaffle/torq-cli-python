"""Executable negative lifecycle fixtures for every authority precondition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    TransitionRule,
    transition_rule,
)
from torq_cli.domain.run_evidence import LANE_ORDER
from torq_cli.domain.run_plan import (
    PLAN_CONTRACT,
    initial_plan_body,
    plan_hash,
    revision_body,
)

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
    evidence_basis = "observed" if rule is None else rule.evidence_basis
    return {
        "sequence": sequence,
        "transition": transition,
        "writer_role": writer_role,
        "evidence_basis": evidence_basis,
        "payload": dict(payload),
    }


def _planned(
    sequence: int = 1,
    *,
    mode: str = "live",
    required_roles: frozenset[str] = frozenset({"g1d", "g1r", "builder", "g2a"}),
    include_hash: bool = False,
) -> dict[str, Any]:
    catalog = [
        {"role": role, "required": role in required_roles}
        for role in LANE_ORDER
    ]
    payload: dict[str, Any] = {
        "mode": mode,
        "profile_id": "test",
        "strategy_id": "test",
        "planned_roles": list(LANE_ORDER),
        "lane_catalog": catalog,
    }
    if include_hash:
        payload["plan_contract"] = PLAN_CONTRACT
        payload["plan_hash"] = plan_hash(
            initial_plan_body(
                profile_id="test",
                strategy_id="test",
                planned_roles=list(LANE_ORDER),
                lane_catalog=catalog,
            )
        )
    return _row(
        sequence,
        "run_planned",
        payload,
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


def _command(
    command_id: str = "command-1",
    *,
    target_role: str = "lead",
) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_type": "context",
        "context_id": "context-1",
        "target_role": target_role,
        "route": "lead_replan" if target_role == "lead" else "direct_lane",
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


def _rejected_command() -> dict[str, Any]:
    return {
        "command_id": "command-1",
        "command_type": "context",
        "target_role": "lead",
        "media_type": "text/plain",
        "content_bytes": 0,
        "finding": "context_empty",
        "earliest_eligible_attempt": None,
        "provider_dispatch": False,
    }


def _injected(
    *,
    accepted_sequence: int = 1,
    attempt_created_sequence: int = 2,
    target_role: str = "lead",
) -> dict[str, Any]:
    payload = _command(target_role=target_role)
    payload.pop("earliest_eligible_attempt")
    return {
        **payload,
        "accepted_sequence": accepted_sequence,
        "effective_attempt": {
            "role": "g1d",
            "attempt_id": "attempt-g1d-1",
            "attempt_ordinal": 1,
            "attempt_created_sequence": attempt_created_sequence,
        },
    }


def _replanned(
    *,
    old_plan_hash: str = _HASH,
    accepted_sequence: int = 1,
    attempt_created_sequence: int = 2,
) -> dict[str, Any]:
    affected = [{
        "role": "g1d",
        "attempt_id": "attempt-g1d-1",
        "attempt_ordinal": 1,
        "attempt_created_sequence": attempt_created_sequence,
    }]
    new_hash = plan_hash(
        revision_body(
            plan_revision=1,
            previous_plan_hash=old_plan_hash,
            command_id="command-1",
            accepted_sequence=accepted_sequence,
            affected_future_attempts=affected,
        )
    )
    return {
        "command_id": "command-1",
        "plan_contract": PLAN_CONTRACT,
        "plan_revision": 1,
        "previous_replan_sequence": None,
        "accepted_sequence": accepted_sequence,
        "reason": "operator_context",
        "old_plan_hash": old_plan_hash,
        "new_plan_hash": new_hash,
        "affected_future_attempts": affected,
        "provider_dispatch": False,
    }


def _append(
    receipts: list[dict[str, Any]],
    transition: str,
    payload: Mapping[str, Any],
    *,
    writer_role: str = "orchestrator",
    decision: str | None = None,
) -> None:
    receipts.append(
        _row(
            len(receipts) + 1,
            transition,
            payload,
            writer_role=writer_role,
            decision=decision,
        )
    )


def _complete_required_lanes(
    terminal: str,
) -> list[dict[str, Any]]:
    receipts = [_planned()]
    exceptional_role = "g1d"
    for role in ("g1d", "g1r", "builder", "g2a"):
        attempt = _attempt(role)
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        if role == exceptional_role and terminal == "stage_blocked":
            _append(
                receipts,
                terminal,
                {**attempt, "provider_dispatch": False},
            )
            continue
        if role == exceptional_role and terminal == "stage_failed":
            _append(
                receipts,
                terminal,
                {**attempt, "provider_dispatch": False},
            )
            continue
        _append(
            receipts,
            "stage_dispatch_started",
            {**attempt, "provider_dispatch": True},
        )
        _append(
            receipts,
            "stage_completed",
            {**attempt, "provider_dispatch": True},
        )
    return receipts


def positive_precondition_case(rule: TransitionRule) -> list[dict[str, Any]]:
    """Return a deterministic valid lifecycle ending in the selected rule."""
    transition = rule.transition
    if transition == "run_planned":
        return [_planned()]
    if transition in {
        "stage_attempt_created",
        "stage_dispatch_started",
        "stage_blocked",
        "stage_completed",
        "stage_failed",
    }:
        receipts = [_planned()]
        attempt = _attempt()
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        if transition == "stage_attempt_created":
            return receipts
        if transition in {"stage_dispatch_started", "stage_completed"}:
            _append(
                receipts,
                "stage_dispatch_started",
                {**attempt, "provider_dispatch": True},
            )
            if transition == "stage_dispatch_started":
                return receipts
            _append(
                receipts,
                "stage_completed",
                {**attempt, "provider_dispatch": True},
            )
            return receipts
        _append(
            receipts,
            transition,
            {**attempt, "provider_dispatch": False},
        )
        return receipts
    if transition == "stage_rejected":
        receipts = [_planned()]
        attempt = _attempt("g1r")
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        _append(
            receipts,
            "stage_dispatch_started",
            {**attempt, "provider_dispatch": True},
        )
        _append(
            receipts,
            "stage_rejected",
            {
                **attempt,
                "provider_dispatch": True,
                "verdict": "reject",
                "reason": "design_rejected",
            },
        )
        return receipts
    if transition == "stage_interrupted":
        receipts = [_planned()]
        attempt = _attempt()
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        _append(
            receipts,
            "stage_interrupted",
            {
                **attempt,
                "provider_dispatch": "unknown",
                "observation_source": "worker_exit",
            },
            writer_role="supervisor",
        )
        return receipts
    if transition == "repair_routed":
        receipts = [_planned()]
        attempt = _attempt("g2a")
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        _append(
            receipts,
            "stage_dispatch_started",
            {**attempt, "provider_dispatch": True},
        )
        _append(
            receipts,
            "stage_rejected",
            {
                **attempt,
                "provider_dispatch": True,
                "verdict": "reject",
                "reason": "audit_rejected",
            },
        )
        _append(
            receipts,
            "repair_routed",
            {
                "target_role": "refine_bug",
                "cycle": 1,
                "attempt_id": "attempt-refine_bug-1",
                "attempt_ordinal": 1,
                "targeted_reaudit": True,
            },
        )
        return receipts
    if transition == "action_opened":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        _append(receipts, "action_opened", _action())
        return receipts
    if transition == "terminalization_started":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        _append(
            receipts,
            "command_accepted",
            _command(),
            writer_role="operator_gateway",
        )
        _append(
            receipts,
            "terminalization_started",
            {"reason": "pending_commands", "provider_dispatch": False},
        )
        return receipts
    if transition in {"command_accepted", "command_rejected"}:
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        payload = _command() if transition == "command_accepted" else _rejected_command()
        _append(
            receipts,
            transition,
            payload,
            writer_role="operator_gateway",
        )
        return receipts
    if transition == "context_injected":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        _append(
            receipts,
            "command_accepted",
            _command(target_role="g1d"),
            writer_role="operator_gateway",
        )
        attempt = _attempt()
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        _append(
            receipts,
            "context_injected",
            _injected(
                accepted_sequence=2,
                attempt_created_sequence=3,
                target_role="g1d",
            ),
            writer_role=rule.writer_role,
        )
        return receipts
    if transition == "command_unapplied":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        _append(
            receipts,
            "command_accepted",
            _command(),
            writer_role="operator_gateway",
        )
        _append(
            receipts,
            "terminalization_started",
            {"reason": "pending_commands", "provider_dispatch": False},
        )
        _append(
            receipts,
            "command_unapplied",
            {
                "command_id": "command-1",
                "accepted_sequence": 2,
                "target_role": "lead",
                "reason": "run_terminating",
                "last_covered_sequence": 3,
                "provider_dispatch": False,
            },
        )
        return receipts
    if transition == "run_replanned":
        plan = _planned(
            mode="dry_run",
            required_roles=frozenset(),
            include_hash=True,
        )
        receipts = [plan]
        _append(
            receipts,
            "command_accepted",
            _command(),
            writer_role="operator_gateway",
        )
        attempt = _attempt()
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        old_hash = str(plan["payload"]["plan_hash"])
        _append(
            receipts,
            "run_replanned",
            _replanned(
                old_plan_hash=old_hash,
                accepted_sequence=2,
                attempt_created_sequence=3,
            ),
        )
        return receipts
    if transition == "action_resolved":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        _append(receipts, "action_opened", _action())
        _append(
            receipts,
            "action_resolved",
            {
                "action_id": "action-1",
                "resolution": "approved",
                "resolver_identity": "operator:local-session",
                "opened_sequence": 2,
            },
            writer_role="operator_gateway",
        )
        return receipts
    if transition == "run_abandoned":
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        attempt = _attempt()
        _append(
            receipts,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        )
        _append(
            receipts,
            "run_abandoned",
            {
                "attempt_ids": [attempt["attempt_id"]],
                "last_covered_sequence": 2,
                "operator_assertion": "no_live_worker",
            },
            writer_role="recovery",
        )
        return receipts
    if transition == "run_decision" and rule.writer_role == "supervisor":
        receipts = positive_precondition_case(
            next(
                candidate
                for candidate in TRANSITION_RULES
                if candidate.writer_role == "supervisor"
                and candidate.transition == "stage_interrupted"
            )
        )
        _append(
            receipts,
            "run_decision",
            {"decision": "failed", "interruption_sequence": len(receipts)},
            writer_role="supervisor",
            decision="failed",
        )
        return receipts
    if transition == "run_decision" and rule.writer_role == "operator_gateway":
        decision = rule.decision_value
        assert decision is not None
        receipts = [_planned(mode="dry_run", required_roles=frozenset())]
        action = _action()
        action["outcome_map"] = {"approved": decision, "rejected": "blocked"}
        _append(receipts, "action_opened", action)
        _append(
            receipts,
            "run_decision",
            {
                "decision": "awaiting_approval",
                "action_id": "action-1",
                "action_opened_sequence": 2,
            },
            decision="awaiting_approval",
        )
        _append(
            receipts,
            "action_resolved",
            {
                "action_id": "action-1",
                "resolution": "approved",
                "resolver_identity": "operator:local-session",
                "opened_sequence": 2,
            },
            writer_role="operator_gateway",
        )
        _append(
            receipts,
            "run_decision",
            {
                "decision": decision,
                "action_id": "action-1",
                "action_resolved_sequence": 4,
            },
            writer_role="operator_gateway",
            decision=decision,
        )
        return receipts
    if transition == "run_decision":
        decision = rule.decision_value
        assert decision is not None
        if decision == "awaiting_approval":
            receipts = [_planned(mode="dry_run", required_roles=frozenset())]
            _append(receipts, "action_opened", _action())
            _append(
                receipts,
                "run_decision",
                {
                    "decision": decision,
                    "action_id": "action-1",
                    "action_opened_sequence": 2,
                },
                decision=decision,
            )
            return receipts
        terminal = {
            "completed": "stage_completed",
            "blocked": "stage_blocked",
            "failed": "stage_failed",
        }[decision]
        receipts = _complete_required_lanes(terminal)
        _append(
            receipts,
            "run_decision",
            {"decision": decision},
            decision=decision,
        )
        return receipts
    raise ValueError(
        f"positive_precondition_fixture_missing:{rule.writer_role}:{transition}"
    )


def negative_precondition_case(
    rule: TransitionRule,
) -> tuple[list[dict[str, Any]], str]:
    """Return a runnable receipt prefix that violates exactly the named guard."""
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
                provider_dispatch="unknown",
                observation_source="worker_exit",
            )
        return [
            _row(1, rule.transition, payload, writer_role=rule.writer_role)
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
                1,
                "stage_attempt_created",
                {**attempt, "provider_dispatch": False},
            )
        expected = (
            "attempt_rejected_without_dispatch"
            if rule.transition == "stage_rejected"
            else "attempt_completed_without_dispatch"
        )
        return [prefix, _row(2, rule.transition, payload)], expected
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
    if precondition.startswith("required_lane"):
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
        payload = (
            _command() if rule.transition == "command_accepted" else _rejected_command()
        )
        return [
            _row(1, rule.transition, payload, writer_role="operator_gateway"),
            _row(2, rule.transition, payload, writer_role="operator_gateway"),
        ], "command_id_duplicate"
    if precondition in {"run_open", "eligible_attempt_open"}:
        return [
            _row(1, "context_injected", _injected(), writer_role=rule.writer_role)
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
        return [
            _row(1, "run_replanned", _replanned())
        ], "run_replanned_precondition_invalid"
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
    raise ValueError(f"precondition_fixture_missing:{precondition}")


__all__ = ["negative_precondition_case", "positive_precondition_case"]
