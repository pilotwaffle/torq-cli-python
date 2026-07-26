"""Machine-readable authorization matrix for schema-v2 run evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class TransitionRule:
    writer_role: str
    transition: str
    evidence_basis: str
    precondition: str
    decision_value: str | None = None
    terminal: bool = False


TRANSITION_RULES: tuple[TransitionRule, ...] = (
    TransitionRule("orchestrator", "run_planned", "observed", "run_not_planned"),
    TransitionRule(
        "orchestrator", "stage_attempt_created", "observed", "lane_available"
    ),
    TransitionRule(
        "orchestrator", "stage_dispatch_started", "observed", "attempt_open"
    ),
    TransitionRule(
        "orchestrator", "stage_blocked", "observed", "attempt_open_undispatched",
        terminal=True,
    ),
    TransitionRule(
        "orchestrator", "stage_rejected", "observed",
        "attempt_open_dispatched_review_verdict_reject",
        terminal=True,
    ),
    TransitionRule(
        "orchestrator", "stage_completed", "observed", "attempt_open_dispatched",
        terminal=True,
    ),
    TransitionRule(
        "orchestrator", "stage_failed", "observed", "attempt_open", terminal=True
    ),
    TransitionRule(
        "orchestrator", "repair_routed", "derived", "qualifying_defect"
    ),
    TransitionRule("orchestrator", "action_opened", "derived", "action_new"),
    TransitionRule(
        "orchestrator", "terminalization_started", "derived", "commands_pending"
    ),
    TransitionRule(
        "orchestrator", "run_decision", "derived", "required_lanes_completed",
        "completed", True,
    ),
    TransitionRule(
        "orchestrator", "run_decision", "derived", "required_lane_blocked",
        "blocked", True,
    ),
    TransitionRule(
        "orchestrator", "run_decision", "derived", "required_lane_failed",
        "failed", True,
    ),
    TransitionRule(
        "orchestrator", "run_decision", "derived", "action_open",
        "awaiting_approval", False,
    ),
    TransitionRule(
        "supervisor", "stage_interrupted", "observed", "attempt_open", None, True
    ),
    TransitionRule(
        "supervisor",
        "run_decision",
        "derived",
        "interruption_linked", "failed", True,
    ),
    TransitionRule(
        "operator_gateway", "command_accepted", "submitted", "command_new"
    ),
    TransitionRule(
        "operator_gateway", "command_rejected", "submitted", "command_new"
    ),
    TransitionRule(
        "operator_gateway", "context_injected", "submitted", "run_open"
    ),
    TransitionRule(
        "orchestrator", "context_injected", "derived", "eligible_attempt_open"
    ),
    TransitionRule(
        "orchestrator", "command_unapplied", "derived", "run_terminating"
    ),
    TransitionRule(
        "orchestrator", "run_replanned", "derived", "accepted_lead_command_eligible"
    ),
    TransitionRule(
        "operator_gateway", "action_resolved", "submitted", "action_open"
    ),
    TransitionRule(
        "operator_gateway",
        "run_decision",
        "derived",
        "last_action_resolved", "completed", True,
    ),
    TransitionRule(
        "operator_gateway", "run_decision", "derived", "last_action_resolved",
        "blocked", True,
    ),
    TransitionRule(
        "operator_gateway", "run_decision", "derived", "last_action_resolved",
        "failed", True,
    ),
    TransitionRule(
        "recovery", "run_abandoned", "submitted", "open_attempts_enumerated",
        terminal=True,
    ),
)

_RULE_INDEX = {
    (rule.writer_role, rule.transition, rule.decision_value): rule
    for rule in TRANSITION_RULES
}
if len(_RULE_INDEX) != len(TRANSITION_RULES):
    raise RuntimeError("transition_rule_duplicate")
if any(
    (rule.transition == "run_decision") != (rule.decision_value is not None)
    for rule in TRANSITION_RULES
):
    raise RuntimeError("transition_rule_decision_discriminator_invalid")
_GOVERNED_TRANSITIONS = frozenset(rule.transition for rule in TRANSITION_RULES)

# Current-schema audit events are closed just like lifecycle transitions.  They
# do not participate in the lifecycle state machine, but naming them here keeps
# an orchestrator capability from becoming an unbounded, signed prose channel.
# Legacy verification retains the historical open-event behavior below.
CURRENT_AUDIT_TRANSITIONS = frozenset(
    {
        "approval_apply",
        "audit",
        "build",
        "design",
        "done",
        "portable_export_requested",
        "reaudit",
        "repair",
        "run_attested",
        "stage_started",
        "usage",
    }
)


def transition_rule(
    writer_role: str,
    transition: str,
    decision_value: object = None,
) -> TransitionRule | None:
    """Return the one authority rule for a governed transition."""
    normalized = decision_value if isinstance(decision_value, str) else None
    return _RULE_INDEX.get((writer_role, transition, normalized))


def transition_authority_finding(
    writer_role: object,
    transition: object,
    evidence_basis: object,
    payload: Mapping[str, object],
    *,
    legacy: bool = False,
) -> str | None:
    """Validate role and basis using the shared machine-readable matrix."""
    if not isinstance(writer_role, str) or not isinstance(transition, str):
        return "receipt_writer_unauthorized"
    if legacy:
        return _legacy_authority_finding(
            writer_role,
            transition,
            evidence_basis,
            payload,
        )
    decision_value = payload.get("decision") if transition == "run_decision" else None
    rule = transition_rule(writer_role, transition, decision_value)
    if rule is None:
        if transition == "run_decision" and any(
            candidate.writer_role == writer_role
            and candidate.transition == transition
            for candidate in TRANSITION_RULES
        ):
            return "run_decision_value_unauthorized"
        # Current evidence is closed: even non-lifecycle audit events must be
        # declared.  Legacy evidence keeps its historical open-event rule in
        # ``_legacy_authority_finding``.
        if (
            transition in CURRENT_AUDIT_TRANSITIONS
            and writer_role == "orchestrator"
            and evidence_basis == "observed"
        ):
            return None
        return "receipt_writer_unauthorized"
    if evidence_basis != rule.evidence_basis:
        return "receipt_writer_unauthorized"
    return None


_LEGACY_BASES = {
    ("orchestrator", "run_planned"): "derived",
    ("orchestrator", "stage_attempt_created"): "observed",
    ("orchestrator", "stage_dispatch_started"): "observed",
    ("orchestrator", "stage_blocked"): "observed",
    ("orchestrator", "stage_completed"): "observed",
    ("orchestrator", "stage_failed"): "observed",
    ("orchestrator", "repair_routed"): "derived",
    ("orchestrator", "action_opened"): "derived",
    ("supervisor", "stage_interrupted"): "derived",
    ("operator_gateway", "command_accepted"): "submitted",
    ("operator_gateway", "command_rejected"): "submitted",
    ("operator_gateway", "context_injected"): "submitted",
    ("orchestrator", "context_injected"): "derived",
    ("orchestrator", "command_unapplied"): "derived",
    ("orchestrator", "run_replanned"): "derived",
    ("operator_gateway", "action_resolved"): "submitted",
    ("recovery", "run_abandoned"): "submitted",
}
_LEGACY_STATUSES = {
    "orchestrator": frozenset({
        "awaiting_approval", "blocked", "execution_complete_action_open",
        "terminating", "workflow_closed",
    }),
    "supervisor": frozenset({"workflow_failed"}),
    "operator_gateway": frozenset({"workflow_closed"}),
}


def _legacy_authority_finding(
    writer_role: str,
    transition: str,
    evidence_basis: object,
    payload: Mapping[str, object],
) -> str | None:
    if transition == "run_decision":
        if (
            evidence_basis != "derived"
            or payload.get("status") not in _LEGACY_STATUSES.get(writer_role, ())
        ):
            return "receipt_writer_unauthorized"
        return None
    expected = _LEGACY_BASES.get((writer_role, transition))
    if expected is None:
        if (
            transition not in _GOVERNED_TRANSITIONS
            and writer_role == "orchestrator"
            and evidence_basis != "submitted"
        ):
            return None
        return "receipt_writer_unauthorized"
    return None if evidence_basis == expected else "receipt_writer_unauthorized"


__all__ = [
    "TRANSITION_RULES",
    "CURRENT_AUDIT_TRANSITIONS",
    "TransitionRule",
    "transition_authority_finding",
    "transition_rule",
]
