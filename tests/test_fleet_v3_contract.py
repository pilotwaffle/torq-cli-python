from __future__ import annotations

from typing import Any

import pytest

from torq_cli.application.fleet import reduce_fleet_snapshot
from torq_cli.application.fleet_controls import (
    FleetControlService,
    compose_fleet_envelope,
)
from torq_cli.interfaces.fleet_http import FleetSessionManager


def _snapshot(*, state: str = "live_verified", covered: int = 4) -> dict[str, Any]:
    return {
        "schema": "torq-fleet-snapshot-v3",
        "verification": {
            "state": state,
            "finding": None,
            "covered_sequence": covered,
            "store_sequence": covered,
            "manifest_generation": None,
        },
        "data_status": "available" if state == "live_verified" else "unavailable",
        "run": {
            "run_id": "run-v3",
            "workflow_state": "open",
            "execution_state": "running",
        },
        "summary": {"open_actions": 1, "reduction_errors": []},
        "lanes": [],
        "actions": [
            {
                "action_id": "approve-1",
                "state": "open",
                "allowed_resolutions": ["approved", "rejected"],
                "outcome_map": {
                    "approved": "completed",
                    "rejected": "blocked",
                },
            }
        ],
        "settlement": None,
    }


def test_v3_envelope_keeps_operational_state_outside_the_snapshot() -> None:
    snapshot = _snapshot()
    envelope = compose_fleet_envelope(
        snapshot,
        session_write_capable=True,
        expires_at="2026-07-26T00:00:00Z",
        service_availability={
            "context": True,
            "resolve_action": True,
            "recover_run": True,
        },
        annotations=[
            {
                "kind": "recovery_required",
                "scope": "run",
                "observed_at": "2026-07-25T12:00:00Z",
                "source": "supervisor",
            }
        ],
        pending=["corr-1"],
    )

    assert set(envelope) == {
        "snapshot",
        "annotations",
        "session",
        "eligibility",
        "pending",
    }
    assert envelope["snapshot"] == snapshot
    assert "annotations" not in envelope["snapshot"]["run"]
    assert "normalized_state" not in envelope["snapshot"]["verification"]
    assert envelope["eligibility"]["context"] == {
        "eligible": True,
        "reason": "eligible",
    }
    assert envelope["eligibility"]["resolve_action"]["approve-1"]["eligible"]
    assert envelope["eligibility"]["recover_run"]["eligible"]
    assert envelope["pending"] == ["corr-1"]


def test_eligibility_reasons_distinguish_trust_session_service_and_precondition() -> None:
    tampered = compose_fleet_envelope(
        _snapshot(state="tampered"),
        session_write_capable=True,
        expires_at=None,
        service_availability={"context": True},
    )
    assert tampered["eligibility"]["context"]["reason"] == "verification_not_mutable"

    read_only = compose_fleet_envelope(
        _snapshot(),
        session_write_capable=False,
        expires_at=None,
        service_availability={"context": True},
    )
    assert read_only["eligibility"]["context"]["reason"] == "session_read_only"

    unavailable = compose_fleet_envelope(
        _snapshot(),
        session_write_capable=True,
        expires_at=None,
    )
    assert unavailable["eligibility"]["context"]["reason"] == (
        "context_service_unavailable"
    )
    assert unavailable["eligibility"]["recover_run"]["reason"] == (
        "recover_run_service_unavailable"
    )


def test_pending_correlations_clear_only_after_manifest_coverage() -> None:
    controls = FleetControlService(context_available=True)
    controls.begin("corr-1", "context")
    first = controls.envelope(
        _snapshot(covered=4),
        session_write_capable=True,
        expires_at=None,
    )
    assert first["pending"] == ["corr-1"]
    with pytest.raises(ValueError, match="fleet_correlation_duplicate"):
        controls.begin("corr-1", "context")

    controls.committed("corr-1", 5)
    lagging = controls.envelope(
        _snapshot(covered=4),
        session_write_capable=True,
        expires_at=None,
    )
    covered = controls.envelope(
        _snapshot(covered=5),
        session_write_capable=True,
        expires_at=None,
    )
    assert lagging["pending"] == ["corr-1"]
    assert covered["pending"] == []


def test_legacy_certificate_operator_fields_are_never_rendered_as_safe_data() -> None:
    receipts = [
        {
            "schema_version": "2.0.0",
            "sequence": 1,
            "transition": "action_opened",
            "writer_role": "orchestrator",
            "evidence_basis": "derived",
            "writer_key_id": "legacy",
            "observed_at": "2026-07-25T12:00:00Z",
            "payload": {
                "action_id": "action-1",
                "type": "approval_required",
                "scope": "run",
                "target": "operator",
                "summary": "legacy operator prose",
            },
        }
    ]
    snapshot = reduce_fleet_snapshot(
        receipts,
        {"run_id": "legacy", "receipt_count": 1, "sealed": True},
        verification_state="verified",
        legacy_certificate=True,
    )

    action = snapshot["actions"][0]
    assert snapshot["data_status"] == "reduction_error"
    assert "legacy_operator_fields_suppressed" in snapshot["summary"][
        "reduction_errors"
    ]
    assert action["summary"] is None
    assert action["allowed_resolutions"] == []
    assert action["outcome_map"] == {}


def test_session_projects_effective_expiry_without_exposing_token() -> None:
    monotonic_now = [100.0]
    wall_now = [1_000.0]
    manager = FleetSessionManager(
        clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now[0],
        idle_seconds=10,
        absolute_seconds=100,
    )
    token = manager.exchange(manager.bootstrap_nonce)
    session = manager.authenticate(f"torq_fleet_session={token}")

    assert session is not None
    assert manager.expires_at(session) == "1970-01-01T00:16:50Z"
    assert token not in manager.expires_at(session)
