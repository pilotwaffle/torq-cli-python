"""Linux cgroup-v2 ownership through a transient systemd service.

The provider is exec'd by the user service manager, which places it in the
unit cgroup before exec.  A plain delegated cgroup plus a post-fork migration
is deliberately not used: it leaves either a pre-assignment execution window
or a same-user migration escape.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path


_BOOTSTRAP_LIMIT = 4_000_000
_TRUSTED_SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_LAUNCHER_ENV_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SYSTEMD_EXEC_PID",
        "USER",
        "XDG_RUNTIME_DIR",
    }
)


class LinuxSystemdCgroup:
    """Own one provider tree in a systemd-managed cgroup-v2 service."""

    unit: str
    _control_group: Path | None
    _leader_pid: int | None
    _started: bool
    _environment: dict[str, str]
    _systemd_run: str
    _systemctl: str
    _stat: str

    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise OSError("owned_process_strong_containment_unavailable")
        self._systemd_run = _trusted_system_tool("systemd-run")
        self._systemctl = _trusted_system_tool("systemctl")
        self._stat = _trusted_system_tool("stat")
        if _filesystem_type(Path("/sys/fs/cgroup"), self._stat) != "cgroup2fs":
            raise OSError("owned_process_cgroup_v2_required")
        self.unit = f"torq-chat-{uuid.uuid4().hex}.service"
        self._control_group: Path | None = None
        self._leader_pid: int | None = None
        self._started = False
        self._environment = {
            key: value for key, value in os.environ.items() if key in _LAUNCHER_ENV_KEYS
        }

    @property
    def leader_pid(self) -> int | None:
        """Return systemd's provider MainPID after startup confirmation."""
        return self._leader_pid

    def launch_command(self, command: Sequence[str], *, cwd: str) -> tuple[str, ...]:
        """Build the transient-service command without embedding credentials."""
        return (
            self._systemd_run,
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--service-type=exec",
            f"--unit={self.unit}",
            f"--working-directory={cwd}",
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=1s",
            "--property=ProtectControlGroups=yes",
            "--",
            sys.executable,
            "-m",
            "torq_cli.adapters.linux_cgroup_exec",
            "--",
            *command,
        )

    def launcher_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Return only variables needed to contact the user service manager."""
        self._environment = {
            key: value for key, value in environment.items() if key in _LAUNCHER_ENV_KEYS
        }
        return dict(self._environment)

    @staticmethod
    def framed_input(environment: Mapping[str, str], content: bytes | None) -> bytes:
        """Frame the provider environment on stdin so secrets never enter argv."""
        encoded = json.dumps(
            dict(environment),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _BOOTSTRAP_LIMIT:
            raise ValueError("owned_process_environment_too_large")
        prompt = content or b""
        return (
            len(encoded).to_bytes(4, "big")
            + encoded
            + len(prompt).to_bytes(8, "big")
            + prompt
        )

    def bind_process(self, process: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
        """Confirm systemd created the protected cgroup before returning control."""
        deadline = time.monotonic() + timeout
        last_error = "owned_process_systemd_start_unconfirmed"
        while time.monotonic() < deadline:
            details = self._show()
            if details is not None:
                state, main_pid, control_group = details
                if control_group and main_pid > 0 and state in {"active", "activating"}:
                    path = _cgroup_path(control_group)
                    if not (path / "cgroup.events").is_file():
                        last_error = "owned_process_cgroup_observation_unavailable"
                        break
                    self._control_group = path
                    self._leader_pid = main_pid
                    self._started = True
                    return
                if state in {"failed", "inactive", "deactivating"}:
                    last_error = "owned_process_systemd_start_failed"
                    break
            if process.poll() is not None:
                last_error = "owned_process_systemd_start_failed"
                break
            time.sleep(0.01)
        self._kill_unit()
        raise OSError(last_error)

    def active_processes(self) -> int:
        """Return a kernel-backed population observation for the entire unit tree."""
        if not self._started:
            raise RuntimeError("owned_process_cgroup_not_started")
        path = self._control_group
        if path is None:
            raise RuntimeError("owned_process_cgroup_observation_unavailable")
        try:
            events = _parse_events((path / "cgroup.events").read_text(encoding="ascii"))
        except FileNotFoundError:
            # A cgroup directory cannot be removed while populated.  Confirm
            # the unified hierarchy itself still exists before treating its
            # collected unit directory as a known-empty kernel observation.
            if (
                _filesystem_type(Path("/sys/fs/cgroup"), self._stat) == "cgroup2fs"
                and not path.exists()
            ):
                return 0
            raise OSError("owned_process_cgroup_observation_unavailable") from None
        except (OSError, ValueError):
            raise OSError("owned_process_cgroup_observation_unavailable") from None
        populated = events.get("populated")
        if populated not in {0, 1}:
            raise OSError("owned_process_cgroup_population_invalid")
        return populated

    def terminate(self) -> None:
        """Ask systemd to SIGKILL every member of the owned cgroup."""
        if not self._started:
            return
        result = self._kill_unit()
        if result.returncode != 0 and self.active_processes() != 0:
            raise OSError("owned_process_cgroup_kill_failed")

    def close(self) -> None:
        """Reset the transient unit after its cgroup has drained."""
        if not self._started:
            return
        if self.active_processes() != 0:
            self.terminate()
        subprocess.run(
            (self._systemctl, "--user", "reset-failed", self.unit),
            check=False,
            capture_output=True,
            env=self._environment,
        )

    def _kill_unit(self) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            (
                self._systemctl,
                "--user",
                "kill",
                "--kill-who=all",
                "--signal=KILL",
                self.unit,
            ),
            check=False,
            capture_output=True,
            env=self._environment,
        )

    def _show(self) -> tuple[str, int, str] | None:
        result = subprocess.run(
            (
                self._systemctl,
                "--user",
                "show",
                self.unit,
                "--property=ActiveState,MainPID,ControlGroup",
            ),
            check=False,
            capture_output=True,
            env=self._environment,
            text=True,
        )
        if result.returncode != 0:
            return None
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        try:
            main_pid = int(values["MainPID"])
            state = values["ActiveState"]
            control_group = values["ControlGroup"]
        except (KeyError, ValueError):
            return None
        return state, main_pid, control_group

def _trusted_system_tool(name: str) -> str:
    candidate = shutil.which(name, path=_TRUSTED_SYSTEM_PATH)
    if candidate is None:
        raise OSError("owned_process_systemd_unavailable")
    path = Path(candidate)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise OSError("owned_process_systemd_unavailable") from None
    trusted_roots = tuple(Path(root).resolve() for root in _TRUSTED_SYSTEM_PATH.split(":"))
    if (
        not path.is_absolute()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
        or resolved.parent not in trusted_roots
    ):
        raise OSError("owned_process_systemd_unavailable")
    return str(resolved)


def _filesystem_type(path: Path, stat_binary: str) -> str:
    result = subprocess.run(
        (stat_binary, "--file-system", "--format=%T", str(path)),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _cgroup_path(value: str) -> Path:
    if not value.startswith("/") or ".." in Path(value).parts:
        raise OSError("owned_process_cgroup_path_invalid")
    root = Path("/sys/fs/cgroup").resolve()
    candidate = (root / value.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise OSError("owned_process_cgroup_path_invalid")
    return candidate


def _parse_events(content: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in content.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("owned_process_cgroup_events_invalid")
        result[parts[0]] = int(parts[1])
    return result
