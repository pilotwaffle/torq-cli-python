"""Run the T-33 governed live profile against an immutable proposal target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.connectors.credential_sources import ExplicitEnvVault
from torq_cli.connectors.live_dispatch import LiveStageDispatcher
from torq_cli.domain.config_schema import parse_config_bytes, validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.safety.entitlements import InMemoryEntitlementLedger
from torq_cli.safety.receipts import restrict_receipt_trust_anchor, verify_receipt_store


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _target(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "torq-safe-target-v1":
        raise ValueError("safe_target_schema_invalid")
    if payload.get("mode") != "proposal_only" or payload.get("allow_mutation") is not False:
        raise ValueError("safe_target_must_be_proposal_only")
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("safe_target_description_required")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--budget-usd", type=float, default=1.0)
    parser.add_argument("--role-ceiling-usd", type=float, default=0.10)
    parser.add_argument("--authorize-live", action="store_true")
    args = parser.parse_args()
    if not args.authorize_live:
        parser.error("--authorize-live is required")
    if args.budget_usd <= 0 or args.role_ceiling_usd <= 0:
        parser.error("cost limits must be positive")

    target = _target(args.target_manifest)
    target_hash = _sha256(args.target_manifest)
    registry = load_registry()
    config = parse_config_bytes(args.config.read_bytes())
    findings = validate_config(config, registry)
    if findings:
        raise ValueError("governed_config_invalid")
    entitlement_accounts = config.get("entitlement_accounts")
    if not isinstance(entitlement_accounts, Mapping):
        raise ValueError("entitlement_accounts_required")
    entitlement_ledger = InMemoryEntitlementLedger.from_config(entitlement_accounts)
    profile = registry.profiles["torq-v5-6-live"]
    vault = ExplicitEnvVault(args.credential_file)
    orchestrator = GovernedOrchestrator(
        LiveStageDispatcher(vault, os.environ),
        loop_budget=1,
        budget_usd=args.budget_usd,
        # Only the OpenAI Responses API lane is metered. Subscription lanes are
        # governed by the account-keyed entitlement ledger instead.
        cost_ceiling_usd_by_role={"g2a": args.role_ceiling_usd},
        entitlement_ledger=entitlement_ledger,
    )
    identity = RunIdentity(
        profile.profile_version,
        orchestrator.policy.version,
        "registry-v1",
        "profile-bound-live",
        target_hash,
        1,
        "new-receipt-chain",
    )
    result = RunController(args.run_root, orchestrator).start(
        identity,
        {"profile": profile.profile_id, "target_hash": target_hash},
        expected={"profile": profile.profile_id, "target_hash": target_hash},
        live=True,
        live_opt_in=True,
        policy_opt_in=True,
        goal=str(target["description"]),
        profile=profile,
    )
    receipt_root = Path(str(result["receipts"]))
    verification = verify_receipt_store(receipt_root)
    if verification.status != "verified":
        raise RuntimeError(f"receipt_verification_failed:{verification.finding}")
    if result["verdict"] != "awaiting_approval":
        raise RuntimeError("governed_run_not_awaiting_approval")
    if _sha256(args.target_manifest) != target_hash:
        raise RuntimeError("safe_target_mutated")

    receipts = [
        json.loads(line)
        for line in (receipt_root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    stages = [
        receipt["payload"]
        for receipt in receipts
        if receipt.get("transition") == "stage_completed"
    ]
    providers = sorted({str(stage["provider"]) for stage in stages})
    if len(providers) < 2:
        raise RuntimeError("heterogeneous_provider_requirement_failed")
    if any(receipt.get("transition") == "approval_apply" for receipt in receipts):
        raise RuntimeError("proposal_boundary_violated")

    if args.export_root.exists():
        raise RuntimeError("evidence_export_root_exists")
    exported_receipts = args.export_root / "run"
    exported_anchor = args.export_root / ".torq-receipt-signing-key.pub"
    args.export_root.mkdir(parents=True)
    shutil.copytree(receipt_root, exported_receipts)
    shutil.copy2(args.run_root / ".torq-receipt-signing-key.pub", exported_anchor)
    restrict_receipt_trust_anchor(exported_anchor)
    trusted_public_key = bytes.fromhex(exported_anchor.read_text(encoding="ascii").strip())
    exported_verification = verify_receipt_store(
        exported_receipts, trusted_public_key=trusted_public_key
    )
    if exported_verification.status != "verified":
        raise RuntimeError(
            f"exported_receipt_verification_failed:{exported_verification.finding}"
        )

    report = {
        "schema": "torq-governed-live-report-v1",
        "date": args.date,
        "provenance": {
            "kind": "machine_generated_governed_live_runner",
            "machine_generated": True,
            "receipt_backed": True,
        },
        "target": {
            "mode": "proposal_only",
            "allow_mutation": False,
            "manifest_sha256": target_hash,
        },
        "run": {
            "run_id": result["run_id"],
            "verdict": result["verdict"],
            "providers": providers,
            "stages": [
                {"role": stage["role"], "provider": stage["provider"], "model": stage["model"]}
                for stage in stages
            ],
            "receipt_root": os.path.relpath(exported_receipts, args.report.parent),
            "trusted_public_key": os.path.relpath(exported_anchor, args.report.parent),
            "receipt_verification": exported_verification.status,
            "usage": result["usage"],
            "proposal": result["proposal"],
            "application_performed": False,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    print(json.dumps({"report": str(args.report), "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
