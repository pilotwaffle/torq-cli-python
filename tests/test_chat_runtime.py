from __future__ import annotations

import time
import base64
from collections.abc import Mapping
from typing import Any
from pathlib import Path

import pytest

from torq_cli.adapters.owned_stream import ProcessEvent
from torq_cli.adapters.process import ContainmentState, ExitObservation
from torq_cli.application.chat_runtime import (
    ChatBusyError,
    ChatProviderCommand,
    ChatRuntimeCoordinator,
    _extract_usage,
)


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.run_root = Path(".").resolve()

    def append(self, event: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        row = {"sequence": len(self.events) + 1, "event": event, "body": dict(body)}
        self.events.append(row)
        return row

    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.events)


class _Owner:
    def __init__(self, *, confirmed: bool = True, returncode: int = 0) -> None:
        self.pid = 42
        self.confirmed = confirmed
        self.returncode = returncode
        self.events = [ProcessEvent(1, "stdout", b"answer")]
        self.stopped = False
        self.closed = False
        self.background_error: BaseException | None = None

    @property
    def output_closed(self) -> bool:
        return not self.events

    def next_event(self, *, timeout: float) -> ProcessEvent | None:
        del timeout
        if self.events:
            return self.events.pop(0)
        time.sleep(0.01)
        return None

    def poll(self) -> int | None:
        return self.returncode if not self.events else None

    def wait(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        return self._observation(False)

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        self.stopped = True
        self.returncode = 1
        self.events.clear()
        return self._observation(True)

    def close(self) -> ExitObservation:
        self.closed = True
        return self._observation(self.stopped)

    def _observation(self, forced: bool) -> ExitObservation:
        state = ContainmentState.KNOWN_EMPTY if self.confirmed else ContainmentState.UNKNOWN
        return ExitObservation(
            self.returncode,
            self.confirmed,
            0 if self.confirmed else None,
            forced,
            True,
            state,
        )


def _command(*_args: object) -> ChatProviderCommand:
    return ChatProviderCommand(("provider",), ".", {})


def _wait_terminal(sink: _Sink) -> None:
    deadline = time.monotonic() + 2
    while not any(row["event"].startswith("turn_completed") for row in sink.events):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _wait_any_terminal(sink: _Sink) -> None:
    deadline = time.monotonic() + 2
    while not any(
        row["event"]
        in {"turn_completed", "turn_failed", "turn_cancelled", "turn_cancellation_uncertain"}
        for row in sink.events
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_completed_turn_streams_provisionally_then_commits_terminal() -> None:
    sink = _Sink()
    owner = _Owner()
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    stream = runtime.subscribe()
    runtime.submit(turn_id="turn-1", text="hello")
    _wait_terminal(sink)

    event = stream.get(timeout=1)
    assert (event.kind, event.data) == ("stdout", b"answer")
    assert [row["event"] for row in sink.events] == [
        "turn_submitted",
        "turn_started",
        "turn_completed",
    ]
    assert sink.events[-1]["body"]["content"] == "answer"
    assert runtime.snapshot()["active_turn_id"] is None


def test_only_one_turn_can_own_runtime() -> None:
    sink = _Sink()
    owner = _Owner()
    owner.events = []
    owner.returncode = 0
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime._active_turn = "existing"
    with pytest.raises(ChatBusyError, match="chat_turn_active"):
        runtime.submit(turn_id="turn-2", text="blocked")


@pytest.mark.parametrize(
    ("confirmed", "terminal"),
    ((True, "turn_cancelled"), (False, "turn_cancellation_uncertain")),
)
def test_cancel_reports_observed_containment_truth(confirmed: bool, terminal: str) -> None:
    sink = _Sink()
    owner = _Owner(confirmed=confirmed)
    owner.events = []
    owner.returncode = None  # type: ignore[assignment]
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime._owner = owner
    runtime._active_turn = "turn-3"
    row = runtime.cancel("turn-3")
    assert row["event"] == terminal
    assert owner.stopped and owner.closed


def test_restart_marks_incomplete_turn_uncertain() -> None:
    sink = _Sink()
    sink.events.extend(
        [
            {"sequence": 1, "event": "turn_submitted", "body": {"turn_id": "lost"}},
            {"sequence": 2, "event": "turn_started", "body": {"turn_id": "lost"}},
        ]
    )
    runtime = ChatRuntimeCoordinator(sink, _command)
    recovered = runtime.recover_incomplete()
    assert recovered[0]["event"] == "turn_cancellation_uncertain"
    assert recovered[0]["body"]["reason"] == "coordinator_restarted"


def test_pump_drains_all_buffered_output_after_leader_exit() -> None:
    sink = _Sink()
    owner = _Owner()
    owner.events = [
        ProcessEvent(1, "stdout", b"first"),
        ProcessEvent(2, "stdout", b"-second"),
    ]
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime.submit(turn_id="turn-drain", text="hello")
    _wait_terminal(sink)
    assert sink.events[-1]["body"]["content"] == "first-second"


def test_turn_identifier_cannot_be_reused() -> None:
    sink = _Sink()
    sink.events.extend(
        [
            {"event": "turn_submitted", "body": {"turn_id": "same"}},
            {"event": "turn_failed", "body": {"turn_id": "same"}},
        ]
    )
    runtime = ChatRuntimeCoordinator(sink, _command)
    with pytest.raises(ValueError, match="chat_turn_id_reused"):
        runtime.submit(turn_id="same", text="again")


def test_started_evidence_failure_force_stops_and_clears_owner() -> None:
    class FailingSink(_Sink):
        def append(self, event: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
            if event == "turn_started":
                raise OSError("evidence unavailable")
            return super().append(event, body)

    sink = FailingSink()
    owner = _Owner()
    owner.events = []
    owner.returncode = None  # type: ignore[assignment]
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    with pytest.raises(RuntimeError, match="chat_runtime_start_failed"):
        runtime.submit(turn_id="turn-start-fail", text="hello")
    assert owner.stopped and owner.closed
    assert runtime.snapshot()["active_turn_id"] is None


def test_force_stop_exception_uses_close_fallback_and_terminalizes_uncertain() -> None:
    sink = _Sink()
    owner = _Owner(confirmed=False)
    owner.events = []
    owner.returncode = None  # type: ignore[assignment]

    def fail_stop(*, timeout: float = 5.0) -> ExitObservation:
        del timeout
        raise OSError("stop failed")

    owner.force_stop = fail_stop  # type: ignore[method-assign]
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime._owner = owner
    runtime._active_turn = "turn-stop-fail"
    row = runtime.cancel("turn-stop-fail")
    assert row["event"] == "turn_cancellation_uncertain"
    assert runtime.snapshot()["active_turn_id"] is None


def test_attachment_signature_is_authoritative_at_runtime_boundary() -> None:
    runtime = ChatRuntimeCoordinator(_Sink(), _command)
    with pytest.raises(ValueError, match="chat_attachment_signature_mismatch"):
        runtime.submit(
            turn_id="turn-fake-image",
            text="inspect",
            attachments=(
                {
                    "name": "fake.png",
                    "media_type": "image/png",
                    "content_base64": base64.b64encode(b"not-png").decode("ascii"),
                },
            ),
        )


def test_failed_cancel_evidence_keeps_recovery_state_and_still_closes_owner() -> None:
    class FailingSink(_Sink):
        def append(self, event: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
            if event == "turn_cancellation_uncertain":
                raise OSError("evidence unavailable")
            return super().append(event, body)

    sink = FailingSink()
    owner = _Owner(confirmed=False)
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime._owner = owner
    runtime._active_turn = "turn-recovery"
    with pytest.raises(OSError, match="evidence unavailable"):
        runtime.cancel("turn-recovery")
    assert owner.closed
    assert runtime.snapshot()["active_turn_id"] == "turn-recovery"
    assert runtime.snapshot()["background_finding"] == "chat_runtime_recovery_required"


def test_background_failure_after_cancel_request_uses_cancel_semantics() -> None:
    sink = _Sink()
    owner = _Owner(confirmed=True)
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime._owner = owner
    runtime._active_turn = "turn-race"
    runtime._cancellation_requested = True
    runtime._fail_background("turn-race", owner)
    assert sink.events[-1]["event"] == "turn_cancelled"


def test_usage_is_signed_as_unavailable_instead_of_invented_zero() -> None:
    sink = _Sink()
    owner = _Owner()

    def command(*_args: object) -> ChatProviderCommand:
        return ChatProviderCommand(
            ("provider",),
            ".",
            {},
            provider="deepseek",
            model="deepseek-v4-pro",
            settlement="plan_covered",
        )

    runtime = ChatRuntimeCoordinator(sink, command, owner_factory=lambda *_a, **_k: owner)
    runtime.submit(turn_id="turn-accounting", text="hello")
    _wait_terminal(sink)
    body = sink.events[-1]["body"]
    assert body["billed_usd"] == "0"
    assert body["metered_usd"] is None
    assert body["usage"] == "unreported"
    assert body["pricing_status"] == "usage_unreported"


def test_unconfirmed_provider_exit_never_claims_failed_or_completed() -> None:
    sink = _Sink()
    owner = _Owner(confirmed=False, returncode=4)
    runtime = ChatRuntimeCoordinator(sink, _command, owner_factory=lambda *_a, **_k: owner)
    runtime.submit(turn_id="turn-unconfirmed", text="hello")
    _wait_any_terminal(sink)
    assert sink.events[-1]["event"] == "turn_cancellation_uncertain"


def test_incomplete_usage_record_is_unreported_not_zero_filled() -> None:
    assert _extract_usage(b'TORQ_CHAT_USAGE\t{"input_tokens":7}\n')[0] is None
    assert _extract_usage(b"TORQ_CHAT_USAGE\t{}\n")[0] is None
    usage, diagnostics = _extract_usage(b'TORQ_CHAT_USAGE\t{"input_tokens":7,"output_tokens":3}\n')
    assert usage == {"input_tokens": 7, "output_tokens": 3, "reasoning_tokens": 0}
    assert diagnostics == b""
