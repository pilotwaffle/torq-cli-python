from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from torq_cli.application.fleet import FleetProjector
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    verify_receipt_store,
)


def _chain(tmp_path: Path, name: str) -> ReceiptChain:
    evidence_root = tmp_path / name
    return ReceiptChain(
        evidence_root,
        "run",
        FileRunKeyStore(evidence_root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _rewrite_and_resign(
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
        mutate(row)
        row["previous_receipt_hash"] = previous
        previous = ReceiptChain._hash(row)
        row["receipt_hash"] = previous
    chain.receipts_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    chain._sequence = len(rows)
    chain._previous = previous
    chain.seal()


def test_receipt_authority_is_versioned_and_supervisor_is_transition_scoped(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "valid")

    worker = chain.append("stage_started", {"role": "g1d"})
    interrupted = chain.append(
        "stage_interrupted",
        {"role": "g1d", "reason": "worker_lease_expired"},
        authority="supervisor_derived",
    )
    decision = chain.append(
        "run_decision",
        {"status": "recovery_required"},
        authority="supervisor_derived",
    )

    assert worker["schema_version"] == "1.1.0"
    assert worker["authority"] == "worker"
    assert interrupted["authority"] == "supervisor_derived"
    assert decision["authority"] == "supervisor_derived"
    assert verify_receipt_store(chain.root).status == "verified"

    with pytest.raises(ValueError, match="receipt_authority_transition_invalid"):
        chain.append(
            "stage_completed",
            {"role": "g1d"},
            authority="supervisor_derived",
        )
    with pytest.raises(ValueError, match="receipt_authority_invalid"):
        chain.append("stage_interrupted", {"role": "g1d"}, authority="operator")


@pytest.mark.parametrize(
    ("mutate", "finding"),
    (
        (
            lambda row: row.__setitem__("authority", "supervisor_derived"),
            "receipt_authority_transition_invalid",
        ),
        (lambda row: row.pop("authority"), "receipt_authority_invalid"),
        (
            lambda row: row.__setitem__("schema_version", "9.0.0"),
            "receipt_schema_unsupported",
        ),
        (
            lambda row: row.__setitem__("schema_version", "1.0.0"),
            "receipt_authority_version_invalid",
        ),
    ),
)
def test_validly_resigned_invalid_authority_contract_fails_closed(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    finding: str,
) -> None:
    chain = _chain(tmp_path, finding)
    chain.append("stage_completed", {"role": "g1d"})
    _rewrite_and_resign(chain, mutate)

    result = verify_receipt_store(chain.root)

    assert result.status == "tampered"
    assert result.finding == finding


def test_legacy_receipt_without_authority_remains_verifiable(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "legacy")
    chain.append(
        "run_planned",
        {"mode": "dry_run", "profile_id": "legacy", "planned_roles": ["g1d"]},
    )
    chain.append("stage_started", {"role": "g1d"})

    def legacy(row: dict[str, Any]) -> None:
        row["schema_version"] = "1.0.0"
        row.pop("authority")

    _rewrite_and_resign(chain, legacy)

    assert verify_receipt_store(chain.root).status == "verified"
    snapshot = FleetProjector(chain.root).snapshot()
    assert snapshot["lanes"][0]["latest_authority"] == "legacy_unspecified"
    assert snapshot["lanes"][0]["transitions"][0]["authority"] == (
        "legacy_unspecified"
    )
