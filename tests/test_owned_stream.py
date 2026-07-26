from __future__ import annotations

import threading

import pytest

from torq_cli.adapters.owned_stream import BoundedEventStream


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoundedEventStream(0)


def test_empty_events_are_rejected_and_close_wakes_consumers() -> None:
    stream = BoundedEventStream(32)
    with pytest.raises(ValueError, match="must not be empty"):
        stream.publish("stdout", b"")
    stream.close()
    assert stream.closed
    assert stream.get(timeout=1) is None
    assert not stream.publish("stdout", b"late")


def test_publish_and_get_account_for_buffered_bytes() -> None:
    stream = BoundedEventStream(10)

    assert stream.publish("stdout", b"abc")
    assert stream.publish("stderr", b"de")
    assert stream.buffered_bytes == 5

    first = stream.get_nowait()
    assert first is not None
    assert (first.sequence, first.channel, first.data) == (1, "stdout", b"abc")
    assert stream.buffered_bytes == 2

    second = stream.get_nowait()
    assert second is not None
    assert (second.sequence, second.channel, second.data) == (2, "stderr", b"de")
    assert stream.buffered_bytes == 0
    assert stream.get_nowait() is None


def test_overflow_is_explicit_and_does_not_consume_sequence() -> None:
    stream = BoundedEventStream(32)

    assert stream.publish("stdout", b"1" * 32)
    assert not stream.publish("stderr", b"5")
    assert not stream.publish("system", b"overflow")
    assert stream.overflowed
    assert stream.dropped_events == 3
    assert stream.buffered_bytes == len(b"stream_overflow")

    marker = stream.get_nowait()
    assert marker is not None
    assert (marker.channel, marker.data) == ("system", b"stream_overflow")
    assert not stream.publish("system", b"ok")
    assert stream.get_nowait() is None


def test_concurrent_publish_preserves_consumer_sequence_order() -> None:
    producer_count = 8
    events_per_producer = 200
    stream = BoundedEventStream(producer_count * events_per_producer)
    barrier = threading.Barrier(producer_count)

    def produce(producer: int) -> None:
        barrier.wait()
        for index in range(events_per_producer):
            assert stream.publish("stdout", bytes([(producer + index) % 256]))

    threads = [
        threading.Thread(target=produce, args=(producer,)) for producer in range(producer_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)

    events = []
    while (event := stream.get_nowait()) is not None:
        events.append(event)

    expected_count = producer_count * events_per_producer
    assert len(events) == expected_count
    assert [event.sequence for event in events] == list(range(1, expected_count + 1))
    assert stream.buffered_bytes == 0
    assert not stream.overflowed
