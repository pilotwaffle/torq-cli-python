"""Opt-in kernel evidence for Linux systemd+cgroup-v2 ownership.

Run on a real login session with:
TORQ_TEST_LINUX_SYSTEMD_CGROUP=1 pytest tests/test_owned_process_linux_kernel.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from torq_cli.adapters.process import ExperimentalLinuxOwnedProcess

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or os.environ.get("TORQ_TEST_LINUX_SYSTEMD_CGROUP") != "1",
    reason="requires an explicit real systemd user-session cgroup-v2 runner",
)

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_owned_provider.py"
_CRASH_FIXTURE = Path(__file__).parent / "fixtures" / "linux_owner_crash.py"


def _command(role: str) -> tuple[str, ...]:
    return (sys.executable, str(_FIXTURE), "--role", role)


def _environment() -> dict[str, str]:
    return dict(os.environ)


def _wait_ready(owner: ExperimentalLinuxOwnedProcess) -> dict[str, object]:
    buffered = b""
    for _ in range(100):
        event = owner.next_event(timeout=0.1)
        if event is None or event.channel != "stdout":
            continue
        buffered += event.data
        lines = buffered.split(b"\n")
        buffered = lines.pop()
        for line in lines:
            row = json.loads(line)
            if row.get("kind") == "ready":
                return row
    raise AssertionError("provider_did_not_become_ready")


def _stop_cycle(tmp_path: Path, role: str = "parent") -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with ExperimentalLinuxOwnedProcess(
        _command(role), cwd=str(tmp_path), env=_environment()
    ) as owner:
        _wait_ready(owner)
        observation = owner.force_stop(timeout=10)
        assert observation.confirmed
        assert observation.active_processes == 0
        assert observation.containment_state.value == "known_empty"


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("/usr/bin/systemctl", "--user", *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("role", ("setsid-escape", "double-fork-escape"))
def test_session_and_double_fork_cannot_escape_owned_cgroup(tmp_path: Path, role: str) -> None:
    _stop_cycle(tmp_path, role)


def test_provider_cannot_delegate_escape_to_user_manager(tmp_path: Path) -> None:
    for role in ("systemd-user-escape", "systemd-machine-escape"):
        escape_unit = ""
        try:
            with ExperimentalLinuxOwnedProcess(
                _command(role), cwd=str(tmp_path), env=_environment()
            ) as owner:
                ready = _wait_ready(owner)
                escape_unit = str(ready["escape_unit"])
                assert int(ready["escape_returncode"]) != 0
                assert _systemctl("show", escape_unit).returncode != 0
                observation = owner.force_stop(timeout=10)
                assert observation.confirmed
                assert observation.active_processes == 0
        finally:
            if escape_unit:
                _systemctl("stop", escape_unit)
                _systemctl("reset-failed", escape_unit)


def test_one_hundred_sequential_stops_leave_cgroup_empty(tmp_path: Path) -> None:
    for _ in range(100):
        _stop_cycle(tmp_path)


def test_twenty_concurrent_stops_leave_cgroups_empty(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_stop_cycle, tmp_path / str(index)) for index in range(20)]
        for future in futures:
            future.result(timeout=30)


def test_coordinator_crash_lease_leaves_no_provider_survivors(tmp_path: Path) -> None:
    metadata = tmp_path / "crash.json"
    coordinator = subprocess.Popen(
        (sys.executable, str(_CRASH_FIXTURE), str(metadata), str(_FIXTURE)),
        cwd=tmp_path,
        env=_environment(),
    )
    deadline = time.monotonic() + 15
    while not metadata.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert metadata.exists()
    assert coordinator.wait(timeout=10) == 91
    recorded = json.loads(metadata.read_text(encoding="utf-8"))
    try:
        pids = recorded["pids"]
        assert len(pids) == 4
        assert len(set(pids)) == 4
        assert recorded["lease_opened"] == 0
        assert recorded["unit"].startswith("torq-chat-")
        assert recorded["unit"].endswith(".service")
        cgroup_root = Path("/sys/fs/cgroup").resolve()
        cgroup_path = (cgroup_root / recorded["control_group"].lstrip("/")).resolve()
        assert cgroup_root in cgroup_path.parents
        deadline = time.monotonic() + 15
        populated: int | None = None
        while time.monotonic() < deadline:
            try:
                events = {
                    key: int(value)
                    for key, value in (
                        line.split()
                        for line in (cgroup_path / "cgroup.events").read_text(
                            encoding="ascii"
                        ).splitlines()
                    )
                }
                populated = events.get("populated")
            except FileNotFoundError:
                populated = 0
            if populated == 0 and all(not Path(f"/proc/{pid}").exists() for pid in pids):
                break
            time.sleep(0.02)
        assert populated == 0
        assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
    finally:
        unit = str(recorded.get("unit", ""))
        if unit:
            _systemctl("stop", unit)
            _systemctl("reset-failed", unit)
