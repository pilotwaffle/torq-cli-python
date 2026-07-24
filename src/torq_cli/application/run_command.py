"""Prime-directive attestation, cancellation checkpoint, and safe resume."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from torq_cli.application.orchestrator import GovernedOrchestrator, OrchestrationBlocked
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import ProfileSpec, load_registry
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


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
    def __init__(
        self,
        run_root: Path,
        orchestrator: GovernedOrchestrator | None = None,
    ) -> None:
        self.run_root = run_root
        self.orchestrator = orchestrator or GovernedOrchestrator()

    def start(
        self,
        identity: RunIdentity,
        actual: Mapping[str, str | None],
        *,
        expected: Mapping[str, str],
        live: bool = False,
        live_opt_in: bool = False,
        policy_opt_in: bool = False,
        goal: str = "",
        profile: ProfileSpec | None = None,
    ) -> dict[str, Any]:
        for field, expected_value in expected.items():
            observed = actual.get(field)
            if observed is None:
                raise ValueError(f"attestation_unattestable:{field}")
            if observed != expected_value:
                raise ValueError(f"attestation_mismatch:{field}")
        if live and not (live_opt_in and policy_opt_in):
            raise ValueError("double_opt_in_required")
        if live and self.orchestrator.dispatcher is None:
            raise OrchestrationBlocked("live_dispatcher_required")
        selected_profile = profile or self._profile(identity.profile_version)
        mode = "live" if live else "dry_run"
        run_id = "run-" + uuid.uuid4().hex
        chain = ReceiptChain(
            self.run_root,
            run_id,
            FileRunKeyStore(self.run_root),
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
        result = self.orchestrator.execute(
            goal=goal,
            profile=selected_profile,
            mode=ExecutionMode.LIVE if live else ExecutionMode.DRY_RUN,
            chain=chain,
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
            "verdict": result.status,
            "planned_roles": result.planned_roles,
            "dispatched_roles": result.dispatched_roles,
            "usage": result.usage,
            "proposal": result.proposal,
            "repair_cycles": result.repair_cycles,
            "timeline": result.timeline,
        }

    @staticmethod
    def _profile(profile_version: str) -> ProfileSpec:
        registry = load_registry()
        matches = [
            profile
            for profile in registry.profiles.values()
            if profile.default and profile.profile_version == profile_version
        ]
        if len(matches) != 1:
            raise ValueError("profile_version_unknown")
        return matches[0]

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
