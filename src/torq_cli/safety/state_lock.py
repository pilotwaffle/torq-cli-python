"""Kernel-held installation lock for the task state authority."""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType

from torq_cli.safety.receipts import restrict_owner_only_directory, restrict_owner_only_file


class InstallationStateLock:
    def __init__(self, state_root: Path) -> None:
        state_root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(state_root)
        self.path = state_root / "task-owner.lock"
        self._stream = self.path.open("a+b")
        restrict_owner_only_file(self.path)
        try:
            if sys.platform == "win32":
                import msvcrt

                self._stream.seek(0)
                if self._stream.tell() == 0 and self.path.stat().st_size == 0:
                    self._stream.write(b"\0")
                    self._stream.flush()
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                flock = getattr(fcntl, "flock")
                flock(self._stream.fileno(), getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"))
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            raise RuntimeError("task_state_already_owned") from exc

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                flock = getattr(fcntl, "flock")
                flock(self._stream.fileno(), getattr(fcntl, "LOCK_UN"))
        finally:
            self._stream.close()

    def __enter__(self) -> InstallationStateLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = ["InstallationStateLock"]
