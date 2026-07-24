"""Prime-directive attestation, cancellation checkpoint, and safe resume."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from torq_cli.safety.receipts import MemoryRunKeyStore, ReceiptChain, verify_receipt_store


class ResumeMismatch(ValueError):
    pass


@dataclass(frozen=True)
class RunIdentity:
    profile_version: str
    policy_version: str
    prompt_binding: str
    model_resolution: str
    sandbox_identity: str
    config_version: int
    receipt_chain_hash: str


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    identity: RunIdentity
    completed: tuple[str, ...]


class RunController:
    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root

    def start(
        self,
        identity: RunIdentity,
        actual: Mapping[str, str | None],
        *,
        expected: Mapping[str, str],
        live: bool = False,
        live_opt_in: bool = False,
        policy_opt_in: bool = False,
    ) -> dict[str, Any]:
        for field, expected_value in expected.items():
            observed = actual.get(field)
            if observed is None:
                raise ValueError(f"attestation_unattestable:{field}")
            if observed != expected_value:
                raise ValueError(f"attestation_mismatch:{field}")
        if live and not (live_opt_in and policy_opt_in):
            raise ValueError("double_opt_in_required")
        mode = "live" if live else "dry_run"
        run_id = "run-" + uuid.uuid4().hex
        chain = ReceiptChain(
            self.run_root,
            run_id,
            MemoryRunKeyStore(),
            profile_version=identity.profile_version,
            policy_version=identity.policy_version,
        )
        chain.append(
            "run_attested",
            {
                "mode": mode,
                "identity": asdict(identity),
                "attested_fields": sorted(expected),
            },
        )
        chain.seal()
        verification = verify_receipt_store(chain.root)
        if verification.status != "verified":
            raise RuntimeError(f"receipt_verification_failed:{verification.finding}")
        return {
            "mode": mode,
            "attested": True,
            "run_id": run_id,
            "receipts": str(chain.root),
        }

    def cancel(self, run_id: str, identity: RunIdentity, *, completed: tuple[str, ...]) -> Path:
        directory = self.run_root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint = directory / "checkpoint.json"
        checkpoint.write_text(json.dumps({"run_id": run_id, "identity": asdict(identity), "completed": completed}, sort_keys=True), encoding="utf-8")
        (directory / "cancellation-receipt.json").write_text(json.dumps({"state": "cancelled", "process_tree": "terminated"}, sort_keys=True), encoding="utf-8")
        return checkpoint

    def resume(self, checkpoint_path: Path, identity: RunIdentity, *, stages: Sequence[str]) -> tuple[str, ...]:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        saved = payload.get("identity", {})
        for field, current in asdict(identity).items():
            if saved.get(field) != current:
                raise ResumeMismatch(f"resume_mismatch:{field}")
        completed = set(payload.get("completed", ()))
        return tuple(stage for stage in stages if stage not in completed)
