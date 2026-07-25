from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.application.fleet import FleetProjector, reduce_fleet_snapshot
from torq_cli.application.orchestrator import GovernedOrchestrator, OrchestrationBlocked
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.domain.run_evidence import validate_v2_receipt_contract
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


_CEILINGS = {
    "g1d": 0.1,
    "g1r": 0.1,
    "builder": 0.1,
    "g2a": 0.1,
    "refine_bug": 0.1,
    "refine_ui": 0.1,
}


def _response(provider: str, model: str, body: Mapping[str, object]) -> NormalizedResponse:
    return NormalizedResponse(
        visible_text=json.dumps(body, sort_keys=True),
        reasoning_trace="",
        usage={"prompt_tokens": 2, "completion_tokens": 3},
        provenance=Provenance(provider, model, False),
    )


class _Dispatcher:
    def __init__(self, *, fail_role: str | None = None) -> None:
        self.fail_role = fail_role

    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        del prompt
        if role == self.fail_role:
            raise RuntimeError("transport unavailable")
        body: Mapping[str, object]
        if role == "g1d":
            body = {"status": "design_complete"}
        elif role == "g1r":
            body = {"verdict": "approve"}
        elif role == "builder":
            body = {"status": "build_complete"}
        elif role == "g2a":
            body = {"verdict": "approve", "defects": []}
        else:
            body = {"status": "repair_complete"}
        return _response(provider, model, body)


def _chain(tmp_path: Path, run_id: str = "run-contract") -> ReceiptChain:
    root = tmp_path / "evidence"
    return ReceiptChain(
        root,
        run_id,
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _orchestrator(dispatcher: _Dispatcher, *, budget: float = 1.0) -> GovernedOrchestrator:
    return GovernedOrchestrator(
        dispatcher,
        budget_usd=budget,
        cost_ceiling_usd_by_role=_CEILINGS,
    )


def _contract_receipt(
    sequence: int,
    transition: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "transition": transition,
        "writer_role": "orchestrator",
        "payload": dict(payload),
    }


def test_happy_path_projects_catalog_attempts_action_and_linked_closure(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = _chain(tmp_path)
    orchestrator = _orchestrator(_Dispatcher())

    result = orchestrator.execute(
        goal="Build Fleet backend",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert result.status == "awaiting_approval"
    assert verify_receipt_store(chain.root).status == "verified"
    snapshot = FleetProjector(chain.root).snapshot()
    assert [lane["role"] for lane in snapshot["lanes"]] == [
        "g1d",
        "g1r",
        "builder",
        "g2a",
        "refine_bug",
        "refine_ui",
    ]
    assert [lane["state"] for lane in snapshot["lanes"]] == [
        "sealed",
        "sealed",
        "sealed",
        "sealed",
        "dormant",
        "dormant",
    ]
    assert all(len(lane["attempts"]) == 1 for lane in snapshot["lanes"][:4])
    assert snapshot["run"]["workflow_state"] == "action_open"
    assert snapshot["summary"]["open_actions"] == 1
    action = snapshot["actions"][0]

    closure = orchestrator.resolve_action(
        chain,
        action_id=action["action_id"],
        resolution="approved",
        resolver_identity="operator:test",
    )

    assert closure["status"] == "workflow_closed"
    assert verify_receipt_store(chain.root).status == "verified"
    closed = FleetProjector(chain.root).snapshot()
    assert closed["verification"]["normalized_state"] == "sealed_verified"
    assert closed["run"]["workflow_state"] == "closed"
    assert closed["run"]["decision_writer"]["writer_role"] == "operator_gateway"
    assert closed["summary"]["open_actions"] == 0
    assert closed["actions"][0]["state"] == "resolved"


def test_transport_exception_writes_one_failed_terminal_attempt(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = _chain(tmp_path, "run-failed")

    with pytest.raises(OrchestrationBlocked, match="unexpected_stage_failure:g1d"):
        _orchestrator(_Dispatcher(fail_role="g1d")).execute(
            goal="Fail safely",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=chain,
        )
    chain.seal()

    assert verify_receipt_store(chain.root).status == "verified"
    receipts = [
        json.loads(line)
        for line in chain.receipts_path.read_text(encoding="utf-8").splitlines()
    ]
    attempt_rows = [
        row for row in receipts if row["payload"].get("attempt_id") is not None
    ]
    assert [row["transition"] for row in attempt_rows] == [
        "stage_attempt_created",
        "stage_dispatch_started",
        "stage_failed",
    ]
    assert attempt_rows[-1]["payload"]["provider_dispatch"] is True
    snapshot = FleetProjector(chain.root).snapshot()
    assert snapshot["lanes"][0]["state"] == "failed"
    assert snapshot["lanes"][0]["dispatch_message"] == "Provider transport attempted"


def test_preflight_refusal_never_claims_transport_attempt(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = _chain(tmp_path, "run-blocked")

    with pytest.raises(OrchestrationBlocked, match="budget_preflight_blocked:g1d"):
        _orchestrator(_Dispatcher(), budget=0.0).execute(
            goal="Refuse safely",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=chain,
        )
    chain.seal()

    receipts = [
        json.loads(line)
        for line in chain.receipts_path.read_text(encoding="utf-8").splitlines()
    ]
    transitions = [row["transition"] for row in receipts]
    assert "stage_dispatch_started" not in transitions
    blocked = next(row for row in receipts if row["transition"] == "stage_blocked")
    assert blocked["payload"]["provider_dispatch"] is False
    assert verify_receipt_store(chain.root).status == "verified"


def test_live_open_attempt_stays_running_but_sealed_is_error() -> None:
    receipts = [
        {
            "schema_version": "2.0.0",
            "sequence": 1,
            "transition": "stage_attempt_created",
            "writer_role": "orchestrator",
            "evidence_basis": "observed",
            "writer_key_id": "test",
            "payload": {
                "role": "g1d",
                "attempt_id": "attempt-1",
                "attempt_ordinal": 1,
                "repair_cycle": 0,
                "provider_dispatch": False,
            },
            "observed_at": "2026-07-24T00:00:00Z",
        }
    ]

    live = reduce_fleet_snapshot(
        receipts,
        {"run_id": "run", "sealed": False, "receipt_count": 1},
        verification_state="verified",
    )
    sealed = reduce_fleet_snapshot(
        receipts,
        {"run_id": "run", "sealed": True, "receipt_count": 1},
        verification_state="verified",
    )

    assert live["lanes"][0]["state"] == "running"
    assert live["data_status"] == "available"
    assert sealed["data_status"] == "reduction_error"
    assert sealed["summary"]["reduction_errors"] == ["attempt_terminal_missing"]


def test_awaiting_approval_cannot_be_abandoned_even_without_open_attempts(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-awaiting-approval")
    chain.append("run_decision", {"status": "awaiting_approval"})
    before = chain.receipts_path.read_bytes()

    with pytest.raises(ValueError, match="run_abandoned_operator_action_open"):
        chain.append(
            "run_abandoned",
            {
                "attempt_ids": ["invented-open-attempt"],
                "last_covered_sequence": 1,
                "operator_assertion": "no_live_worker",
            },
            writer_role="recovery",
            evidence_basis="submitted",
        )

    assert chain.receipts_path.read_bytes() == before


def test_open_operator_action_blocks_recovery_abandonment(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-action-open-recovery")
    chain.append("run_attested", {"mode": "live"})
    chain.append(
        "action_opened",
        {
            "action_id": "operator-review",
            "type": "approval_required",
            "scope": "run",
            "target": "operator",
            "summary": "Review the governed result.",
            "caused_by_sequence": 1,
        },
    )
    attempt = {
        "role": "g1d",
        "attempt_id": "attempt-action-open",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
    }
    chain.append(
        "stage_attempt_created",
        {**attempt, "provider_dispatch": False},
    )

    with pytest.raises(ValueError, match="run_abandoned_operator_action_open"):
        chain.append(
            "run_abandoned",
            {
                "attempt_ids": [attempt["attempt_id"]],
                "last_covered_sequence": 3,
                "operator_assertion": "no_live_worker",
            },
            writer_role="recovery",
            evidence_basis="submitted",
        )


def test_invented_orchestrator_decision_status_is_rejected_before_append(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-invented-decision")

    with pytest.raises(ValueError, match="run_decision_status_unauthorized"):
        chain.append("run_decision", {"status": "looks_finished_to_me"})

    assert chain.sequence == 0
    assert not chain.receipts_path.exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_payload_is_a_pre_append_finding(
    tmp_path: Path,
    value: float,
) -> None:
    chain = _chain(tmp_path, "run-non-finite")

    with pytest.raises(ValueError, match="receipt_payload_non_finite"):
        chain.append("run_attested", {"score": value})

    assert chain.sequence == 0
    assert not chain.receipts_path.exists()


def test_legacy_signed_non_finite_receipt_remains_verifiable(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-legacy-non-finite")
    chain.append("run_attested", {"score": 1.0})

    def legacy_canonical(value: Mapping[str, object]) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=True,
        ).encode()

    envelope = json.loads(chain.receipts_path.read_text(encoding="utf-8"))
    envelope.pop("receipt_hash")
    envelope["payload"] = {"score": float("nan")}
    writer_body = dict(envelope)
    writer_body.pop("writer_signature")
    envelope["writer_signature"] = Ed25519PrivateKey.from_private_bytes(
        chain.run_keys.orchestrator
    ).sign(legacy_canonical(writer_body)).hex()
    receipt_hash = "sha256:" + hashlib.sha256(legacy_canonical(envelope)).hexdigest()
    chain.receipts_path.write_bytes(
        legacy_canonical({**envelope, "receipt_hash": receipt_hash}) + b"\n"
    )

    manifest_path = chain.root / "terminal-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("signature")
    manifest["terminal_receipt_hash"] = receipt_hash
    manifest["signature"] = Ed25519PrivateKey.from_private_bytes(
        chain.run_keys.manifest
    ).sign(legacy_canonical(manifest)).hex()
    manifest_path.write_bytes(legacy_canonical(manifest))

    assert verify_receipt_store(chain.root).status == "verified"


def test_non_ascii_receipt_line_is_the_canonical_hashed_encoding(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-canonical")
    receipt = chain.append("run_attested", {"summary": "café ✓"})
    stored = chain.receipts_path.read_text(encoding="utf-8").rstrip("\n")

    assert stored == json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    body = dict(receipt)
    receipt_hash = body.pop("receipt_hash")
    assert ReceiptChain._hash(body) == receipt_hash


def test_lifecycle_contract_rejects_a_second_terminal_for_one_attempt() -> None:
    attempt = {
        "role": "g1d",
        "attempt_id": "attempt-1",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
    }
    receipts = [
        _contract_receipt(
            1,
            "stage_attempt_created",
            {**attempt, "provider_dispatch": False},
        ),
        _contract_receipt(
            2,
            "stage_dispatch_started",
            {**attempt, "provider_dispatch": True},
        ),
        _contract_receipt(
            3,
            "stage_completed",
            {**attempt, "provider_dispatch": True},
        ),
        _contract_receipt(
            4,
            "stage_failed",
            {**attempt, "provider_dispatch": True},
        ),
    ]

    assert validate_v2_receipt_contract(receipts, sealed=True) == (
        "attempt_transition_after_terminal"
    )
