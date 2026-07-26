"""Generate the complete authority-matrix conformance corpus."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    transition_authority_finding,
)


_WRITERS = ("orchestrator", "supervisor", "operator_gateway", "recovery")
_BASES = ("observed", "submitted", "derived")


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
                "expected_finding": None,
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


__all__ = ["corpus_digest", "generate_authority_corpus"]
