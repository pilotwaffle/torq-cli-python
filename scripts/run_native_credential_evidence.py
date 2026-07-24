"""T-35 clean-machine installed-wheel native credential evidence runner."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import uuid
from pathlib import Path

from torq_cli.application.credential_evidence import exercise_native_credential
from torq_cli.connectors.native_credentials import native_store_for_current_platform


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-os", choices=("macOS", "Linux"), required=True)
    parser.add_argument("--expected-backend", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()

    observed_os = platform.system()
    expected_platform = {"macOS": "Darwin", "Linux": "Linux"}[args.expected_os]
    if observed_os != expected_platform:
        raise RuntimeError("clean_machine_os_mismatch")
    observed_hash = _sha256(args.wheel)
    if observed_hash != args.wheel_sha256.upper():
        raise RuntimeError("wheel_hash_mismatch")

    store = native_store_for_current_platform()
    if store.backend != args.expected_backend:
        raise RuntimeError("native_backend_mismatch")
    result = exercise_native_credential(
        store,
        provider="deepseek",
        credential_ref="credref_" + uuid.uuid4().hex,
    )
    report = {
        "schema": "torq-native-credential-evidence-v1",
        "date": args.date,
        "provenance": {
            "kind": "machine_generated_clean_hosted_runner",
            "machine_generated": True,
            "installed_wheel": True,
        },
        "host": {
            "os": args.expected_os,
            "platform_release": platform.release(),
            "python": platform.python_version(),
            "runner_os": os.environ.get("RUNNER_OS", "unreported"),
            "image_os": os.environ.get("ImageOS", "unreported"),
            "image_version": os.environ.get("ImageVersion", "unreported"),
        },
        "source": {
            "commit": os.environ.get("GITHUB_SHA", "unreported"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "unreported"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "unreported"),
        },
        "artifact": {
            "package": "torq-cli",
            "version": importlib.metadata.version("torq-cli"),
            "wheel_sha256": observed_hash,
        },
        "credential_round_trip": result,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "backend": result["backend"],
        "report": str(args.report),
        "status": "passed",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
