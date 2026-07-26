from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

from torq_cli.adapters import process as process_module
from torq_cli.adapters.process import OwnedProcess


_FAKE_PROVIDER = Path(__file__).parent / "fixtures" / "fake_owned_provider.py"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _python_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source = str(Path("src").resolve())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
    return environment


def _one_force_stop_cycle(tmp_path: Path) -> None:
    command = (sys.executable, str(_FAKE_PROVIDER), "--role", "parent")
    observed_pids: set[int] = set()
    sequences: list[int] = []
    with OwnedProcess(command, cwd=str(tmp_path), env=_python_environment()) as owned:
        deadline = time.monotonic() + 10
        while len(observed_pids) < 3 and time.monotonic() < deadline:
            event = owned.next_event(timeout=0.25)
            if event is None:
                continue
            sequences.append(event.sequence)
            if event.channel != "stdout":
                continue
            payload = json.loads(event.data)
            if payload.get("kind") == "ready":
                observed_pids.add(int(payload["pid"]))
        assert len(observed_pids) == 3
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

        observation = owned.force_stop(timeout=5)
        assert observation.returncode is not None
        if observation.active_processes == 0:
            assert observation.confirmed
        else:
            # POSIX may deny a group-existence probe after the leader exits.
            # Preserve the honest uncertain observation; the PID leak oracle
            # below independently verifies this deterministic process tree.
            assert os.name != "nt"
            assert observation.active_processes is None
            assert not observation.confirmed

    deadline = time.monotonic() + 5
    # QueryInformationJobObject(active=0) is the authoritative Windows tree
    # oracle. PID-only probes are vulnerable to immediate PID reuse and Python
    # 3.11's os.kill(pid, 0) can raise SystemError on an exited Windows PID.
    survivors = set() if os.name == "nt" else set(observed_pids)
    while survivors and time.monotonic() < deadline:
        survivors = {pid for pid in survivors if _pid_exists(pid)}
        if survivors:
            time.sleep(0.02)
    assert not survivors


@pytest.mark.parametrize("cycle", range(20))
def test_twenty_streaming_force_stop_cycles_leave_no_survivors(
    tmp_path: Path, cycle: int
) -> None:
    del cycle
    _one_force_stop_cycle(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object handshake")
def test_provider_cannot_start_before_job_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "provider-started"

    def refuse_assignment(
        self: process_module._WindowsJob, process: object
    ) -> None:
        del self, process
        raise OSError("assignment_refused")

    monkeypatch.setattr(process_module._WindowsJob, "assign", refuse_assignment)
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(OSError, match="assignment_refused"):
        OwnedProcess(command, cwd=str(tmp_path), env=_python_environment())
    assert not marker.exists()
