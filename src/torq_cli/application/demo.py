"""Zero-configuration dry-run demo for TORQ.

``torq demo`` scaffolds the three JSON documents ``torq run`` normally
requires (identity, expected attestation, actual attestation) from the
installed registry and policy, then — with ``--run`` — executes a real
dry-run through the same RunController path the CLI uses. No provider is
contacted and nothing outside the chosen run root is written.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.domain.registry_schema import load_registry

# Attestation for the demo run: RunController.start compares expected and
# actual pairwise, so the demo pins one field both documents agree on.
_DEMO_ATTESTATION = {"demo": "local"}
_DEFAULT_GOAL = "Add input validation to the demo login form"


def _demo_identity() -> RunIdentity:
    """Build a valid identity for the packaged default profile and policy."""
    registry = load_registry()
    defaults = [p for p in registry.profiles.values() if p.default]
    if len(defaults) != 1:
        raise RuntimeError("demo_profile_ambiguous")
    profile = defaults[0]
    orchestrator = GovernedOrchestrator()
    return RunIdentity(
        profile_version=profile.profile_version,
        policy_version=orchestrator.policy.version,
        prompt_binding="demo",
        model_resolution="demo-local",
        sandbox_identity="demo-local",
        config_version=1,
        receipt_chain_hash="",
    )


def scaffold_demo(run_root: Path, goal: str) -> dict[str, Any]:
    """Write demo identity/expected/actual JSON and return a report."""
    identity = _demo_identity()
    inputs = run_root / "demo-inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "identity.json").write_text(
        json.dumps(asdict(identity), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name in ("expected.json", "actual.json"):
        (inputs / name).write_text(
            json.dumps(_DEMO_ATTESTATION, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return {"goal": goal, "inputs_dir": str(inputs), "identity": asdict(identity)}


def execute_demo(run_root: Path, goal: str) -> dict[str, Any]:
    """Run a real dry-run via the same controller the CLI uses."""
    controller = RunController(run_root)
    return controller.start(
        _demo_identity(),
        dict(_DEMO_ATTESTATION),
        expected=dict(_DEMO_ATTESTATION),
        goal=goal,
    )


def demo_command(goal: str, run_root: Path, execute: bool) -> int:
    """CLI entry point. Prints JSON reports like the other commands."""
    goal = goal or _DEFAULT_GOAL
    try:
        scaffold = scaffold_demo(run_root, goal)
        print(json.dumps({"status": "scaffolded", **scaffold}, sort_keys=True))
        if not execute:
            inputs = scaffold["inputs_dir"]
            print(
                json.dumps(
                    {
                        "status": "hint",
                        "run": (
                            "torq run --goal " + json.dumps(goal)
                            + " --run-root " + str(run_root)
                            + " --identity " + inputs + "/identity.json"
                            + " --expected " + inputs + "/expected.json"
                            + " --actual " + inputs + "/actual.json"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        report = execute_demo(run_root, goal)
        print(json.dumps({"status": "dry_run_complete", "report": report}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
        return 3
