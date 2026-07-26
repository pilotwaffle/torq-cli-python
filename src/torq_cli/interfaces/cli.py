"""Argparse interface for the Foundation commands."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from torq_cli import __version__
from torq_cli.application import import_v5_config, import_v5_console
from torq_cli.application.fleet import FleetProjector
from torq_cli.application.chat_projection import reduce_chat_projection
from torq_cli.application.chat_runtime import ChatRuntimeCoordinator
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.application.live_runtime import build_live_runtime
from torq_cli.application.resolve import envelope_to_dict, resolve_path
from torq_cli.application.setup import SetupError, SetupService
from torq_cli.application.status_effective import effective_status
from torq_cli.connectors.status import inspect_harness
from torq_cli.connectors.credential_sources import CredentialSourceError, ExplicitEnvVault
from torq_cli.connectors.native_credentials import (
    NativeCredentialError,
    NativeCredentialStore,
    native_store_for_current_platform,
)
from torq_cli.connectors.headless_credentials import (
    HeadlessCredentialError,
    HeadlessEncryptedFileStore,
)
from torq_cli.adapters.chat_provider import ChatProviderCommandFactory, current_environment
from torq_cli.adapters.linux_containment import linux_containment_capability
from torq_cli.domain.credential_backend import BackendUnavailable
from torq_cli.domain.models import ResultEnvelope
from torq_cli.domain.provider_matrix import PROVIDERS, load_provider_matrix
from torq_cli.interfaces.fleet_http import create_fleet_server
from torq_cli.safety.chat_evidence import ChatEvidenceJournal, verify_chat_evidence
from torq_cli.safety.receipts import FileRunKeyStore, verify_receipt_store


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
            "findings": [
                {
                    "id": "internal_error",
                    "message": "Internal failure occurred without exposing details.",
                    "severity": "critical",
                    "bucket": "A",
                    "status_class": "internal_error",
                    "stage": "complete",
                    "path": "/",
                    "context": {},
                }
            ],
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
    for action in ("store", "verify-access", "revoke"):
        native = auth_sub.add_parser(action)
        native.add_argument("--provider", required=True)
        native.add_argument("--credential-ref", required=True)
        native.add_argument(
            "--backend", choices=("platform_keychain", "headless_encrypted_file"),
            default="platform_keychain",
        )
        native.add_argument("--store-root")
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
    run.add_argument("--config")
    run.add_argument("--live", action="store_true")
    run.add_argument("--allow-live", action="store_true")
    run.add_argument("--policy-allow-live", action="store_true")
    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    verify = evidence_sub.add_parser("verify")
    verify.add_argument("--run-root", required=True)
    verify.add_argument("--trusted-public-key")
    fleet = sub.add_parser("fleet")
    fleet.add_argument("--run-root", required=True)
    fleet.add_argument("--trusted-public-key")
    fleet.add_argument("--serve", action="store_true")
    fleet.add_argument("--host", default="127.0.0.1")
    fleet.add_argument("--port", type=int, default=8765)
    fleet.add_argument(
        "--chat-provider",
        choices=("claude", "deepseek", "kimi", "qwen", "zai"),
    )
    fleet.add_argument("--chat-model")
    fleet.add_argument("--credential-file")
    fleet.add_argument("--claude-bin", default="claude")
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
        else:
            # The packaged matrix is dated decision evidence, not proof that
            # this invocation currently holds credentials or a live grant.
            auth = "unavailable"
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


def _read_attended_secret() -> str:
    if not sys.stdin.isatty():
        raise NativeCredentialError("attended_secret_input_required")
    try:
        return getpass.getpass("Credential: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise NativeCredentialError("attended_secret_input_required") from exc


def _read_trusted_public_key(path: str | None) -> bytes | None:
    if path is None:
        return None
    encoded = Path(path).read_text(encoding="ascii").strip()
    trusted_public_key = bytes.fromhex(encoded)
    if len(trusted_public_key) != 32:
        raise ValueError("trusted_public_key_invalid")
    return trusted_public_key


def main(argv: Sequence[str] | None = None) -> int:
    supplied = list(argv) if argv is not None else sys.argv[1:]
    import_boundary = (
        import_v5_console if supplied[:2] == ["config", "import-v5-console"] else import_v5_config
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
            report = {
                "status": "configured",
                "config": str(Path(args.config)),
                "config_version": document["config_version"],
            }
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
            try:
                identity_value = json.loads(Path(args.identity).read_text(encoding="utf-8"))
                if not isinstance(identity_value, dict):
                    raise ValueError
                identity = RunIdentity(**identity_value)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("run_identity_invalid") from exc
            if not args.resume and not args.goal:
                raise ValueError("goal_required")
            if args.live and not args.resume and not (args.allow_live and args.policy_allow_live):
                raise ValueError("double_opt_in_required")
            expected = None
            actual = None
            if not args.resume:
                try:
                    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
                    actual = json.loads(Path(args.actual).read_text(encoding="utf-8"))
                    if not isinstance(expected, dict) or not isinstance(actual, dict):
                        raise ValueError
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    raise ValueError("run_attestation_input_invalid") from exc
                for field, expected_value in expected.items():
                    observed = actual.get(field)
                    if observed is None:
                        raise ValueError(f"attestation_unattestable:{field}")
                    if observed != expected_value:
                        raise ValueError(f"attestation_mismatch:{field}")
            live_runtime = None
            if args.live and not args.resume:
                if args.config is None:
                    raise ValueError("live_config_required")
                live_runtime = build_live_runtime(
                    Path(args.config).resolve(),
                    Path(args.run_root).resolve(),
                    expected_config_version=identity.config_version,
                    expected_profile_version=identity.profile_version,
                    expected_policy_version=identity.policy_version,
                )
            controller = RunController(
                Path(args.run_root),
                None if live_runtime is None else live_runtime.orchestrator,
            )
            if args.resume:
                remaining = controller.resume(
                    Path(args.resume), identity, stages=("g1d", "g1r", "builder", "g2a")
                )
                report = {"status": "resumed", "remaining": remaining}
            else:
                assert expected is not None and actual is not None
                report = controller.start(
                    identity,
                    actual,
                    expected=expected,
                    live=args.live,
                    live_opt_in=args.allow_live,
                    policy_opt_in=args.policy_allow_live,
                    goal=args.goal,
                    profile=None if live_runtime is None else live_runtime.profile,
                )
            print(json.dumps(report, sort_keys=True))
            return 0
        except (ValueError, BackendUnavailable, NativeCredentialError) as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        except Exception:
            print(json.dumps({"status": "internal_error"}, sort_keys=True))
            return 5
    if args.command in {"evidence", "fleet"}:
        try:
            trusted_public_key = _read_trusted_public_key(args.trusted_public_key)
        except (OSError, UnicodeError, ValueError):
            print(
                json.dumps(
                    {
                        "status": "tampered",
                        "finding": "trusted_public_key_invalid",
                    },
                    sort_keys=True,
                )
            )
            return 3
    if args.command == "fleet":
        projector = FleetProjector(
            Path(args.run_root),
            trusted_public_key=trusted_public_key,
        )
        if args.serve:
            try:
                chat_controller = None
                chat_snapshot_provider = None
                if args.chat_provider is not None:
                    if args.chat_model is None:
                        raise ValueError("chat_model_required")
                    if sys.platform.startswith("linux"):
                        capability = linux_containment_capability()
                        if not capability.available:
                            raise ValueError(capability.reason)
                    elif sys.platform != "win32":
                        raise ValueError("chat_strong_containment_unavailable")
                    vault = None
                    if args.chat_provider != "claude":
                        if args.credential_file is None:
                            raise ValueError("chat_credential_source_required")
                        vault = ExplicitEnvVault(Path(args.credential_file).resolve())
                    run_root = Path(args.run_root).resolve()
                    keys = FileRunKeyStore(run_root.parent).get_or_create_run_keys(run_root.name)
                    journal = ChatEvidenceJournal(run_root, keys.operator_gateway)
                    command_factory = ChatProviderCommandFactory(
                        provider=args.chat_provider,
                        model=args.chat_model,
                        cwd=run_root,
                        base_environment=current_environment(),
                        vault=vault,
                        claude_binary=args.claude_bin,
                    )
                    chat_controller = ChatRuntimeCoordinator(journal, command_factory)
                    chat_controller.recover_incomplete()
                    chat_projection_cache: dict[str, Any] = {}

                    def chat_snapshot_provider() -> Mapping[str, Any]:
                        try:
                            head_path = (
                                run_root.parent
                                / ".torq-chat-heads"
                                / run_root.name
                                / "head.v1.json"
                            )
                            journal_stat = journal.path.stat() if journal.path.exists() else None
                            head_stat = head_path.stat() if head_path.exists() else None
                            cache_key = (
                                0 if journal_stat is None else journal_stat.st_mtime_ns,
                                0 if journal_stat is None else journal_stat.st_size,
                                0 if head_stat is None else head_stat.st_mtime_ns,
                                0 if head_stat is None else head_stat.st_size,
                            )
                            if chat_projection_cache.get("key") == cache_key:
                                cached = chat_projection_cache.get("value")
                                if isinstance(cached, Mapping):
                                    return cached
                            rows = verify_chat_evidence(run_root)
                            projected = reduce_chat_projection(
                                rows,
                                verification_state="verified",
                            )
                            chat_projection_cache.update({"key": cache_key, "value": projected})
                            return projected
                        except (OSError, UnicodeError, ValueError) as exc:
                            return reduce_chat_projection(
                                (),
                                verification_state="tampered",
                                verification_finding=str(exc),
                            )

                server = create_fleet_server(
                    projector,
                    host=args.host,
                    port=args.port,
                    chat_controller=chat_controller,
                    chat_snapshot_provider=chat_snapshot_provider,
                )
            except (OSError, ValueError) as exc:
                print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
                return 3
            host, port = server.server_address[:2]
            host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
            bootstrap_nonce = str(getattr(server, "fleet_bootstrap_nonce"))
            print(
                json.dumps(
                    {
                        "status": "serving",
                        "url": (f"http://{host_text}:{port}/bootstrap?nonce={bootstrap_nonce}"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    if chat_controller is not None:
                        chat_controller.shutdown(timeout=5.0)
                finally:
                    server.server_close()
            return 0
        snapshot = projector.snapshot()
        print(json.dumps(snapshot, sort_keys=True))
        return {
            "sealed_verified": 0,
            "live_verified": 0,
            "live_catching_up": 0,
            "tampered": 3,
            "incomplete": 4,
            "unreadable": 4,
        }[str(snapshot["verification"]["state"])]
    if args.command == "evidence":
        result = verify_receipt_store(Path(args.run_root), trusted_public_key=trusted_public_key)
        print(json.dumps({"status": result.status, "finding": result.finding}, sort_keys=True))
        return result.exit_code
    if args.command == "auth":
        try:
            if args.auth_command == "status":
                report = _matrix_auth_status(args.credential_file)
                print(json.dumps(report, sort_keys=True))
                return int(report["exit_code"])
            store: HeadlessEncryptedFileStore | NativeCredentialStore
            if args.backend == "headless_encrypted_file":
                if not isinstance(args.store_root, str):
                    raise HeadlessCredentialError("credential_store_root_required")
                store_root = Path(args.store_root)
                if not store_root.is_absolute():
                    raise HeadlessCredentialError("credential_store_absolute_required")
                store = HeadlessEncryptedFileStore(store_root)
            else:
                if args.store_root is not None:
                    raise NativeCredentialError("credential_backend_selection_invalid")
                store = native_store_for_current_platform()
            if args.auth_command == "store":
                secret = _read_attended_secret()
                try:
                    store.store(args.provider, args.credential_ref, secret)
                finally:
                    secret = ""
                report = {"status": "stored", "backend": store.backend}
                code = 0
            elif args.auth_command == "verify-access":
                present = store.contains(args.provider, args.credential_ref)
                present_status = (
                    "present"
                    if store.backend == "headless_encrypted_file"
                    else "access_verified"
                )
                report = {
                    "status": present_status if present else "absent",
                    "backend": store.backend,
                }
                code = 0 if present else 3
            else:
                revoked = store.revoke(args.provider, args.credential_ref)
                report = {
                    "status": "revoked" if revoked else "absent",
                    "backend": store.backend,
                }
                code = 0 if revoked else 3
        except (
            BackendUnavailable, CredentialSourceError, HeadlessCredentialError,
            NativeCredentialError,
        ) as exc:
            print(json.dumps({"status": "blocked", "finding": str(exc)}, sort_keys=True))
            return 3
        print(json.dumps(report, sort_keys=True))
        return code
    if args.command == "harness":
        try:
            expected_raw = json.loads(Path(args.expected).read_text(encoding="utf-8"))
            actual_raw = json.loads(Path(args.actual).read_text(encoding="utf-8"))
            expected = {
                str(agent): (str(binding[0]), str(binding[1]))
                for agent, binding in expected_raw.items()
            }
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

        envelope = ResultEnvelope(
            "1.0.0",
            command,
            "internal_error",
            None,
            (FindingCatalog.make("internal_error", path="/"),),
            {},
        )
        _print_envelope(envelope, compact=False)
        return 5
    rendering_failed = _print_envelope(envelope, compact=False)
    if rendering_failed:
        return 5
    return exit_code_for(
        envelope.status, getattr(args, "require_effective", False), envelope.findings
    )
