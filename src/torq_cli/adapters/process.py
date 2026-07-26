"""The sole production subprocess boundary; audited by hermetic import tests."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BufferedReader
from typing import Any


class ManagedProcess:
    """Cross-platform process-tree boundary using process groups/job-tree fallback."""

    def __init__(self, command: Sequence[str], *, cwd: str, env: Mapping[str, str]) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            creationflags=flags,
            start_new_session=os.name != "nt",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @classmethod
    def for_provider_config(
        cls,
        command: Sequence[str],
        *,
        cwd: str,
        provider: str,
        config: Mapping[str, Any],
        base_environment: Mapping[str, str],
    ) -> ManagedProcess:
        """Start a provider child using only its config-resolved credential."""
        from torq_cli.connectors.credential_sources import provider_environment_from_config

        environment = provider_environment_from_config(config, provider, base_environment)
        return cls(command, cwd=cwd, env=environment)

    def cancel_tree(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(("taskkill", "/PID", str(self.process.pid), "/T", "/F"), capture_output=True, check=False)
        else:
            killpg = getattr(os, "killpg")
            getpgid = getattr(os, "getpgid")
            killpg(getpgid(self.process.pid), getattr(signal, "SIGKILL"))
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


@dataclass(frozen=True)
class ProcessEvent:
    """One ordered, provisional output event from an owned process."""

    sequence: int
    channel: str
    data: bytes


@dataclass(frozen=True)
class ExitObservation:
    """OS observation made after a process has exited or a deadline elapsed."""

    returncode: int | None
    confirmed: bool
    active_processes: int | None
    forced: bool


class _JobObjectAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _JobObjectBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """Small fail-closed wrapper around a kill-on-close Windows Job Object."""

    _BASIC_ACCOUNTING = 1
    _EXTENDED_LIMIT = 9
    _KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.QueryInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        kernel32.QueryInformationJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        limits = _JobObjectExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            self._handle = None
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, ctypes.c_void_p(int(process._handle))
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(
            self._handle, exit_code
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        if self._handle is None:
            return 0
        accounting = _JobObjectAccounting()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._BASIC_ACCOUNTING,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class OwnedProcess:
    """Experimental process owner with ordered output and observed tree death.

    On Windows, a bootstrap blocks on stdin until it has been assigned to a
    kill-on-close Job Object. The provider and all descendants therefore begin
    only after the ownership boundary exists. POSIX providers start in a new
    session and are controlled as one process group.
    """

    def __init__(self, command: Sequence[str], *, cwd: str, env: Mapping[str, str]) -> None:
        if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
            raise ValueError("owned_process_command_invalid")
        self._events: queue.Queue[ProcessEvent] = queue.Queue()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._job: _WindowsJob | None = None
        self._pgid: int | None = None
        self._closed = False

        if os.name == "nt":
            self._job = _WindowsJob()
            bootstrap = (sys.executable, "-m", "torq_cli.adapters.process", "--owned-bootstrap")
            try:
                process = subprocess.Popen(
                    bootstrap,
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except BaseException:
                self._job.close()
                raise
            try:
                self._job.assign(process)
                if process.stdin is None:
                    raise RuntimeError("owned_process_bootstrap_stdin_missing")
                process.stdin.write(json.dumps(list(command)).encode("utf-8") + b"\n")
                process.stdin.close()
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                self._job.close()
                raise
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            self._pgid = os.getpgid(process.pid)
        self.process: subprocess.Popen[bytes] = process
        try:
            self._drainers = self._start_drainers()
        except BaseException:
            if self._job is not None:
                self._job.close()
            elif self._pgid is not None:
                os.killpg(self._pgid, signal.SIGKILL)
            process.wait(timeout=5)
            raise

    @property
    def pid(self) -> int:
        return self.process.pid

    def _start_drainers(self) -> tuple[threading.Thread, threading.Thread]:
        if self.process.stdout is None or self.process.stderr is None:
            raise RuntimeError("owned_process_output_pipe_missing")
        threads = (
            threading.Thread(target=self._drain, args=("stdout", self.process.stdout), daemon=True),
            threading.Thread(target=self._drain, args=("stderr", self.process.stderr), daemon=True),
        )
        for thread in threads:
            thread.start()
        return threads

    def _drain(self, channel: str, stream: BufferedReader) -> None:
        while True:
            data = stream.readline()
            if not data:
                return
            with self._sequence_lock:
                self._sequence += 1
                event = ProcessEvent(self._sequence, channel, data)
            self._events.put(event)

    def next_event(self, *, timeout: float) -> ProcessEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        """Force the owned tree down and report whether tree death was observed."""
        if self.process.poll() is None:
            if self._job is not None:
                self._job.terminate()
            elif self._pgid is not None:
                os.killpg(self._pgid, signal.SIGKILL)
        deadline = time.monotonic() + timeout
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        active = self._active_processes()
        while active not in (None, 0) and time.monotonic() < deadline:
            time.sleep(0.01)
            active = self._active_processes()
        confirmed = self.process.poll() is not None and active == 0
        self._join_drainers(deadline)
        return ExitObservation(self.process.poll(), confirmed, active, True)

    def _active_processes(self) -> int | None:
        if self._job is not None:
            return self._job.active_processes()
        if self._pgid is None:
            return None
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return None
        return 1

    def _join_drainers(self, deadline: float) -> None:
        for thread in self._drainers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        if self._closed:
            return
        if self.process.poll() is None:
            self.force_stop()
        if self._job is not None:
            self._job.close()
        self._closed = True

    def __enter__(self) -> OwnedProcess:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


def _owned_bootstrap() -> int:
    """Start the real Windows target only after the parent releases the handshake."""
    encoded = sys.stdin.buffer.readline(1_048_577)
    if not encoded or len(encoded) > 1_048_576:
        return 125
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 125
    if not isinstance(payload, list) or not payload or not all(
        isinstance(part, str) and "\x00" not in part for part in payload
    ):
        return 125
    child = subprocess.Popen(payload, stdin=subprocess.DEVNULL)
    return child.wait()


if __name__ == "__main__" and sys.argv[1:] == ["--owned-bootstrap"]:
    raise SystemExit(_owned_bootstrap())
