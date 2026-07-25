"""Pin the verifier-side bounds that keep operator prose out of receipt bodies.

The Release 3 contract already routes operator content into an encrypted
artifact and records only a hash. These tests hold the *verifier* — the portable
trust boundary — to the same discipline: a receipt written by any path, not only
the sanctioned producer, must have its operator-influenced fields bounded, so a
receipt cannot become a channel for unbounded prose that later renders in a UI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from torq_cli.domain.run_evidence import validate_receipt_payload
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    verify_receipt_store,
)


_PROSE = "LEAK: merger terms do not disclose " * 3000  # ~100 KB
#: Over every bound in the contract, but short enough that pytest can put it in
#: a parametrization id without overflowing the Windows environment-block limit.
_OVER_BOUND = "x" * 700


def _accepted_payload() -> dict[str, object]:
    return {
        "command_id": "cmd-abc123",
        "command_type": "context",
        "context_id": "ctx-abc123",
        "target_role": "lead",
        "route": "lead_replan",
        "artifact": "artifacts/ctx-abc123.enc",
        "artifact_hash": "sha256:" + "0" * 64,
        "media_type": "text/plain",
        "content_bytes": 10,
        "provider_dispatch": False,
        "direct_route_confirmed": True,
        "earliest_eligible_attempt": {
            "kind": "attempt_created_after_acknowledgement"
        },
    }


def test_baseline_command_accepted_still_validates() -> None:
    assert (
        validate_receipt_payload(
            "command_accepted", _accepted_payload(), writer_role="operator_gateway"
        )
        is None
    )


@pytest.mark.parametrize(
    "case",
    [
        "media_type_prose",
        "media_type_space",
        "redactions_prose",
        "redactions_str",
        "content_bytes_str",
        "content_bytes_over",
        "provider_dispatch_int",
        "context_id_spaces",
        "source_name_long",
    ],
)
def test_command_accepted_rejects_prose_shaped_values(case: str) -> None:
    value: object = {
        "media_type_prose": ("media_type", "text/" + _OVER_BOUND),
        "media_type_space": ("media_type", "text/with a space"),
        "redactions_prose": ("redactions", [_OVER_BOUND]),
        "redactions_str": ("redactions", "KEY_ASSIGNMENT"),
        "content_bytes_str": ("content_bytes", "not-a-number"),
        "content_bytes_over": ("content_bytes", 1_048_577),
        "provider_dispatch_int": ("provider_dispatch", 1),
        "context_id_spaces": ("context_id", "a sentence with spaces"),
        "source_name_long": ("source_name", "x" * 300),
    }[case]
    field, bad = value  # type: ignore[misc]
    payload = {**_accepted_payload(), field: bad}
    assert (
        validate_receipt_payload(
            "command_accepted", payload, writer_role="operator_gateway"
        )
        == "command_accept_invalid"
    )


def test_redaction_list_length_is_capped() -> None:
    # Each token is a valid pattern name, so no single item is prose — but a
    # list far longer than the registry could produce is bytes accumulating one
    # valid token at a time, and is refused.
    payload = {**_accepted_payload(), "redactions": ["KEY_ASSIGNMENT"] * 100}
    assert (
        validate_receipt_payload(
            "command_accepted", payload, writer_role="operator_gateway"
        )
        == "command_accept_invalid"
    )
    ok = {**_accepted_payload(), "redactions": ["KEY_ASSIGNMENT", "PRIVATE_KEY"]}
    assert (
        validate_receipt_payload(
            "command_accepted", ok, writer_role="operator_gateway"
        )
        is None
    )


def test_dispatched_roles_length_is_capped() -> None:
    assert (
        validate_receipt_payload(
            "run_decision",
            {
                "status": "blocked",
                "dispatched_roles": ["g1d"] * 100,
                "provider_dispatch": False,
            },
            writer_role="orchestrator",
        )
        == "run_decision_text_invalid"
    )


def test_command_accepted_rejects_an_undeclared_key() -> None:
    payload = {**_accepted_payload(), "operator_note": _OVER_BOUND}
    assert (
        validate_receipt_payload(
            "command_accepted", payload, writer_role="operator_gateway"
        )
        == "command_accept_invalid"
    )


def test_action_opened_rejects_unbounded_summary() -> None:
    assert (
        validate_receipt_payload(
            "action_opened",
            {
                "action_id": "a1",
                "type": "approval",
                "scope": "run",
                "target": "lead",
                "summary": _OVER_BOUND,
                "caused_by_sequence": 1,
            },
            writer_role="orchestrator",
        )
        == "action_opened_invalid"
    )


def test_action_opened_rejects_an_undeclared_key() -> None:
    assert (
        validate_receipt_payload(
            "action_opened",
            {
                "action_id": "a1",
                "type": "approval",
                "scope": "run",
                "target": "lead",
                "summary": "Approve the plan",
                "caused_by_sequence": 1,
                "operator_note": _OVER_BOUND,
            },
            writer_role="orchestrator",
        )
        == "action_opened_invalid"
    )


def test_action_resolved_rejects_unbounded_resolution() -> None:
    assert (
        validate_receipt_payload(
            "action_resolved",
            {
                "action_id": "a1",
                "resolution": _OVER_BOUND,
                "resolver_identity": "operator",
                "opened_sequence": 1,
            },
            writer_role="operator_gateway",
        )
        == "action_resolved_invalid"
    )


def test_run_decision_rejects_prose_and_undeclared_keys() -> None:
    assert (
        validate_receipt_payload(
            "run_decision",
            {"status": "blocked", "reason": _OVER_BOUND, "provider_dispatch": False},
            writer_role="orchestrator",
        )
        == "run_decision_text_invalid"
    )
    assert (
        validate_receipt_payload(
            "run_decision",
            {"status": "blocked", "note": _OVER_BOUND, "provider_dispatch": False},
            writer_role="orchestrator",
        )
        == "run_decision_text_invalid"
    )
    assert (
        validate_receipt_payload(
            "run_decision",
            {
                "status": "blocked",
                "reason": "live_dispatcher_required",
                "dispatched_roles": ["g1d", "g1r"],
                "provider_dispatch": False,
            },
            writer_role="orchestrator",
        )
        is None
    )


def test_a_prose_summary_can_no_longer_be_sealed_into_a_verified_store() -> None:
    # The end-to-end form of the finding: on the unhardened contract a 100 KB
    # operator summary appended, sealed, and verified clean. It must now be
    # refused at append, so no such store can exist.
    root = Path(tempfile.mkdtemp()) / "ev"
    chain = ReceiptChain(
        root,
        "run-prose",
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    planned = chain.append("run_attested", {"mode": "live"})
    with pytest.raises(ValueError, match="action_opened_invalid"):
        chain.append(
            "action_opened",
            {
                "action_id": "a1",
                "type": "approval",
                "scope": "run",
                "target": "lead",
                "summary": _OVER_BOUND,
                "caused_by_sequence": planned["sequence"],
            },
            writer_role="orchestrator",
        )
    # The store never accepted the prose, so it stays empty of it.
    text = chain.receipts_path.read_text(encoding="utf-8")
    assert "merger terms" not in text


def test_command_rejected_may_still_record_the_input_it_refused() -> None:
    # A rejection echoes the refused input, and the reason may be that the input
    # was malformed. The bound must not stop a rejection from being recorded —
    # only stop it carrying a document.
    ok = validate_receipt_payload(
        "command_rejected",
        {
            "command_id": "cmd-1",
            "command_type": "context",
            "target_role": "unknown-lane",
            "media_type": "image/png",
            "finding": "context_media_type_invalid",
            "content_bytes": 1_048_577,
            "provider_dispatch": False,
        },
        writer_role="operator_gateway",
    )
    assert ok is None
    prose = validate_receipt_payload(
        "command_rejected",
        {
            "command_id": "cmd-1",
            "command_type": "context",
            "media_type": _OVER_BOUND,
            "finding": "context_media_type_invalid",
            "content_bytes": 0,
            "provider_dispatch": False,
        },
        writer_role="operator_gateway",
    )
    assert prose == "command_rejection_invalid"


def test_oversize_floor_applies_to_every_transition() -> None:
    # The floor under all transitions, including the writer-only lifecycle ones
    # (run_planned, stage_*) that no operator ingress reaches. A document-sized
    # string anywhere in any payload is refused before its transition-specific
    # checks even run.
    assert (
        validate_receipt_payload(
            "run_planned",
            {
                "mode": "live",
                "profile_id": "p",
                "strategy_id": "s",
                "planned_roles": ["g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"],
                "lane_catalog": [
                    {"role": r}
                    for r in ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
                ],
                "operator_note": _PROSE,
            },
            writer_role="orchestrator",
        )
        == "receipt_value_oversized"
    )
    assert (
        validate_receipt_payload(
            "stage_attempt_created",
            {
                "role": "g1d",
                "attempt_id": "a1",
                "attempt_ordinal": 1,
                "repair_cycle": 0,
                "provider_dispatch": False,
                "leak": _PROSE,
            },
            writer_role="orchestrator",
        )
        == "receipt_value_oversized"
    )


def test_run_planned_and_run_abandoned_reject_undeclared_keys() -> None:
    assert (
        validate_receipt_payload(
            "run_planned",
            {
                "mode": "live",
                "profile_id": "p",
                "strategy_id": "s",
                "planned_roles": ["g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"],
                "lane_catalog": [
                    {"role": r}
                    for r in ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
                ],
                "smuggled": "note",
            },
            writer_role="orchestrator",
        )
        == "lane_catalog_invalid"
    )
    assert (
        validate_receipt_payload(
            "run_abandoned",
            {
                "attempt_ids": ["a1"],
                "last_covered_sequence": 1,
                "operator_assertion": "no_live_worker",
                "smuggled": "note",
            },
            writer_role="recovery",
        )
        == "run_abandoned_invalid"
    )


def test_verify_store_helper_is_importable() -> None:
    # Guards against an accidental import break in the module under test.
    assert callable(verify_receipt_store)
