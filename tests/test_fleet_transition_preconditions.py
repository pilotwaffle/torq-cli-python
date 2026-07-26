from __future__ import annotations

import pytest

from torq_cli.domain.evidence_transitions import TRANSITION_RULES, TransitionRule
from torq_cli.domain.run_evidence import validate_v2_receipt_contract
from torq_cli.testing.fleet_preconditions import negative_precondition_case


@pytest.mark.parametrize(
    "rule",
    TRANSITION_RULES,
    ids=lambda rule: (
        f"{rule.writer_role}:{rule.transition}:{rule.decision_value or '-'}"
    ),
)
def test_every_transition_precondition_has_a_negative_chain_fixture(
    rule: TransitionRule,
) -> None:
    receipts, expected = negative_precondition_case(rule)
    assert validate_v2_receipt_contract(receipts, sealed=False) == expected
