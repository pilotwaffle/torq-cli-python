"""Machine-readable authorization matrix for schema-v2 run evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransitionRule:
    writer_role: str
    transition: str
    evidence_basis: str
    precondition: str
    terminal: bool = False


TRANSITION_RULES: tuple[TransitionRule, ...] = (
    TransitionRule("orchestrator", "run_planned", "derived", "run_not_planned"),
    TransitionRule(
        "orchestrator", "stage_attempt_created", "observed", "lane_available"
    ),
    TransitionRule(
        "orchestrator", "stage_dispatch_started", "observed", "attempt_open"
    ),
    TransitionRule(
        "orchestrator", "stage_blocked", "observed", "attempt_open_undispatched", True
    ),
    TransitionRule(
        "orchestrator", "stage_completed", "observed", "attempt_open_dispatched", True
    ),
    TransitionRule(
        "orchestrator", "stage_failed", "observed", "attempt_open", True
    ),
    TransitionRule(
        "orchestrator", "repair_routed", "derived", "qualifying_defect"
    ),
    TransitionRule("orchestrator", "action_opened", "derived", "action_new"),
    TransitionRule("orchestrator", "run_decision", "derived", "decision_valid", True),
    TransitionRule(
        "supervisor", "stage_interrupted", "derived", "attempt_open", True
    ),
    TransitionRule(
        "supervisor", "run_decision", "derived", "interruption_linked", True
    ),
    TransitionRule(
        "operator_gateway", "context_injected", "submitted", "run_open"
    ),
    TransitionRule(
        "operator_gateway", "action_resolved", "submitted", "action_open"
    ),
    TransitionRule(
        "operator_gateway", "run_decision", "derived", "last_action_resolved", True
    ),
    TransitionRule(
        "recovery", "run_abandoned", "submitted", "open_attempts_enumerated", True
    ),
)

_RULE_INDEX = {
    (rule.writer_role, rule.transition): rule for rule in TRANSITION_RULES
}
_GOVERNED_TRANSITIONS = frozenset(rule.transition for rule in TRANSITION_RULES)


def transition_rule(writer_role: str, transition: str) -> TransitionRule | None:
    """Return the one authority rule for a governed transition."""
    return _RULE_INDEX.get((writer_role, transition))


def transition_authority_finding(
    writer_role: object,
    transition: object,
    evidence_basis: object,
) -> str | None:
    """Validate role and basis using the shared machine-readable matrix."""
    if not isinstance(writer_role, str) or not isinstance(transition, str):
        return "receipt_writer_unauthorized"
    rule = transition_rule(writer_role, transition)
    if rule is None:
        # Pre-v2 generic audit events remain readable while governed lifecycle
        # transitions are closed. New governed rows must be declared above.
        if (
            transition not in _GOVERNED_TRANSITIONS
            and writer_role == "orchestrator"
            and evidence_basis != "submitted"
        ):
            return None
        return "receipt_writer_unauthorized"
    if evidence_basis != rule.evidence_basis:
        return "receipt_writer_unauthorized"
    return None


__all__ = [
    "TRANSITION_RULES",
    "TransitionRule",
    "transition_authority_finding",
    "transition_rule",
]
