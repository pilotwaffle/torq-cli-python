from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from torq_cli.adapters.process import OwnedProcess


class _PartialWriter:
    def __init__(self, *, chunk_size: int) -> None:
        self.chunk_size = chunk_size
        self.received = bytearray()
        self.closed = False
        self.flushed = False

    def write(self, content: Any) -> int:
        data = bytes(content)
        count = min(self.chunk_size, len(data))
        self.received.extend(data[:count])
        return count

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


def _bare_owner(stream: object) -> OwnedProcess:
    owner = object.__new__(OwnedProcess)
    owner._lifecycle = threading.Condition(threading.RLock())
    owner._closed = False
    owner._background_error = None
    owner._process = SimpleNamespace(stdin=stream, poll=lambda: None)
    owner._stdin_writer = None
    return owner


def test_input_writer_retries_partial_writes_until_complete() -> None:
    stream = _PartialWriter(chunk_size=2)
    owner = _bare_owner(stream)

    owner._write_input(b"abcdef")

    assert stream.received == b"abcdef"
    assert stream.flushed
    assert stream.closed
    assert owner.background_error is None


def test_input_writer_surfaces_zero_write_without_losing_first_error() -> None:
    stream = _PartialWriter(chunk_size=0)
    owner = _bare_owner(stream)
    prior = OSError("prior_transport_error")
    owner._background_error = prior

    owner._write_input(b"payload")

    assert owner.background_error is prior
    assert stream.closed


def test_input_cleanup_closes_stdin_idempotently() -> None:
    stream = _PartialWriter(chunk_size=1)
    owner = _bare_owner(stream)

    assert owner._close_input() is None
    assert owner._close_input() is None
    assert stream.closed


def test_input_writer_that_cannot_terminate_is_visible() -> None:
    class StuckThread:
        def join(self, timeout: float) -> None:
            assert timeout >= 0

        def is_alive(self) -> bool:
            return True

    owner = _bare_owner(_PartialWriter(chunk_size=1))
    owner._stdin_writer = StuckThread()  # type: ignore[assignment]

    assert not owner._join_input(time.monotonic() + 0.01)
    assert isinstance(owner.background_error, RuntimeError)
    assert str(owner.background_error) == "owned_process_input_writer_stuck"
