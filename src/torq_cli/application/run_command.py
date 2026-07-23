"""Prime-directive attestation, cancellation checkpoint, and safe resume."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
        del identity
        for field, expected_value in expected.items():
            observed = actual.get(field)
            if observed is None:
                raise ValueError(f"attestation_unattestable:{field}")
            if observed != expected_value:
                raise ValueError(f"attestation_mismatch:{field}")
        if live and not (live_opt_in and policy_opt_in):
            raise ValueError("double_opt_in_required")
        return {"mode": "live" if live else "dry_run", "attested": True}

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

