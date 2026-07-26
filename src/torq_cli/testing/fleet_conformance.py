"""Generate the complete authority-matrix conformance corpus."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from torq_cli.application.fleet import reduce_fleet_snapshot
from torq_cli.application.fleet_controls import compose_fleet_envelope
from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    transition_authority_finding,
)
from torq_cli.testing.fleet_preconditions import (
    negative_precondition_case,
    positive_precondition_case,
)


_WRITERS = ("orchestrator", "supervisor", "operator_gateway", "recovery")
_BASES = ("observed", "submitted", "derived")

UI_AXIS_VALUES: dict[str, tuple[str, ...]] = {
    "verification_state": (
        "tampered",
        "unreadable",
        "incomplete",
        "live_catching_up",
        "sealed_verified",
        "live_verified",
    ),
    "execution_state": ("running", "complete", "blocked", "abandoned"),
    "workflow_state": (
        "open",
        "action_open",
        "closed",
        "abandoned",
        "legacy_execution_complete_action_open",
    ),
    "lane_state": (
        "dormant",
        "queued",
        "running",
        "sealed",
        "blocked",
        "needs_you",
        "failed",
        "interrupted",
        "abandoned",
    ),
    "annotation_kind": (
        "none",
        "broker_unavailable",
        "workflow_reconciled",
        "orphaned",
        "recovery_required",
    ),
    "session_capability": ("write_capable", "read_only", "expired"),
    "schema_version": ("1.0.0", "1.1.0", "2.0.0", "unsupported"),
}

INVALID_MUTATOR_STAGES = (
    "truncate_chain",
    "resign_foreign_key",
    "restore_manifest_generation",
    "withhold_manifest_replacement",
    "replace_schema_unsupported",
)

_OBSERVED_AT = "2026-07-25T12:00:00Z"


def _payload(transition: str, decision: str | None) -> dict[str, Any]:
    return {"decision": decision} if transition == "run_decision" else {}


def generate_authority_corpus() -> dict[str, Any]:
    """Generate one positive plus every applicable first-order rule mutation."""
    fixtures: list[dict[str, Any]] = []
    ordered = sorted(
        TRANSITION_RULES,
        key=lambda rule: (
            rule.writer_role,
            rule.transition,
            rule.decision_value or "",
        ),
    )
    for rule in ordered:
        rule_id = ":".join(
            (rule.writer_role, rule.transition, rule.decision_value or "-")
        )
        payload = _payload(rule.transition, rule.decision_value)
        positive_chain = positive_precondition_case(rule)
        fixtures.append(
            {
                "fixture_id": f"allow:{rule_id}",
                "rule_id": rule_id,
                "mutation_stage": None,
                "precondition": rule.precondition,
                "writer_role": rule.writer_role,
                "transition": rule.transition,
                "evidence_basis": rule.evidence_basis,
                "payload": payload,
                "receipt_chain": positive_chain,
                "expected_finding": None,
            }
        )
        for writer in _WRITERS:
            if writer == rule.writer_role:
                continue
            finding = transition_authority_finding(
                writer,
                rule.transition,
                rule.evidence_basis,
                payload,
            )
            if finding is not None:
                fixtures.append(
                    {
                        "fixture_id": f"deny:writer_role:{rule_id}:{writer}",
                        "rule_id": rule_id,
                        "mutation_stage": "writer_role",
                        "precondition": rule.precondition,
                        "writer_role": writer,
                        "transition": rule.transition,
                        "evidence_basis": rule.evidence_basis,
                        "payload": payload,
                        "expected_finding": finding,
                    }
                )
                break
        wrong_basis = next(
            basis for basis in _BASES if basis != rule.evidence_basis
        )
        receipt_chain, expected_finding = negative_precondition_case(rule)
        fixtures.append(
            {
                "fixture_id": f"deny:evidence_basis:{rule_id}:{wrong_basis}",
                "rule_id": rule_id,
                "mutation_stage": "evidence_basis",
                "precondition": rule.precondition,
                "writer_role": rule.writer_role,
                "transition": rule.transition,
                "evidence_basis": wrong_basis,
                "payload": payload,
                "expected_finding": "receipt_writer_unauthorized",
            }
        )
        if rule.transition == "run_decision":
            fixtures.append(
                {
                    "fixture_id": f"deny:decision_value:{rule_id}",
                    "rule_id": rule_id,
                    "mutation_stage": "decision_value",
                    "precondition": rule.precondition,
                    "writer_role": rule.writer_role,
                    "transition": rule.transition,
                    "evidence_basis": rule.evidence_basis,
                    "payload": {"decision": "invented"},
                    "expected_finding": "run_decision_value_unauthorized",
                }
            )
        fixtures.append(
            {
                "fixture_id": f"deny:precondition:{rule_id}",
                "rule_id": rule_id,
                "mutation_stage": "precondition",
                "precondition": rule.precondition,
                "writer_role": rule.writer_role,
                "transition": rule.transition,
                "evidence_basis": rule.evidence_basis,
                "payload": payload,
                "receipt_chain": receipt_chain,
                "expected_finding": expected_finding,
            }
        )
    return {
        "schema": "torq-fleet-conformance-v1",
        "completeness": {
            "source": "TRANSITION_RULES",
            "rule_count": len(ordered),
            "positive_per_rule": 1,
            "mutation_stages": [
                "writer_role",
                "evidence_basis",
                "decision_value_if_applicable",
                "precondition",
            ],
        },
        "fixtures": fixtures,
    }


def _ui_receipt(
    sequence: int,
    transition: str,
    payload: Mapping[str, Any],
    *,
    writer_role: str = "orchestrator",
    evidence_basis: str = "observed",
    schema_version: str = "2.0.0",
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "sequence": sequence,
        "observed_at": _OBSERVED_AT,
        "transition": transition,
        "writer_role": writer_role,
        "evidence_basis": evidence_basis,
        "writer_key_id": "sha256:" + "1" * 64,
        "payload": dict(payload),
    }


def _ui_chain_for_lane_state(state: str) -> list[dict[str, Any]]:
    conditional = state == "dormant"
    receipts = [
        _ui_receipt(
            1,
            "run_planned",
            {
                "mode": "live",
                "profile_id": "conformance",
                "strategy_id": "conformance",
                "planned_roles": ["g1d"],
                "lane_catalog": [
                    {
                        "role": "g1d",
                        "order": 0,
                        "kind": "conditional" if conditional else "core",
                        "required": not conditional,
                        "provider": "test",
                        "model": "test-model",
                    }
                ],
            },
        )
    ]
    if state in {"dormant", "queued"}:
        return receipts
    attempt = {
        "role": "g1d",
        "attempt_id": "attempt-g1d-1",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
        "provider_dispatch": False,
    }
    receipts.append(_ui_receipt(2, "stage_attempt_created", attempt))
    if state == "running":
        return receipts
    if state == "needs_you":
        receipts.append(
            _ui_receipt(
                3,
                "action_opened",
                {
                    "action_id": "action-g1d",
                    "type": "approval_required",
                    "scope": "lane",
                    "target": "g1d",
                    "summary": "Approve lane",
                    "allowed_resolutions": ["approved", "rejected"],
                    "outcome_map": {
                        "approved": "completed",
                        "rejected": "blocked",
                    },
                    "caused_by_sequence": 2,
                },
                evidence_basis="derived",
            )
        )
        return receipts
    if state == "abandoned":
        receipts.append(
            _ui_receipt(
                3,
                "run_abandoned",
                {
                    "attempt_ids": ["attempt-g1d-1"],
                    "last_covered_sequence": 2,
                    "operator_assertion": "no_live_worker",
                },
                writer_role="recovery",
                evidence_basis="submitted",
            )
        )
        return receipts
    transition = {
        "sealed": "stage_completed",
        "blocked": "stage_blocked",
        "failed": "stage_failed",
        "interrupted": "stage_interrupted",
    }[state]
    if transition in {"stage_completed", "stage_failed"}:
        receipts.append(
            _ui_receipt(
                3,
                "stage_dispatch_started",
                {**attempt, "provider_dispatch": True},
            )
        )
    writer = "supervisor" if transition == "stage_interrupted" else "orchestrator"
    basis = "observed"
    payload = {
        **attempt,
        "provider_dispatch": (
            "unknown"
            if transition == "stage_interrupted"
            else transition in {"stage_completed", "stage_failed"}
        ),
    }
    if transition == "stage_interrupted":
        payload["observation_source"] = "worker_exit"
    receipts.append(
        _ui_receipt(
            len(receipts) + 1,
            transition,
            payload,
            writer_role=writer,
            evidence_basis=basis,
        )
    )
    return receipts


def _unavailable_snapshot(state: str, finding: str) -> dict[str, Any]:
    return {
        "schema": "torq-fleet-snapshot-v3",
        "verification": {
            "state": state,
            "finding": finding,
            "covered_sequence": None,
            "store_sequence": None,
            "manifest_generation": None,
        },
        "data_status": "unavailable",
        "run": None,
        "summary": None,
        "lanes": [],
        "actions": [],
        "settlement": None,
        "accounting": None,
    }


def _scenario_specs() -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    base = {
        "verification_state": "live_verified",
        "execution_state": "running",
        "workflow_state": "open",
        "lane_state": "queued",
        "annotation_kind": "none",
        "session_capability": "write_capable",
        "schema_version": "2.0.0",
        "mutator_stage": "none",
    }

    def add(fixture_id: str, **overrides: str) -> None:
        specs.append({"fixture_id": fixture_id, **base, **overrides})

    for value in UI_AXIS_VALUES["lane_state"]:
        add(f"lane:{value}", lane_state=value)
    verification_mutators = {
        "tampered": "resign_foreign_key",
        "unreadable": "replace_schema_unsupported",
        "incomplete": "truncate_chain",
        "live_catching_up": "withhold_manifest_replacement",
        "sealed_verified": "none",
        "live_verified": "none",
    }
    for value, mutator in verification_mutators.items():
        add(
            f"verification:{value}",
            verification_state=value,
            mutator_stage=mutator,
        )
    add(
        "verification:manifest_rollback",
        verification_state="tampered",
        mutator_stage="restore_manifest_generation",
    )
    workflow_pairs = {
        "open": "running",
        "action_open": "complete",
        "closed": "complete",
        "abandoned": "abandoned",
        "legacy_execution_complete_action_open": "complete",
    }
    for workflow, execution in workflow_pairs.items():
        add(
            f"workflow:{workflow}",
            workflow_state=workflow,
            execution_state=execution,
        )
    add("execution:blocked", execution_state="blocked", workflow_state="closed")
    for value in UI_AXIS_VALUES["annotation_kind"]:
        add(f"annotation:{value}", annotation_kind=value)
    for value in UI_AXIS_VALUES["session_capability"]:
        add(f"session:{value}", session_capability=value)
    for value in UI_AXIS_VALUES["schema_version"]:
        add(
            f"schema:{value}",
            schema_version=value,
            verification_state="unreadable" if value == "unsupported" else "live_verified",
            mutator_stage=(
                "replace_schema_unsupported" if value == "unsupported" else "none"
            ),
        )
    return specs


def generate_ui_corpus() -> dict[str, Any]:
    """Generate paired chain/envelope fixtures over every declared UI axis."""
    chain_fixtures: list[dict[str, Any]] = []
    snapshot_fixtures: list[dict[str, Any]] = []
    observed: dict[str, set[str]] = {name: set() for name in UI_AXIS_VALUES}
    observed_mutators: set[str] = set()
    for spec in _scenario_specs():
        receipts = _ui_chain_for_lane_state(spec["lane_state"])
        schema = spec["schema_version"]
        if schema in {"1.0.0", "1.1.0"}:
            for receipt in receipts:
                receipt["schema_version"] = schema
        manifest = {
            "run_id": "run-conformance",
            "receipt_count": len(receipts),
            "manifest_generation": 1,
            "sealed": spec["verification_state"] == "sealed_verified",
            "terminal_receipt_hash": "sha256:" + "2" * 64,
        }
        verification = spec["verification_state"]
        if verification in {"tampered", "unreadable", "incomplete"}:
            snapshot = _unavailable_snapshot(
                verification,
                {
                    "tampered": "receipt_signature_invalid",
                    "unreadable": "receipt_schema_unsupported",
                    "incomplete": "receipt_chain_truncated",
                }[verification],
            )
        else:
            reducer_state = (
                "verified"
                if verification in {"live_verified", "sealed_verified"}
                else verification
            )
            snapshot = reduce_fleet_snapshot(
                receipts,
                manifest,
                verification_state=reducer_state,
            )
            snapshot["run"]["execution_state"] = spec["execution_state"]
            snapshot["run"]["workflow_state"] = spec["workflow_state"]
        annotation_kind = spec["annotation_kind"]
        annotations = []
        if annotation_kind != "none":
            annotations.append(
                {
                    "kind": annotation_kind,
                    "scope": "run",
                    "observed_at": _OBSERVED_AT,
                    "source": (
                        "broker"
                        if annotation_kind == "broker_unavailable"
                        else "supervisor"
                    ),
                }
            )
        capability = spec["session_capability"]
        envelope = compose_fleet_envelope(
            snapshot,
            session_write_capable=capability == "write_capable",
            expires_at=(
                "2026-07-25T11:59:59Z"
                if capability == "expired"
                else "2026-07-25T13:00:00Z"
            ),
            annotations=annotations,
        )
        axes = {name: spec[name] for name in UI_AXIS_VALUES}
        chain_fixtures.append(
            {
                "fixture_id": spec["fixture_id"],
                "axes": axes,
                "mutator_stage": spec["mutator_stage"],
                "receipts": deepcopy(receipts),
                "manifest": manifest,
            }
        )
        snapshot_fixtures.append(
            {
                "fixture_id": spec["fixture_id"],
                "axes": axes,
                "envelope": envelope,
            }
        )
        for name, value in axes.items():
            observed[name].add(value)
        if spec["mutator_stage"] != "none":
            observed_mutators.add(spec["mutator_stage"])
    missing_axes = {
        name: sorted(set(values) - observed[name])
        for name, values in UI_AXIS_VALUES.items()
        if set(values) - observed[name]
    }
    missing_mutators = sorted(set(INVALID_MUTATOR_STAGES) - observed_mutators)
    if missing_axes or missing_mutators:
        raise ValueError(
            "fleet_ui_corpus_incomplete:"
            + json.dumps(
                {"axes": missing_axes, "mutators": missing_mutators},
                sort_keys=True,
            )
        )
    return {
        "schema": "torq-fleet-ui-conformance-v1",
        "completeness": {
            "axes": {name: list(values) for name, values in UI_AXIS_VALUES.items()},
            "reachable_tuple_rule": "every_declared_axis_value_covered",
            "invalid_mutator_stages": list(INVALID_MUTATOR_STAGES),
            "pairing_key": "fixture_id",
        },
        "chain_fixtures": chain_fixtures,
        "snapshot_fixtures": snapshot_fixtures,
    }


def corpus_digest(corpus: dict[str, Any] | None = None) -> str:
    """Return a reproducibility digest for the canonical generated corpus."""
    encoded = json.dumps(
        corpus or generate_authority_corpus(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "INVALID_MUTATOR_STAGES",
    "UI_AXIS_VALUES",
    "corpus_digest",
    "generate_authority_corpus",
    "generate_ui_corpus",
]
