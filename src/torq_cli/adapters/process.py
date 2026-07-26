"""The sole production subprocess boundary; audited by hermetic import tests."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from io import BufferedReader
from typing import Any

from torq_cli.adapters.macos_containment import require_macos_strong_containment
from torq_cli.adapters.linux_cgroup import LinuxSystemdCgroup
from torq_cli.adapters.linux_containment import linux_containment_capability
from torq_cli.adapters.owned_stream import BoundedEventStream, ProcessChannel, ProcessEvent
from torq_cli.adapters.windows_job import WindowsJob, cpython_process_handle


class ManagedProcess:
    """Legacy provider process boundary retained for existing dispatch paths."""

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
        """Cancel the legacy process tree."""
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(self.process.pid), "/T", "/F"),
                capture_output=True,
                check=False,
            )
        else:
            killpg = getattr(os, "killpg")
            getpgid = getattr(os, "getpgid")
            killpg(getpgid(self.process.pid), 9)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


class ContainmentState(str, Enum):
    """Strength of the latest process-tree containment observation."""

    KNOWN_EMPTY = "known_empty"
    KNOWN_ACTIVE = "known_active"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExitObservation:
    """OS observation made after a process has exited or a deadline elapsed."""

    returncode: int | None
    confirmed: bool
    active_processes: int | None
    forced: bool
    output_complete: bool
    containment_state: ContainmentState


class OwnedProcess:
    """Own one kernel-contained process tree and expose bounded provisional output.

    Windows creates the provider suspended, assigns it to a kill-on-close Job
    Object, and only then resumes it. Linux delegates pre-exec placement and
    whole-cgroup termination to a protected transient systemd service. Other
    platforms fail closed; process groups are never treated as ownership.
    """

    _CREATE_SUSPENDED = 0x00000004
    _EXPERIMENTAL_LINUX = False

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        event_capacity_bytes: int = 1_048_576,
        chunk_size: int = 4096,
        input_data: bytes | None = None,
    ) -> None:
        if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
            raise ValueError("owned_process_command_invalid")
        if sys.platform == "darwin":
            require_macos_strong_containment()
        if os.name != "nt" and not sys.platform.startswith("linux"):
            raise OSError("owned_process_strong_containment_unavailable")
        if sys.platform.startswith("linux") and not self._EXPERIMENTAL_LINUX:
            capability = linux_containment_capability()
            if not capability.available:
                raise OSError("owned_process_strong_containment_unavailable")
        if len(subprocess.list2cmdline(command)) > 32_767:
            raise ValueError("owned_process_command_too_large")
        if event_capacity_bytes < len(b"stream_overflow"):
            raise ValueError("owned_process_event_capacity_too_small")
        if chunk_size <= 0 or chunk_size > event_capacity_bytes:
            raise ValueError("owned_process_chunk_size_invalid")
        if input_data is not None and len(input_data) > 42_000_000:
            raise ValueError("owned_process_input_too_large")

        self._events = BoundedEventStream(event_capacity_bytes)
        self._chunk_size = chunk_size
        self._windows_job = WindowsJob() if os.name == "nt" else None
        self._linux_job = LinuxSystemdCgroup() if os.name != "nt" else None
        if self._windows_job is not None:
            self._job: WindowsJob | LinuxSystemdCgroup = self._windows_job
        elif self._linux_job is not None:
            self._job = self._linux_job
        else:
            raise OSError("owned_process_strong_containment_unavailable")
        self._lifecycle = threading.Condition(threading.RLock())
        self._stopping = False
        self._cleaning = False
        self._closed = False
        self._stop_observation: ExitObservation | None = None
        self._overflow_stop_started = False
        self._force_requested = False
        self._background_error: BaseException | None = None
        self._active_waiters = 0
        self._drainers_remaining = 2

        process: subprocess.Popen[bytes]
        try:
            launch_command = command
            launch_environment = dict(env)
            launch_input = input_data
            creation_flags = self._CREATE_SUSPENDED
            if os.name != "nt":
                linux_job = self._linux_job
                if linux_job is None:
                    raise OSError("owned_process_strong_containment_unavailable")
                launch_command = linux_job.launch_command(command, cwd=cwd)
                launch_environment = linux_job.launcher_environment(env)
                launch_input = linux_job.framed_input(env, input_data)
                creation_flags = 0
            process = subprocess.Popen(
                launch_command,
                cwd=cwd,
                env=launch_environment,
                stdin=subprocess.PIPE if launch_input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
                bufsize=0,
            )
            if os.name == "nt":
                windows_job = self._windows_job
                if windows_job is None:
                    raise OSError("owned_process_strong_containment_unavailable")
                process_handle = cpython_process_handle(process)
                windows_job.assign_process_handle(process_handle)
                windows_job.resume_process_handle(process_handle)
            else:
                if linux_job is None:
                    raise OSError("owned_process_strong_containment_unavailable")
                linux_job.bind_process(process)
        except BaseException:
            try:
                self._cleanup_failed_start(locals().get("process"))
            except BaseException:
                pass
            raise
        self._process = process
        self._stdin_writer: threading.Thread | None = None
        if launch_input is not None:
            self._stdin_writer = threading.Thread(
                target=self._write_input,
                args=(launch_input, os.name == "nt"),
                daemon=True,
            )
            self._stdin_writer.start()
        try:
            self._drainers = self._start_drainers()
        except BaseException:
            try:
                try:
                    self._job.terminate()
                finally:
                    process.wait(timeout=5)
            except BaseException:
                pass
            try:
                self._job.close()
            except BaseException:
                pass
            raise

    @property
    def pid(self) -> int:
        """Return the owned leader PID without exposing its process handle."""
        if self._linux_job is not None and self._linux_job.leader_pid is not None:
            return self._linux_job.leader_pid
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """Return the leader return code when it has exited."""
        return self._process.poll()

    @property
    def background_error(self) -> BaseException | None:
        """Return an output/cancellation error raised by a background worker."""
        with self._lifecycle:
            return self._background_error

    @property
    def output_closed(self) -> bool:
        """Whether both output pipes reached EOF and no more events can arrive."""
        return self._events.closed

    def poll(self) -> int | None:
        """Poll the leader process."""
        return self._process.poll()

    def _cleanup_failed_start(self, process: object) -> None:
        candidate = process if isinstance(process, subprocess.Popen) else None
        try:
            self._job.close()
        finally:
            if candidate is not None:
                try:
                    candidate.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    candidate.kill()
                    candidate.wait(timeout=5)

    def _start_drainers(self) -> tuple[threading.Thread, threading.Thread]:
        if self._process.stdout is None or self._process.stderr is None:
            raise RuntimeError("owned_process_output_pipe_missing")
        threads = (
            threading.Thread(
                target=self._drain, args=("stdout", self._process.stdout), daemon=True
            ),
            threading.Thread(
                target=self._drain, args=("stderr", self._process.stderr), daemon=True
            ),
        )
        for thread in threads:
            thread.start()
        return threads

    def _write_input(self, content: bytes, close_stream: bool = True) -> None:
        stream = self._process.stdin
        if stream is None:
            return
        remaining = memoryview(content)
        try:
            while remaining:
                written = stream.write(remaining)
                if written is None or written <= 0:
                    raise OSError("owned_process_input_short_write")
                remaining = remaining[written:]
            stream.flush()
        except (OSError, ValueError) as exc:
            with self._lifecycle:
                if not self._closed and self._process.poll() is None:
                    if self._background_error is None:
                        self._background_error = exc
        finally:
            remaining.release()
            if close_stream:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _drain(self, channel: ProcessChannel, stream: BufferedReader) -> None:
        try:
            while True:
                data = os.read(stream.fileno(), self._chunk_size)
                if not data:
                    return
                if self._events.publish(channel, data):
                    continue
                with self._lifecycle:
                    if self._overflow_stop_started:
                        return
                    self._overflow_stop_started = True
                threading.Thread(target=self._background_force_stop, daemon=True).start()
                return
        except OSError as exc:
            with self._lifecycle:
                if not self._closed:
                    self._background_error = exc
        finally:
            with self._lifecycle:
                self._drainers_remaining -= 1
                if self._drainers_remaining == 0:
                    self._events.close()

    def _background_force_stop(self) -> None:
        try:
            self.force_stop()
        except BaseException as exc:
            with self._lifecycle:
                self._background_error = exc

    def next_event(self, *, timeout: float) -> ProcessEvent | None:
        """Return the next provisional event, preserving global sequence order."""
        self._validate_timeout(timeout)
        return self._events.get(timeout)

    def wait(self, *, timeout: float = 5.0) -> ExitObservation:
        """Wait for the leader and report Job containment without forcing it."""
        self._validate_timeout(timeout)
        with self._lifecycle:
            if self._closed:
                return self._stop_observation or self._uncertain_observation()
            self._active_waiters += 1
        try:
            self._process.wait(timeout=timeout)
            state, active = self._containment()
            output_complete = self._join_drainers(time.monotonic() + 1.0)
            self._join_input(time.monotonic() + 1.0)
            with self._lifecycle:
                forced = self._force_requested
            return ExitObservation(
                self._process.returncode,
                state is ContainmentState.KNOWN_EMPTY,
                active,
                forced,
                output_complete,
                state,
            )
        finally:
            with self._lifecycle:
                self._active_waiters -= 1
                self._lifecycle.notify_all()

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        """Force the owned Job down and return an honest OS observation."""
        self._validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        with self._lifecycle:
            while self._cleaning:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._uncertain_observation()
                self._lifecycle.wait(remaining)
            while self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._uncertain_observation()
                self._lifecycle.wait(remaining)
            if self._stop_observation is not None:
                return self._stop_observation
            self._stopping = True
        try:
            observation = self._force_stop_impl(deadline)
        except BaseException:
            with self._lifecycle:
                self._stopping = False
                self._lifecycle.notify_all()
            raise
        with self._lifecycle:
            if (
                observation.returncode is not None
                and observation.containment_state is ContainmentState.KNOWN_EMPTY
            ):
                self._stop_observation = observation
            self._stopping = False
            self._lifecycle.notify_all()
        return observation

    def _force_stop_impl(self, deadline: float) -> ExitObservation:
        state, _ = self._containment()
        forced = state is not ContainmentState.KNOWN_EMPTY
        if forced:
            self._force_requested = True
            if self._linux_job is not None:
                self._linux_job.terminate(timeout=max(0.001, deadline - time.monotonic()))
            else:
                self._job.terminate()
        self._wait_root(max(0.0, deadline - time.monotonic()))
        state, active = self._containment()
        while state is ContainmentState.KNOWN_ACTIVE and time.monotonic() < deadline:
            time.sleep(0.01)
            state, active = self._containment()
        output_complete = self._join_drainers(min(deadline, time.monotonic() + 1.0))
        self._join_input(min(deadline, time.monotonic() + 1.0))
        confirmed = self._process.poll() is not None and state is ContainmentState.KNOWN_EMPTY
        return ExitObservation(
            self._process.poll(),
            confirmed,
            active,
            self._force_requested,
            output_complete,
            state,
        )

    def _containment(self) -> tuple[ContainmentState, int | None]:
        try:
            active = self._job.active_processes()
        except (OSError, RuntimeError):
            return ContainmentState.UNKNOWN, None
        state = ContainmentState.KNOWN_EMPTY if active == 0 else ContainmentState.KNOWN_ACTIVE
        return state, active

    def _wait_root(self, timeout: float) -> None:
        try:
            self._process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            pass

    def _join_drainers(self, deadline: float) -> bool:
        for thread in self._drainers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._drainers)

    def _join_input(self, deadline: float) -> bool:
        if self._stdin_writer is None:
            return True
        self._stdin_writer.join(timeout=max(0.0, deadline - time.monotonic()))
        complete = not self._stdin_writer.is_alive()
        if not complete:
            with self._lifecycle:
                if self._background_error is None:
                    self._background_error = RuntimeError("owned_process_input_writer_stuck")
        return complete

    def _close_input(self) -> BaseException | None:
        stream = self._process.stdin
        if stream is None:
            return None
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            return exc
        return None

    def _uncertain_observation(self) -> ExitObservation:
        state, active = self._containment()
        return ExitObservation(
            self._process.poll(),
            False,
            active,
            self._force_requested,
            False,
            state,
        )

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("owned_process_timeout_invalid")

    def _release_resources(self) -> BaseException | None:
        try:
            self._job.close()
        except BaseException as exc:
            return exc
        first_error = self._close_input()
        self._wait_root(5.0)
        input_complete = self._join_input(time.monotonic() + 1.0)
        if not input_complete and first_error is None:
            first_error = self.background_error
        self._join_drainers(time.monotonic() + 1.0)

        for resource in (self._process.stdout, self._process.stderr):
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._join_drainers(time.monotonic() + 1.0)
        return first_error

    def close(self) -> ExitObservation:
        """Stop the Job, release every handle, and return the final observation."""
        with self._lifecycle:
            if self._closed and self._stop_observation is not None:
                return self._stop_observation
        primary_error: BaseException | None = None
        try:
            observation = self.force_stop()
        except BaseException as exc:
            primary_error = exc
            observation = self._uncertain_observation()
        with self._lifecycle:
            while self._stopping:
                self._lifecycle.wait()
            while self._active_waiters:
                self._lifecycle.wait()
            while self._cleaning:
                self._lifecycle.wait()
            if self._closed:
                cleanup_error = None
            else:
                self._cleaning = True
                cleanup_error = None
        if self._cleaning:
            cleanup_error = self._release_resources()
        with self._lifecycle:
            self._closed = cleanup_error is None
            self._cleaning = False
            self._lifecycle.notify_all()
        if primary_error is not None:
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error
        return observation

    def __enter__(self) -> OwnedProcess:
        """Return this owner as a context manager."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Stop and close the owned boundary."""
        del exc_type, exc, traceback
        self.close()


class ExperimentalLinuxOwnedProcess(OwnedProcess):
    """Exercise user-systemd behavior without advertising production ownership."""

    _EXPERIMENTAL_LINUX = True
