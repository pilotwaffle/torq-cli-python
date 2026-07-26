"""Thread-safe, bounded streaming primitives for owned subprocess output."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from threading import Condition
from typing import Literal

ProcessChannel = Literal["stdout", "stderr", "system"]


@dataclass(frozen=True, slots=True)
class ProcessEvent:
    """One immutable, provisionally observed process-output event."""

    sequence: int
    channel: ProcessChannel
    data: bytes


class BoundedEventStream:
    """A non-blocking producer queue bounded by buffered payload bytes.

    ``publish`` returns ``False`` when capacity is unavailable and records the
    overflow. Sequence allocation and insertion share one lock, so consumers
    always observe strictly increasing sequence numbers.
    """

    def __init__(self, byte_capacity: int) -> None:
        if byte_capacity <= 0:
            raise ValueError("byte_capacity must be positive")
        self._byte_capacity = byte_capacity
        self._buffered_bytes = 0
        self._next_sequence = 1
        self._events: deque[ProcessEvent] = deque()
        self._overflowed = False
        self._overflow_marker_emitted = False
        self._dropped_events = 0
        self._closed = False
        self._condition = Condition()

    @property
    def byte_capacity(self) -> int:
        """Maximum number of event payload bytes retained at once."""
        return self._byte_capacity

    @property
    def buffered_bytes(self) -> int:
        """Current number of retained payload bytes."""
        with self._condition:
            return self._buffered_bytes

    @property
    def overflowed(self) -> bool:
        """Whether any event has been rejected for exceeding capacity."""
        with self._condition:
            return self._overflowed

    @property
    def dropped_events(self) -> int:
        """Number of rejected or evicted events after capacity exhaustion."""
        with self._condition:
            return self._dropped_events

    @property
    def closed(self) -> bool:
        """Whether producers declared that no more events can arrive."""
        with self._condition:
            return self._closed

    def close(self) -> None:
        """Mark the stream complete and wake every waiting consumer."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def publish(self, channel: ProcessChannel, data: bytes) -> bool:
        """Atomically sequence and enqueue an event without blocking.

        Returns ``False`` and marks the stream overflowed if retaining ``data``
        would exceed the configured capacity. Rejected events receive no
        sequence number.
        """
        if channel not in ("stdout", "stderr", "system"):
            raise ValueError(f"unsupported process event channel: {channel!r}")
        if not isinstance(data, bytes):
            raise TypeError("process event data must be bytes")
        if not data:
            raise ValueError("process event data must not be empty")

        with self._condition:
            if self._closed:
                return False
            if self._overflowed:
                self._dropped_events += 1
                return False
            if len(data) > self._byte_capacity - self._buffered_bytes:
                self._overflowed = True
                self._dropped_events += 1
                if not self._overflow_marker_emitted:
                    marker = b"stream_overflow"
                    self._dropped_events += len(self._events)
                    self._events.clear()
                    self._buffered_bytes = 0
                    if len(marker) <= self._byte_capacity:
                        self._events.append(ProcessEvent(self._next_sequence, "system", marker))
                        self._next_sequence += 1
                        self._buffered_bytes = len(marker)
                        self._condition.notify_all()
                    self._overflow_marker_emitted = True
                return False

            event = ProcessEvent(self._next_sequence, channel, data)
            self._next_sequence += 1
            self._events.append(event)
            self._buffered_bytes += len(data)
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> ProcessEvent | None:
        """Remove the oldest event, waiting up to ``timeout`` seconds.

        ``None`` as a timeout waits indefinitely. A non-positive timeout is a
        non-blocking poll. ``None`` is returned when the timeout expires.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while not self._events:
                if self._closed:
                    return None
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

            event = self._events.popleft()
            self._buffered_bytes -= len(event.data)
            return event

    def get_nowait(self) -> ProcessEvent | None:
        """Remove the oldest event, or return ``None`` if the stream is empty."""
        return self.get(timeout=0.0)
