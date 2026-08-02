from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from torq_cli.adapters import process as process_module
from torq_cli.adapters.process import OwnedProcess
from torq_cli.adapters.windows_job import WindowsJob

_FAKE_PROVIDER = Path(__file__).parent / "fixtures" / "fake_owned_provider.py"

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="OwnedProcess requires kernel-backed Windows Job containment"
)


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


def _command(role: str) -> tuple[str, str, str, str]:
    return (sys.executable, str(_FAKE_PROVIDER), "--role", role)


def _collect_ready_pids(owned: OwnedProcess, *, expected: int) -> set[int]:
    observed: set[int] = set()
    buffered = b""
    deadline = time.monotonic() + 10
    while len(observed) < expected and time.monotonic() < deadline:
        event = owned.next_event(timeout=0.25)
        if event is None or event.channel != "stdout":
            continue
        buffered += event.data
        lines = buffered.split(b"\n")
        buffered = lines.pop()
        for line in lines:
            payload = json.loads(line)
            if payload.get("kind") == "ready":
                observed.add(int(payload["pid"]))
    assert len(observed) == expected
    return observed


def _assert_pids_exit(pids: set[int]) -> None:
    deadline = time.monotonic() + 5
    survivors = set(pids)
    while survivors and time.monotonic() < deadline:
        survivors = {pid for pid in survivors if _pid_exists(pid)}
        if survivors:
            time.sleep(0.02)
    assert not survivors


def _one_force_stop_cycle(tmp_path: Path) -> None:
    command = _command("parent")
    observed_pids: set[int] = set()
    sequences: list[int] = []
    buffered = b""
    with OwnedProcess(command, cwd=str(tmp_path), env=_python_environment()) as owned:
        deadline = time.monotonic() + 10
        while len(observed_pids) < 3 and time.monotonic() < deadline:
            event = owned.next_event(timeout=0.25)
            if event is None:
                continue
            sequences.append(event.sequence)
            if event.channel != "stdout":
                continue
            buffered += event.data
            lines = buffered.split(b"\n")
            buffered = lines.pop()
            for line in lines:
                payload = json.loads(line)
                if payload.get("kind") == "ready":
                    observed_pids.add(int(payload["pid"]))
        assert len(observed_pids) == 3
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

        observation = owned.force_stop(timeout=5)
        assert observation.returncode is not None
        if observation.active_processes == 0:
            assert observation.confirmed is (os.name == "nt")
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


def test_force_stop_owns_descendants_after_leader_exits(tmp_path: Path) -> None:
    """Tree ownership must not depend on the original leader remaining alive."""
    with OwnedProcess(
        _command("exiting-parent"),
        cwd=str(tmp_path),
        env=_python_environment(),
    ) as owned:
        observed_pids = _collect_ready_pids(owned, expected=3)
        natural = owned.wait(timeout=5)
        assert natural.returncode == 0

        observation = owned.force_stop(timeout=5)
        assert observation.confirmed is (os.name == "nt")
        assert observation.active_processes == 0

    if os.name != "nt":
        _assert_pids_exit(observed_pids)


def test_flushed_newline_free_output_is_streamed_before_exit(tmp_path: Path) -> None:
    with OwnedProcess(
        _command("partial"),
        cwd=str(tmp_path),
        env=_python_environment(),
    ) as owned:
        event = owned.next_event(timeout=2)
        assert event is not None
        assert event.channel == "stdout"
        assert event.data == b"partial-token"
        assert owned.poll() is None


def test_stdout_stderr_events_are_dequeued_in_observation_order(
    tmp_path: Path,
) -> None:
    with OwnedProcess(
        _command("dual-stream"),
        cwd=str(tmp_path),
        env=_python_environment(),
    ) as owned:
        events = [owned.next_event(timeout=3), owned.next_event(timeout=3)]
        assert all(event is not None for event in events)
        concrete = [event for event in events if event is not None]
        assert {event.channel for event in concrete} == {"stdout", "stderr"}
        assert [event.sequence for event in concrete] == [1, 2]


@pytest.mark.parametrize(("role", "returncode"), (("complete", 0), ("failed", 7)))
def test_natural_completion_is_confirmed_without_claiming_force(
    tmp_path: Path,
    role: str,
    returncode: int,
) -> None:
    with OwnedProcess(
        _command(role),
        cwd=str(tmp_path),
        env=_python_environment(),
    ) as owned:
        natural = owned.wait(timeout=5)
        assert natural.returncode == returncode
        observation = owned.force_stop(timeout=1)
        assert observation.returncode == returncode
        assert observation.confirmed is (os.name == "nt")
        assert observation.active_processes == 0
        assert not observation.forced


def test_repeated_and_concurrent_stop_close_is_idempotent(tmp_path: Path) -> None:
    owned = OwnedProcess(
        _command("parent"),
        cwd=str(tmp_path),
        env=_python_environment(),
    )
    observed_pids = _collect_ready_pids(owned, expected=3)
    barrier = threading.Barrier(5)
    observations: list[object] = []
    errors: list[BaseException] = []

    def stop() -> None:
        barrier.wait()
        try:
            observations.append(owned.force_stop(timeout=5))
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    def close() -> None:
        barrier.wait()
        try:
            owned.close()
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    workers = [
        threading.Thread(target=stop),
        threading.Thread(target=stop),
        threading.Thread(target=close),
        threading.Thread(target=close),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert not errors
    assert observations
    assert all(getattr(item, "confirmed", False) is (os.name == "nt") for item in observations)
    owned.close()
    if os.name != "nt":
        _assert_pids_exit(observed_pids)


def test_uncertain_stop_can_be_retried_to_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = OwnedProcess(_command("parent"), cwd=str(tmp_path), env=_python_environment())
    _collect_ready_pids(owned, expected=3)
    real_containment = owned._containment
    monkeypatch.setattr(
        owned,
        "_containment",
        lambda: (process_module.ContainmentState.UNKNOWN, None),
    )
    first = owned.force_stop(timeout=0.001)
    assert not first.confirmed
    assert first.forced

    monkeypatch.setattr(owned, "_containment", real_containment)
    second = owned.force_stop(timeout=5)
    assert second.confirmed
    assert second.active_processes == 0
    assert second.forced
    assert owned.wait(timeout=1).forced
    owned.close()


def test_wait_after_confirmed_force_stop_preserves_forced_history(tmp_path: Path) -> None:
    with OwnedProcess(_command("parent"), cwd=str(tmp_path), env=_python_environment()) as owned:
        _collect_ready_pids(owned, expected=3)
        stopped = owned.force_stop(timeout=5)
        assert stopped.confirmed
        assert stopped.forced
        waited = owned.wait(timeout=1)
        assert waited.confirmed
        assert waited.forced
        assert waited.returncode == stopped.returncode


def test_close_waits_for_inflight_uncertain_stop_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = OwnedProcess(_command("parent"), cwd=str(tmp_path), env=_python_environment())
    _collect_ready_pids(owned, expected=3)
    real_containment = owned._containment
    monkeypatch.setattr(
        owned,
        "_containment",
        lambda: (process_module.ContainmentState.UNKNOWN, None),
    )
    assert not owned.force_stop(timeout=0.001).confirmed

    entered = threading.Event()
    release = threading.Event()

    def delayed_containment() -> tuple[process_module.ContainmentState, int | None]:
        entered.set()
        assert release.wait(timeout=5)
        return real_containment()

    monkeypatch.setattr(owned, "_containment", delayed_containment)
    errors: list[BaseException] = []

    def retry() -> None:
        try:
            owned.force_stop(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    def close() -> None:
        try:
            owned.close()
        except BaseException as exc:
            errors.append(exc)

    retry_thread = threading.Thread(target=retry)
    close_thread = threading.Thread(target=close)
    retry_thread.start()
    assert entered.wait(timeout=5)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    release.set()
    retry_thread.join(timeout=5)
    close_thread.join(timeout=5)
    assert not errors
    assert not retry_thread.is_alive()
    assert not close_thread.is_alive()


def test_close_uses_kill_on_close_when_explicit_termination_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = OwnedProcess(_command("parent"), cwd=str(tmp_path), env=_python_environment())
    _collect_ready_pids(owned, expected=3)
    real_close = owned._job.close
    job_close_called = False

    def fail_terminate(exit_code: int = 1) -> None:
        del exit_code
        raise OSError("terminate_injected")

    def tracked_close() -> None:
        nonlocal job_close_called
        job_close_called = True
        real_close()

    monkeypatch.setattr(owned._job, "terminate", fail_terminate)
    monkeypatch.setattr(owned._job, "close", tracked_close)
    with pytest.raises(OSError, match="terminate_injected"):
        owned.close()
    assert job_close_called
    assert owned.poll() is not None
    assert all(not thread.is_alive() for thread in owned._drainers)


def test_close_handle_failure_is_retriable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owned = OwnedProcess(_command("parent"), cwd=str(tmp_path), env=_python_environment())
    _collect_ready_pids(owned, expected=3)
    real_close = owned._job.close
    attempts = 0

    def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("close_injected")
        real_close()

    monkeypatch.setattr(owned._job, "close", fail_once)
    with pytest.raises(OSError, match="close_injected"):
        owned.close()
    observation = owned.close()
    assert observation.confirmed
    assert attempts == 2


def test_bounded_output_overflow_is_explicit_and_stops_tree(tmp_path: Path) -> None:
    """The byte-bounded buffer must report overflow and stop its process tree."""
    with OwnedProcess(
        _command("flood"),
        cwd=str(tmp_path),
        env=_python_environment(),
        event_capacity_bytes=4096,
    ) as owned:
        deadline = time.monotonic() + 5
        overflow = None
        while overflow is None and time.monotonic() < deadline:
            event = owned.next_event(timeout=0.25)
            if event is not None and event.channel == "system":
                overflow = event
        assert overflow is not None
        assert overflow.data == b"stream_overflow"
        observation = owned.force_stop(timeout=5)
        assert observation.confirmed is (os.name == "nt")
        assert observation.active_processes == 0


@pytest.mark.parametrize("cycle", range(20))
def test_twenty_streaming_force_stop_cycles_leave_no_survivors(tmp_path: Path, cycle: int) -> None:
    del cycle
    _one_force_stop_cycle(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object handshake")
def test_provider_cannot_start_before_job_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "provider-started"

    def refuse_assignment(self: WindowsJob, process: object) -> None:
        del self, process
        raise OSError("assignment_refused")

    monkeypatch.setattr(WindowsJob, "assign_process_handle", refuse_assignment)
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(OSError, match="assignment_refused"):
        OwnedProcess(command, cwd=str(tmp_path), env=_python_environment())
    assert not marker.exists()
