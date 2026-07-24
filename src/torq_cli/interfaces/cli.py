"""Argparse interface for the Foundation commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from torq_cli import __version__
from torq_cli.application import import_v5_config, import_v5_console
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.application.resolve import envelope_to_dict, resolve_path
from torq_cli.application.setup import SetupError, SetupService
from torq_cli.application.status_effective import effective_status
from torq_cli.connectors.status import inspect_harness
from torq_cli.connectors.credential_sources import CredentialSourceError, ExplicitEnvVault
from torq_cli.domain.models import ResultEnvelope
from torq_cli.domain.provider_matrix import PROVIDERS, load_provider_matrix
from torq_cli.safety.receipts import verify_receipt_store


def exit_code_for(status: str, require_effective: bool, findings: Sequence[object]) -> int:
    classes = {getattr(finding, "status_class", "") for finding in findings}
    if "internal_error" in classes or status == "internal_error":
        return 5
    if "invalid" in classes or status == "invalid":
        return 2
    if "blocked" in classes or status == "blocked":
        return 3
    if require_effective and status == "unattested":
        return 4
    return 0


def _print_envelope(envelope: ResultEnvelope, *, compact: bool) -> bool:
    rendering_failed = False
    try:
        rendered: dict[str, Any] = envelope_to_dict(envelope)
    except AttributeError:
        rendering_failed = True
        rendered = {
            "schema_version": "1.0.0",
            "command": envelope.command,
            "status": "internal_error",
            "snapshot": None,
            "findings": [{
                "id": "internal_error",
                "message": "Internal failure occurred without exposing details.",
                "severity": "critical",
                "bucket": "A",
                "status_class": "internal_error",
                "stage": "complete",
                "path": "/",
                "context": {},
            }],
            "data": {},
        }
    if compact:
        print(json.dumps(rendered, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(rendered, sort_keys=True))
    return rendering_failed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="torq")
    parser.add_argument("--version", action="version", version=f"torq {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    profile = sub.add_parser("profile")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    validate = profile_sub.add_parser("validate")
    validate.add_argument("--config", required=True)
    status = sub.add_parser("status")
    status.add_argument("--offline", action="store_true")
    status.add_argument("--config", required=True)
    status.add_argument("--require-effective", action="store_true")
    status.add_argument("--runtime")
    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    import_v5 = config_sub.add_parser("import-v5-normalized")
    import_v5.add_argument("--config", required=True)
    import_v5_console_parser = config_sub.add_parser("import-v5-console")
    import_v5_console_parser.add_argument("--config", required=True)
    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_status_parser = auth_sub.add_parser("status")
    auth_status_parser.add_argument("--credential-file")
    harness = sub.add_parser("harness")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)
    inspect = harness_sub.add_parser("inspect")
    inspect.add_argument("--expected", required=True)
    inspect.add_argument("--actual", required=True)
    setup = sub.add_parser("setup")
    setup.add_argument("--config", required=True)
    setup.add_argument("--answers", required=True)
    setup.add_argument("--credential-file")
    run = sub.add_parser("run")
    run.add_argument("--goal")
    run.add_argument("--resume")
    run.add_argument("--run-root", required=True)
    run.add_argument("--identity", required=True)
    run.add_argument("--expected", required=True)
    run.add_argument("--actual", required=True)
    run.add_argument("--live", action="store_true")
    run.add_argument("--allow-live", action="store_true")
    run.add_argument("--policy-allow-live", action="store_true")
    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    verify = evidence_sub.add_parser("verify")
    verify.add_argument("--run-root", required=True)
    return parser


def _matrix_auth_status(credential_file: str | None = None) -> dict[str, Any]:
    matrix = load_provider_matrix()
    configured = (
        ExplicitEnvVault(Path(credential_file)).configured_providers()
        if credential_file is not None
        else frozenset()
    )
    rows: dict[str, Any] = {}
    for provider in PROVIDERS:
        surfaces = matrix["providers"][provider]["surfaces"]
        auth = surfaces["authentication"]["status"]
        identity = surfaces["resolved_model_identity"]["status"]
        if "blocked" in {auth, identity}:
            state = "blocked"
        elif auth == "verified" and identity == "verified":
            state = "available"
        else:
            state = "unavailable"
        credential_configured = provider in configured
        if credential_configured:
            auth = "configured"
            state = "unavailable"
            identity = "unattestable"
        rows[provider] = {
            "state": state,
            "authentication": auth,
            "resolved_model_identity": identity,
            "credential_configured": credential_configured,
        }
    blocked = any(row["state"] != "available" for row in rows.values())
    return {"providers": rows, "exit_code": 3 if blocked else 0}


def main(argv: Sequence[str] | None = None) -> int:
    supplied = list(argv) if argv is not None else sys.argv[1:]
    import_boundary = (
        import_v5_console
        if supplied[:2] == ["config", "import-v5-console"]
        else import_v5_config
    )
    if any(argument == "--output" or argument.startswith("--output=") for argument in supplied):
        envelope = import_boundary.output_rejected()
        return 5 if _print_envelope(envelope, compact=True) else 2
    args = _parser().parse_args(argv)
    if args.command == "setup":
        try:
            answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
            if args.credential_file is not None:
                answers = {**answers, "credential_file": args.credential_file}
            document = SetupService().configure(Path(args.config), answers)
            report = {"status": "configured", "config": str(Path(args.config)), "config_version": document["config_version"]}
            print(json.dumps(report, sort_keys=True))
            return 0
        except (SetupError, CredentialSourceError) as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        except Exception:
            print(json.dumps({"status": "internal_error"}, sort_keys=True))
            return 5
    if args.command == "run":
        try:
            identity = RunIdentity(**json.loads(Path(args.identity).read_text(encoding="utf-8")))
            controller = RunController(Path(args.run_root))
            if args.resume:
                remaining = controller.resume(Path(args.resume), identity, stages=("g1d", "g1r", "builder", "g2a"))
                report = {"status": "resumed", "remaining": remaining}
            else:
                if not args.goal:
                    raise ValueError("goal_required")
                expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
                actual = json.loads(Path(args.actual).read_text(encoding="utf-8"))
                report = controller.start(identity, actual, expected=expected, live=args.live, live_opt_in=args.allow_live, policy_opt_in=args.policy_allow_live)
                report.update({"verdict": "dry_run_complete" if not args.live else "awaiting_approval", "usage": "unreported", "proposal": None})
            print(json.dumps(report, sort_keys=True))
            return 0
        except ValueError as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        except Exception:
            print(json.dumps({"status": "internal_error"}, sort_keys=True))
            return 5
    if args.command == "evidence":
        result = verify_receipt_store(Path(args.run_root))
        print(json.dumps({"status": result.status, "finding": result.finding}, sort_keys=True))
        return result.exit_code
    if args.command == "auth":
        try:
            report = _matrix_auth_status(args.credential_file)
        except CredentialSourceError as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        print(json.dumps(report, sort_keys=True))
        return int(report["exit_code"])
    if args.command == "harness":
        try:
            expected_raw = json.loads(Path(args.expected).read_text(encoding="utf-8"))
            actual_raw = json.loads(Path(args.actual).read_text(encoding="utf-8"))
            expected = {str(agent): (str(binding[0]), str(binding[1])) for agent, binding in expected_raw.items()}
            report = inspect_harness(expected, actual_raw)
        except Exception:
            report = {"ok": False, "status": "internal_error", "agents": {}}
            print(json.dumps(report, sort_keys=True))
            return 5
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 3
    if args.command == "status" and args.require_effective and not args.offline:
        try:
            if not args.runtime:
                raise ValueError("runtime_attestation_required")
            config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
            lanes = json.loads(Path(args.runtime).read_text(encoding="utf-8"))
            report = effective_status(config, lanes)
            print(json.dumps(report, sort_keys=True))
            return int(report["exit_code"])
        except ValueError as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        except Exception:
            print(json.dumps({"status": "internal_error"}, sort_keys=True))
            return 5
    if args.command == "config":
        try:
            if args.config_command == "import-v5-console":
                envelope = import_v5_console.import_v5_console_path(args.config)
            else:
                envelope = import_v5_config.import_v5_path(args.config)
        except Exception:
            envelope = import_boundary.internal_error()
        rendering_failed = _print_envelope(envelope, compact=True)
        if rendering_failed:
            return 5
        if envelope.status == "internal_error":
            return 5
        return exit_code_for(envelope.status, False, envelope.findings)
    command = "profile_validate" if args.command == "profile" else "status_offline"
    try:
        envelope = resolve_path(command, args.config, getattr(args, "require_effective", False))
    except Exception:
        from torq_cli.domain.findings import FindingCatalog
        from torq_cli.domain.models import ResultEnvelope
        envelope = ResultEnvelope("1.0.0", command, "internal_error", None, (FindingCatalog.make("internal_error", path="/"),), {})
        _print_envelope(envelope, compact=False)
        return 5
    rendering_failed = _print_envelope(envelope, compact=False)
    if rendering_failed:
        return 5
    return exit_code_for(envelope.status, getattr(args, "require_effective", False), envelope.findings)
