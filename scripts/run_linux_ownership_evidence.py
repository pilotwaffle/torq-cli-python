"""Run the fail-closed experimental Linux user-systemd behavior gate.

This driver is intentionally Linux-host specific.  It records why a host was
accepted or refused, runs opt-in experimental tests only after a real systemd
user manager and unified cgroup hierarchy have been observed, and rejects a
successful pytest exit if any selected test was skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree


_EXPECTED_TESTS = 8
_SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_TEST_TARGETS = (
    "tests/test_owned_process_linux_kernel.py",
    "tests/test_chat_end_to_end.py",
)
_CHILD_ENV_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PYTHONIOENCODING",
        "TMPDIR",
        "USER",
        "XDG_RUNTIME_DIR",
    }
)


class PrerequisiteError(RuntimeError):
    """A required host ownership primitive was not observed."""


def _run(command: tuple[str, ...], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PrerequisiteError("linux_ownership_control_timeout") from exc


def _trusted_tool(name: str) -> str:
    candidate = shutil.which(name, path=_SYSTEM_PATH)
    if candidate is None:
        raise PrerequisiteError(f"linux_ownership_{name}_missing")
    resolved = Path(candidate).resolve(strict=True)
    roots = tuple(Path(value).resolve() for value in _SYSTEM_PATH.split(":"))
    if not resolved.is_file() or not os.access(resolved, os.X_OK) or resolved.parent not in roots:
        raise PrerequisiteError(f"linux_ownership_{name}_untrusted")
    return str(resolved)


def _properties(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _observe_prerequisites(bootstrap_status: int) -> dict[str, object]:
    if bootstrap_status != 0:
        raise PrerequisiteError("linux_ownership_user_manager_bootstrap_failed")
    if not sys.platform.startswith("linux"):
        raise PrerequisiteError("linux_ownership_linux_host_required")
    if Path("/proc/1/comm").read_text(encoding="ascii").strip() != "systemd":
        raise PrerequisiteError("linux_ownership_systemd_pid1_required")

    stat_tool = _trusted_tool("stat")
    filesystem = _run(
        (stat_tool, "--file-system", "--format=%T", "/sys/fs/cgroup")
    )
    if filesystem.returncode != 0 or filesystem.stdout.strip() != "cgroup2fs":
        raise PrerequisiteError("linux_ownership_cgroup_v2_required")

    uid = os.getuid()
    runtime_dir = Path(f"/run/user/{uid}")
    configured_runtime = os.environ.get("XDG_RUNTIME_DIR")
    expected_bus = f"unix:path={runtime_dir}/bus"
    if configured_runtime != str(runtime_dir):
        raise PrerequisiteError("linux_ownership_runtime_directory_invalid")
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS") != expected_bus:
        raise PrerequisiteError("linux_ownership_bus_address_invalid")
    runtime_stat = runtime_dir.stat()
    if runtime_stat.st_uid != uid or stat.S_IMODE(runtime_stat.st_mode) & 0o077:
        raise PrerequisiteError("linux_ownership_runtime_directory_unsafe")
    bus = runtime_dir / "bus"
    bus_stat = bus.stat()
    if bus_stat.st_uid != uid or not stat.S_ISSOCK(bus_stat.st_mode):
        raise PrerequisiteError("linux_ownership_user_bus_unavailable")

    systemctl = _trusted_tool("systemctl")
    manager = _run(
        (
            systemctl,
            "--user",
            "show",
            "--property=Version,ControlGroup",
        )
    )
    if manager.returncode != 0:
        raise PrerequisiteError("linux_ownership_user_manager_unavailable")
    manager_values = _properties(manager.stdout)
    control_group = manager_values.get("ControlGroup", "")
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        raise PrerequisiteError("linux_ownership_user_manager_cgroup_invalid")
    cgroup_root = Path("/sys/fs/cgroup").resolve()
    cgroup_path = (cgroup_root / control_group.lstrip("/")).resolve()
    if cgroup_root not in cgroup_path.parents or not (cgroup_path / "cgroup.events").is_file():
        raise PrerequisiteError("linux_ownership_user_manager_cgroup_unobservable")

    systemd_run = _trusted_tool("systemd-run")
    true_tool = _trusted_tool("true")
    preflight_unit = f"torq-ci-preflight-{os.getpid()}.service"
    inaccessible_control_plane = " ".join(
        (
            "/proc",
            "/run/dbus/system_bus_socket",
            "/run/systemd/private",
            str(runtime_dir / "bus"),
            str(runtime_dir / "systemd"),
        )
    )
    preflight = _run(
        (
            systemd_run,
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--service-type=exec",
            f"--unit={preflight_unit}",
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=1s",
            "--property=ProtectControlGroups=yes",
            f"--property=InaccessiblePaths={inaccessible_control_plane}",
            "--property=RestrictAddressFamilies=AF_INET AF_INET6",
            "--",
            true_tool,
        )
    )
    if preflight.returncode != 0:
        raise PrerequisiteError("linux_ownership_transient_service_preflight_failed")

    return {
        "cgroup_filesystem": "cgroup2fs",
        "manager_control_group": control_group,
        "manager_version": manager_values.get("Version", "unreported"),
        "pid1": "systemd",
        "preflight_unit": preflight_unit,
        "user_bus": str(bus),
        "user_id": uid,
    }


def _junit_summary(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        name: sum(int(suite.attrib.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, allow_nan=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_digest(path: Path) -> None:
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_path.write_text(f"{_sha256(path)}  {path.name}\n", encoding="ascii")


def _provenance() -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "host": {
            "architecture": platform.machine(),
            "image_os": os.environ.get("ImageOS", "unreported"),
            "image_version": os.environ.get("ImageVersion", "unreported"),
            "kernel": platform.release(),
            "runner_os": os.environ.get("RUNNER_OS", "unreported"),
        },
        "kind": "machine_generated_linux_systemd_experimental_evidence",
        "source": {
            "event_name": os.environ.get("GITHUB_EVENT_NAME", "unreported"),
            "event_sha": os.environ.get("GITHUB_SHA", "unreported"),
            "head_sha": os.environ.get("TORQ_EVIDENCE_HEAD_SHA", "unreported"),
            "ref": os.environ.get("GITHUB_REF", "unreported"),
            "repository": os.environ.get("GITHUB_REPOSITORY", "unreported"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "unreported"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "unreported"),
            "workflow": os.environ.get("GITHUB_WORKFLOW", "unreported"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-status", required=True, type=int)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    report: dict[str, object] = {
        "provenance": _provenance(),
        "schema": "torq-linux-systemd-experimental-evidence-v1",
        "status": "refused",
    }
    result = 3
    try:
        report["prerequisites"] = _observe_prerequisites(args.bootstrap_status)
        child_environment = {
            key: value for key, value in os.environ.items() if key in _CHILD_ENV_KEYS
        }
        child_environment.update(
            {
                "TORQ_TEST_LINUX_SYSTEMD_CGROUP": "1",
                "TORQ_TEST_NETWORK_MODE": "deny",
            }
        )
        command = (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *_TEST_TARGETS,
            f"--junitxml={args.junit}",
        )
        completed = subprocess.run(command, check=False, env=child_environment)
        if not args.junit.is_file():
            raise RuntimeError("linux_ownership_junit_missing")
        summary = _junit_summary(args.junit)
        report["tests"] = {"command": list(command), **summary}
        report["artifacts"] = {"junit_sha256": _sha256(args.junit)}
        if completed.returncode != 0 or summary["failures"] or summary["errors"]:
            report["status"] = "tests_failed"
            result = completed.returncode or 1
        elif summary["skipped"]:
            raise RuntimeError("linux_ownership_test_skipped")
        elif summary["tests"] != _EXPECTED_TESTS:
            raise RuntimeError("linux_ownership_test_inventory_changed")
        else:
            report["status"] = "passed"
            result = 0
    except (OSError, PrerequisiteError, RuntimeError, ValueError) as exc:
        report["finding"] = str(exc)
    finally:
        _write_report(args.report, report)
        _write_digest(args.report)

    print(json.dumps({"report": str(args.report), "status": report["status"]}, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
