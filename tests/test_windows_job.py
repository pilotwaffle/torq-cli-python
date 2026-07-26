from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from torq_cli.adapters.windows_job import WindowsJob, cpython_process_handle


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object tests")


def _start_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(60)"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_empty(job: WindowsJob, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while job.active_processes() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.active_processes() == 0


def test_terminate_accounts_for_assigned_process_death() -> None:
    process = _start_sleeper()
    job = WindowsJob()
    try:
        job.assign_process_handle(cpython_process_handle(process))
        assert job.active_processes() == 1

        job.terminate(exit_code=23)
        process.wait(timeout=5)
        _wait_for_empty(job)

        job.terminate(exit_code=23)
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_suspended_process_executes_only_after_assignment_and_resume(tmp_path: object) -> None:
    marker = os.path.join(str(tmp_path), "resumed")
    process = subprocess.Popen(
        (sys.executable, "-c", f"from pathlib import Path; Path({marker!r}).touch()"),
        creationflags=0x00000004,
    )
    job = WindowsJob()
    try:
        handle = cpython_process_handle(process)
        assert not os.path.exists(marker)
        job.assign_process_handle(handle)
        assert not os.path.exists(marker)
        job.resume_process_handle(handle)
        process.wait(timeout=5)
        assert os.path.exists(marker)
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_close_is_idempotent_and_kills_assigned_process() -> None:
    process = _start_sleeper()
    job = WindowsJob()
    try:
        job.assign_process_handle(cpython_process_handle(process))
        job.close()
        job.close()
        process.wait(timeout=5)
        with pytest.raises(RuntimeError, match="windows_job_closed"):
            job.active_processes()
        job.terminate()
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_assignment_after_close_fails_closed() -> None:
    job = WindowsJob()
    job.close()

    with pytest.raises(RuntimeError, match="windows_job_closed"):
        job.assign_process_handle(1)


def test_concurrent_termination_and_close_are_serialized() -> None:
    process = _start_sleeper()
    job = WindowsJob()
    job.assign_process_handle(cpython_process_handle(process))
    barrier = threading.Barrier(8)
    failures: list[BaseException] = []

    def operate(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            if index % 2:
                job.terminate()
            else:
                job.close()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=operate, args=(index,)) for index in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        process.wait(timeout=5)

        assert not failures
        assert not any(thread.is_alive() for thread in threads)
        with pytest.raises(RuntimeError, match="windows_job_closed"):
            job.active_processes()
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("handle", [0, -1])
def test_invalid_process_handle_rejected(handle: int) -> None:
    with WindowsJob() as job, pytest.raises(ValueError, match="windows_job_process_handle_invalid"):
        job.assign_process_handle(handle)
