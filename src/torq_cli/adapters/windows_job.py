"""Windows Job Object containment isolated behind a small lifecycle-safe API."""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any


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


class WindowsJob:
    """Own a Windows Job Object configured to kill all members when closed.

    Construction configures ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` before the
    handle is exposed for process assignment. All public operations serialize
    on one lifecycle lock, making termination and close safe to repeat.
    """

    _BASIC_ACCOUNTING = 1
    _EXTENDED_LIMIT = 9
    _KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        """Create and configure an empty kill-on-close Job Object.

        Raises:
            OSError: If this platform is not Windows or a Win32 operation fails.
        """
        if sys.platform != "win32":
            raise OSError("windows_job_unsupported_platform")

        self._lock = threading.RLock()
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._ntdll: Any = ctypes.WinDLL("ntdll", use_last_error=True)
        self._configure_api()

        raw_handle = kernel32.CreateJobObjectW(None, None)
        if not raw_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: int | None = int(raw_handle)

        limits = _JobObjectExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            ctypes.c_void_p(self._handle),
            self._EXTENDED_LIMIT,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            try:
                kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            finally:
                self._handle = None
            raise ctypes.WinError(error)

    def _configure_api(self) -> None:
        kernel32 = self._kernel32
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
        self._ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
        self._ntdll.NtResumeProcess.restype = ctypes.c_long

    def assign_process_handle(self, handle: int) -> None:
        """Assign an open process handle to this Job Object.

        The caller must keep the process handle valid for this call. Assignment
        after close fails rather than allowing execution outside containment.
        """
        if not isinstance(handle, int) or handle <= 0:
            raise ValueError("windows_job_process_handle_invalid")
        with self._lock:
            job_handle = self._require_open()
            if not self._kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(job_handle), ctypes.c_void_p(handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self, exit_code: int = 1) -> None:
        """Force every process in the job to exit, or do nothing if closed.

        This operation is idempotent. A Win32 failure is surfaced as ``OSError``
        so callers never mistake a failed termination request for confirmation.
        """
        if not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("windows_job_exit_code_invalid")
        with self._lock:
            if self._handle is None:
                return
            if not self._kernel32.TerminateJobObject(
                ctypes.c_void_p(self._handle), ctypes.c_uint32(exit_code)
            ):
                raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        """Return the number of processes Windows currently accounts as active.

        A closed job raises because its accounting handle is gone; returning zero
        in that state would falsely upgrade kill-on-close into observed process
        death. Win32 query failures are likewise raised to the caller.
        """
        with self._lock:
            job_handle = self._require_open()
            accounting = _JobObjectAccounting()
            if not self._kernel32.QueryInformationJobObject(
                ctypes.c_void_p(job_handle),
                self._BASIC_ACCOUNTING,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(accounting.ActiveProcesses)

    def resume_process_handle(self, handle: int) -> None:
        """Resume a suspended process only after it belongs to this job."""
        if not isinstance(handle, int) or handle <= 0:
            raise ValueError("windows_job_process_handle_invalid")
        with self._lock:
            self._require_open()
            status = int(self._ntdll.NtResumeProcess(ctypes.c_void_p(handle)))
            if status != 0:
                raise OSError(status, "windows_process_resume_failed")

    def close(self) -> None:
        """Close ownership and trigger kill-on-close, safely and idempotently."""
        with self._lock:
            if self._handle is None:
                return
            handle = self._handle
            if not self._kernel32.CloseHandle(ctypes.c_void_p(handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def _require_open(self) -> int:
        if self._handle is None:
            raise RuntimeError("windows_job_closed")
        return self._handle

    def __enter__(self) -> WindowsJob:
        """Return this open job for use as a context manager."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close this job when its context exits."""
        del exc_type, exc, traceback
        self.close()


def cpython_process_handle(process: object) -> int:
    """Return CPython's Windows process handle without leaking it to callers."""
    raw_handle = getattr(process, "_handle", None)
    if not isinstance(raw_handle, int) or raw_handle <= 0:
        raise RuntimeError("windows_process_handle_unavailable")
    return raw_handle
