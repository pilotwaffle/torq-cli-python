"""Authority and encoding invariants for governed schema-v2 evidence.

Covers three gaps found reviewing `8d00380`: an unencodable payload that
signed cleanly, a `run_decision` status set that was open on the orchestrator
side, and `run_abandoned` reaching a run that was still waiting on a person.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from torq_cli.domain.evidence_transitions import transition_rule
from torq_cli.domain.run_evidence import validate_v2_receipt_contract
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


_ATTEMPT = {
    "role": "g1d",
    "attempt_id": "attempt-1",
    "attempt_ordinal": 1,
    "repair_cycle": 0,
}


def _chain(tmp_path: Path) -> ReceiptChain:
    root = tmp_path / "evidence"
    return ReceiptChain(
        root,
        "run-authority",
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _receipt(
    sequence: int,
    transition: str,
    payload: Mapping[str, object],
    *,
    writer_role: str = "orchestrator",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "transition": transition,
        "writer_role": writer_role,
        "payload": dict(payload),
    }


def _open_attempt(sequence: int = 1) -> dict[str, object]:
    return _receipt(
        sequence,
        "stage_attempt_created",
        {**_ATTEMPT, "provider_dispatch": False},
    )


def _abandon(sequence: int, last_covered: int) -> dict[str, object]:
    return _receipt(
        sequence,
        "run_abandoned",
        {
            "attempt_ids": [_ATTEMPT["attempt_id"]],
            "last_covered_sequence": last_covered,
            "operator_assertion": "no_live_worker",
        },
        writer_role="recovery",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_payload_is_rejected_before_it_is_signed(
    tmp_path: Path,
    value: float,
) -> None:
    """`NaN`/`Infinity` are not JSON, so they must never reach signed bytes."""
    chain = _chain(tmp_path)

    with pytest.raises(ValueError, match="receipt_payload_unencodable"):
        chain.append("run_planned", {"mode": "live", "drift": value})

    assert not chain.receipts_path.exists()


def test_unserializable_payload_is_rejected_before_it_is_signed(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path)

    with pytest.raises(ValueError, match="receipt_payload_unencodable"):
        chain.append("run_planned", {"mode": "live", "roles": {"g1d"}})

    assert not chain.receipts_path.exists()


def test_every_run_decision_rule_declares_a_closed_status_set() -> None:
    """Authority is the (role, transition, status) triple, not the pair."""
    for role in ("orchestrator", "supervisor", "operator_gateway"):
        rule = transition_rule(role, "run_decision")
        assert rule is not None
        assert rule.statuses


def test_orchestrator_cannot_close_a_run_with_an_undeclared_status() -> None:
    receipts = [
        _open_attempt(),
        _receipt(2, "run_decision", {"status": "looks_fine_to_me"}),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=False) == (
        "run_decision_status_invalid"
    )


def test_supervisor_cannot_declare_a_status_reserved_for_another_writer() -> None:
    receipts = [
        _open_attempt(),
        _receipt(
            2,
            "run_decision",
            {"status": "workflow_closed", "interruption_sequence": 1},
            writer_role="supervisor",
        ),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=False) == (
        "run_decision_status_invalid"
    )


def test_run_abandoned_cannot_close_a_run_awaiting_approval() -> None:
    receipts = [
        _open_attempt(),
        _receipt(2, "run_decision", {"status": "awaiting_approval"}),
        _abandon(3, last_covered=2),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=False) == (
        "run_abandoned_awaiting_approval"
    )


def test_run_abandoned_cannot_close_a_run_with_an_open_action() -> None:
    receipts = [
        _open_attempt(),
        _receipt(
            2,
            "action_opened",
            {
                "action_id": "action-1",
                "type": "approval",
                "scope": "run",
                "target": "g1d",
                "summary": "Approve the design",
                "caused_by_sequence": 1,
            },
        ),
        _abandon(3, last_covered=2),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=False) == (
        "run_abandoned_actions_open"
    )


def test_run_abandoned_still_closes_an_unattended_run() -> None:
    """The guards above must not disable recovery on a genuinely dead run."""
    receipts = [
        _open_attempt(),
        _abandon(2, last_covered=1),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=True) is None
