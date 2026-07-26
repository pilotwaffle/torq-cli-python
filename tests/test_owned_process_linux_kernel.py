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

from torq_cli.adapters.process import OwnedProcess


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


def _wait_ready(owner: OwnedProcess) -> int:
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
                return int(row["pid"])
    raise AssertionError("provider_did_not_become_ready")


def _stop_cycle(tmp_path: Path, role: str = "parent") -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with OwnedProcess(_command(role), cwd=str(tmp_path), env=_environment()) as owner:
        _wait_ready(owner)
        observation = owner.force_stop(timeout=10)
        assert observation.confirmed
        assert observation.active_processes == 0
        assert observation.containment_state.value == "known_empty"


@pytest.mark.parametrize("role", ("setsid-escape", "double-fork-escape"))
def test_session_and_double_fork_cannot_escape_owned_cgroup(tmp_path: Path, role: str) -> None:
    _stop_cycle(tmp_path, role)


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
    pids = json.loads(metadata.read_text(encoding="utf-8"))["pids"]
    while any(Path(f"/proc/{pid}").exists() for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
