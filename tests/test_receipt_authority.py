from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.application.fleet import FleetProjector
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    signing_file_permissions_are_restricted,
    verify_receipt_store,
)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _key_id(private_key: bytes) -> str:
    public = Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()
    return "sha256:" + hashlib.sha256(public).hexdigest()


def _chain(evidence_root: Path, name: str) -> ReceiptChain:
    return ReceiptChain(
        evidence_root,
        name,
        FileRunKeyStore(evidence_root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _rewrite_v2_and_resign(
    chain: ReceiptChain,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    rows = [
        json.loads(line)
        for line in chain.receipts_path.read_text(encoding="utf-8").splitlines()
    ]
    previous: str | None = None
    for row in rows:
        row.pop("receipt_hash")
        row.pop("writer_signature")
        mutate(row)
        role = str(row.get("writer_role"))
        private_key = getattr(chain.run_keys, role, chain.run_keys.orchestrator)
        if role in {"orchestrator", "supervisor", "operator_gateway"}:
            row["writer_key_id"] = _key_id(private_key)
        row["previous_receipt_hash"] = previous
        row["writer_signature"] = (
            Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(_canonical(row))
            .hex()
        )
        previous = ReceiptChain._hash(row)
        row["receipt_hash"] = previous
    chain.receipts_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    chain._sequence = len(rows)
    chain._previous = previous
    chain.seal()


def _convert_to_legacy(chain: ReceiptChain) -> None:
    rows = [
        json.loads(line)
        for line in chain.receipts_path.read_text(encoding="utf-8").splitlines()
    ]
    previous: str | None = None
    for row in rows:
        for field in (
            "receipt_hash",
            "writer_role",
            "evidence_basis",
            "writer_key_id",
            "writer_signature",
        ):
            row.pop(field)
        row["schema_version"] = "1.0.0"
        row["previous_receipt_hash"] = previous
        previous = ReceiptChain._hash(row)
        row["receipt_hash"] = previous
    chain.receipts_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    public_key = (
        Ed25519PrivateKey.from_private_bytes(chain.key).public_key().public_bytes_raw()
    )
    body = {
        "run_id": chain.run_id,
        "terminal_receipt_hash": previous,
        "receipt_count": len(rows),
        "sealed": True,
    }
    manifest = {
        **body,
        "public_key": public_key.hex(),
        "signature": Ed25519PrivateKey.from_private_bytes(chain.key)
        .sign(_canonical(body))
        .hex(),
    }
    (chain.root / "terminal-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )


def test_schema_v2_uses_root_certified_per_run_keys_and_separate_artifact_key(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    first = _chain(evidence_root, "run-1")
    second = _chain(evidence_root, "run-2")
    receipt = first.append("run_attested", {"mode": "live"})
    first.seal()

    certificate = json.loads(first.certificate_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (first.root / "terminal-manifest.json").read_text(encoding="utf-8")
    )

    assert receipt["schema_version"] == "2.0.0"
    assert receipt["writer_role"] == "orchestrator"
    assert receipt["evidence_basis"] == "observed"
    assert "writer_signature" in receipt
    assert manifest["manifest_key_id"] == certificate["manifest_key"]["key_id"]
    assert set(certificate["writers"]) == {
        "orchestrator",
        "supervisor",
        "operator_gateway",
    }
    assert first.run_keys.manifest != second.run_keys.manifest
    assert first.run_keys.orchestrator != second.run_keys.orchestrator
    assert first.run_keys.artifact != first.run_keys.manifest
    assert first.run_keys.artifact != second.run_keys.artifact
    assert verify_receipt_store(first.root).status == "verified"

    private_directory = evidence_root / ".torq-run-identities"
    assert private_directory.is_dir()
    assert not private_directory.is_relative_to(first.root)
    assert signing_file_permissions_are_restricted(private_directory)
    if os.name != "nt":
        assert stat.S_IMODE(private_directory.stat().st_mode) == 0o700
    for run_directory in private_directory.iterdir():
        assert signing_file_permissions_are_restricted(run_directory)
        if os.name != "nt":
            assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    for private_file in private_directory.rglob("*.key"):
        assert signing_file_permissions_are_restricted(private_file)
        if os.name != "nt":
            assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


def test_writer_permissions_allow_only_certified_role_basis_transitions(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "evidence", "run")
    attempt = {
        "role": "g1r",
        "attempt_id": "attempt-g1r-1",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
    }
    chain.append(
        "stage_attempt_created",
        {**attempt, "provider_dispatch": False},
    )
    interrupted = chain.append(
        "stage_interrupted",
        {**attempt, "provider_dispatch": "unknown"},
        writer_role="supervisor",
        evidence_basis="derived",
    )
    chain.append(
        "run_decision",
        {
            "status": "workflow_failed",
            "interruption_sequence": interrupted["sequence"],
        },
        writer_role="supervisor",
        evidence_basis="derived",
    )
    opened = chain.append(
        "action_opened",
        {
            "action_id": "approve-1",
            "type": "approval_required",
            "scope": "run",
            "target": "operator",
            "caused_by_sequence": interrupted["sequence"],
            "summary": "Review interruption",
        },
    )
    resolved = chain.append(
        "action_resolved",
        {
            "action_id": "approve-1",
            "resolution": "rejected",
            "resolver_identity": "operator:test",
            "opened_sequence": opened["sequence"],
        },
        writer_role="operator_gateway",
        evidence_basis="submitted",
    )
    chain.append(
        "run_decision",
        {
            "status": "workflow_closed",
            "action_id": "approve-1",
            "action_resolved_sequence": resolved["sequence"],
        },
        writer_role="operator_gateway",
        evidence_basis="derived",
    )
    assert verify_receipt_store(chain.root).status == "verified"

    with pytest.raises(ValueError, match="receipt_writer_unauthorized"):
        chain.append(
            "stage_completed",
            {**attempt, "provider_dispatch": True},
            writer_role="supervisor",
            evidence_basis="derived",
        )
    with pytest.raises(ValueError, match="receipt_writer_unauthorized"):
        chain.append(
            "action_opened",
            {
                "action_id": "approve-2",
                "type": "approval_required",
                "scope": "run",
                "target": "operator",
                "caused_by_sequence": resolved["sequence"],
                "summary": "Review",
            },
            writer_role="operator_gateway",
            evidence_basis="submitted",
        )
    with pytest.raises(ValueError, match="receipt_writer_unauthorized"):
        chain.append(
            "action_resolved",
            {
                "action_id": "approve-2",
                "resolution": "approved",
                "resolver_identity": "operator:test",
                "opened_sequence": opened["sequence"],
            },
        )


@pytest.mark.parametrize(
    ("mutate", "finding"),
    (
        (
            lambda row: row.update(
                writer_role="supervisor", evidence_basis="derived"
            ),
            "receipt_writer_unauthorized",
        ),
        (
            lambda row: row.update(
                writer_role="operator_gateway", evidence_basis="submitted"
            ),
            "receipt_writer_unauthorized",
        ),
        (
            lambda row: row.update(writer_role="rogue", evidence_basis="observed"),
            "receipt_writer_role_invalid",
        ),
    ),
)
def test_validly_resigned_unauthorized_writer_fails_closed(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    finding: str,
) -> None:
    chain = _chain(tmp_path / finding, "run")
    chain.append("run_attested", {"mode": "live"})
    _rewrite_v2_and_resign(chain, mutate)

    result = verify_receipt_store(chain.root)

    assert result.status == "tampered"
    assert result.finding == finding


def test_cross_run_certificate_and_chain_transplant_fails(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    first = _chain(evidence_root, "run-1")
    first.append("run_attested", {"mode": "live"})
    first.seal()
    second = _chain(evidence_root, "run-2")
    second.append("run_attested", {"mode": "live"})
    second.seal()

    for name in ("receipts.jsonl", "terminal-manifest.json", "run-certificate.json"):
        (second.root / name).write_bytes((first.root / name).read_bytes())

    result = verify_receipt_store(second.root)

    assert result.status == "tampered"
    assert result.finding == "run_certificate_invalid"


def test_schema_v1_verifies_and_fleet_labels_writer_legacy_unclassified(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "legacy", "run")
    chain.append("stage_started", {"role": "g1d"})
    _convert_to_legacy(chain)

    assert verify_receipt_store(chain.root).status == "verified"
    snapshot = FleetProjector(chain.root).snapshot()
    lane = snapshot["lanes"][0]
    assert lane["latest_writer_role"] == "legacy_unclassified"
    assert lane["latest_evidence_basis"] == "legacy_unclassified"
    assert lane["latest_writer_key_id"] == "legacy_unclassified"
