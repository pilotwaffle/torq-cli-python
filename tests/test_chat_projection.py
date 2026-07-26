from __future__ import annotations

from typing import Any

from torq_cli.application.chat_projection import reduce_chat_projection


def _receipt(sequence: int, transition: str, **payload: Any) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "event": transition,
        "observed_at": f"2026-07-26T00:00:{sequence:02d}Z",
        "body": payload,
        "verified": True,
    }


def test_unverified_or_tampered_input_exposes_no_transcript() -> None:
    receipts = [
        _receipt(
            1,
            "assistant_message_committed",
            turn_id="turn-1",
            content="must not leak",
        )
    ]

    projection = reduce_chat_projection(
        receipts,
        verification_state="tampered",
        verification_finding="receipt_hash_mismatch",
    )

    assert projection["data_status"] == "unavailable"
    assert projection["verification"] == {
        "state": "tampered",
        "finding": "receipt_hash_mismatch",
    }
    assert projection["messages"] == []
    assert projection["cost"] is None


def test_only_committed_messages_become_durable_and_remain_receipt_ordered() -> None:
    projection = reduce_chat_projection(
        [
            _receipt(
                1,
                "turn_submitted",
                turn_id="turn-1",
                message_id="msg-user",
                role="user",
                content="Build the project",
            ),
            _receipt(2, "output_delta", turn_id="turn-1", content="secret partial"),
            _receipt(3, "turn_started", turn_id="turn-1", worker_pid=123),
            _receipt(
                4,
                "turn_completed",
                turn_id="turn-1",
                message_id="msg-assistant",
                role="assistant",
                content="Done",
            ),
        ],
        verification_state="verified",
    )

    assert [row["role"] for row in projection["messages"]] == ["user", "assistant"]
    assert [row["sequence"] for row in projection["messages"]] == [1, 4]
    assert all(row["durable"] is True for row in projection["messages"])
    assert "secret partial" not in repr(projection)
    assert projection["provisional"] == {"durable": False, "suppressed_events": 1}
    assert projection["active_turn"] is None


def test_active_turn_and_cancellation_states_follow_lifecycle() -> None:
    requested = reduce_chat_projection(
        [
            _receipt(1, "turn_submitted", turn_id="turn-7", role="user", content="go"),
            _receipt(2, "turn_started", turn_id="turn-7", worker_pid=123),
            _receipt(3, "turn_cancellation_requested", turn_id="turn-7"),
        ],
        verification_state="live_catching_up",
    )
    uncertain = reduce_chat_projection(
        [
            _receipt(1, "turn_started", turn_id="turn-7", worker_pid=123),
            _receipt(2, "turn_cancellation_requested", turn_id="turn-7"),
            _receipt(3, "turn_cancellation_uncertain", turn_id="turn-7"),
        ],
        verification_state="verified",
    )
    confirmed = reduce_chat_projection(
        [
            _receipt(1, "turn_started", turn_id="turn-7", worker_pid=123),
            _receipt(2, "turn_cancellation_requested", turn_id="turn-7"),
            _receipt(3, "turn_cancellation_confirmed", turn_id="turn-7"),
        ],
        verification_state="verified",
    )

    assert requested["active_turn"]["state"] == "cancelling"
    assert requested["cancellation"]["state"] == "requested"
    assert uncertain["active_turn"]["state"] == "cancellation_uncertain"
    assert uncertain["cancellation"]["state"] == "uncertain"
    assert confirmed["active_turn"] is None
    assert confirmed["cancellation"]["state"] == "confirmed"


def test_cost_totals_use_exact_decimal_arithmetic() -> None:
    projection = reduce_chat_projection(
        [
            _receipt(
                1,
                "turn_completed",
                turn_id="turn-1",
                role="assistant",
                content="one",
                billed_usd="0.1",
                metered_usd="0.2",
            ),
            _receipt(
                2,
                "turn_completed",
                turn_id="turn-2",
                role="assistant",
                content="two",
                billed_usd="0.2",
                metered_usd="0.1",
            ),
        ],
        verification_state="verified",
    )

    assert projection["cost"] == {
        "status": "available",
        "billed_usd": "0.3",
        "metered_equivalent_usd": "0.3",
    }


def test_unreported_usage_never_projects_authoritative_zero_cost() -> None:
    projection = reduce_chat_projection(
        [
            _receipt(
                1,
                "turn_completed",
                turn_id="turn-1",
                role="assistant",
                content="done",
                usage="unreported",
                billed_usd="0",
                metered_usd=None,
            )
        ],
        verification_state="verified",
    )

    assert projection["cost"] == {
        "status": "partially_available",
        "billed_usd": "0",
        "metered_equivalent_usd": None,
    }


def test_attachment_projection_keeps_metadata_but_never_sensitive_values() -> None:
    projection = reduce_chat_projection(
        [
            _receipt(
                1,
                "user_message_committed",
                turn_id="turn-1",
                content="Inspect this",
                attachments=[
                    {
                        "attachment_id": "attachment-1",
                        "name": r"C:\private\report.pdf",
                        "media_type": "application/pdf",
                        "size_bytes": 42,
                        "sha256": "sha256:" + "a" * 64,
                        "body": "TOP SECRET",
                        "content": "TOP SECRET",
                        "path": r"C:\private\report.pdf",
                        "api_key": "sk-secret",
                        "url": "https://secret.invalid",
                    }
                ],
            )
        ],
        verification_state="verified",
    )

    attachment = projection["messages"][0]["attachments"][0]
    assert attachment == {
        "attachment_id": "attachment-1",
        "name": "report.pdf",
        "media_type": "application/pdf",
        "size_bytes": 42,
        "sha256": "sha256:" + "a" * 64,
    }
    serialized = repr(projection)
    assert "TOP SECRET" not in serialized
    assert "sk-secret" not in serialized
    assert "C:\\private" not in serialized
    assert "secret.invalid" not in serialized


def test_visible_values_are_bounded_and_control_characters_removed() -> None:
    projection = reduce_chat_projection(
        [
            _receipt(
                1,
                "system_message_committed",
                turn_id="turn-1\u202e.exe",
                message_id="msg\x00-1",
                content="safe\x00 text\u202eevil",
                attachments=[{"name": "x" * 200 + ".txt"}],
            )
        ],
        verification_state="verified",
    )

    message = projection["messages"][0]
    assert message["turn_id"] == "turn-1.exe"
    assert message["message_id"] == "msg-1"
    assert message["content"] == "safe text.evil".replace(".", "", 1)
    assert len(message["attachments"][0]["name"]) <= 128
    assert "\u202e" not in repr(projection)
    assert "\x00" not in repr(projection)


def test_malformed_verified_prefix_fails_closed_as_tampered() -> None:
    out_of_order = [
        _receipt(2, "turn_accepted", turn_id="turn-1"),
        _receipt(1, "turn_completed", turn_id="turn-1"),
    ]
    invalid_cost = [_receipt(1, "turn_usage_recorded", billed_usd="NaN")]
    unverified_row = [_receipt(1, "turn_started", turn_id="turn-1", worker_pid=1)]
    unverified_row[0]["verified"] = False

    for receipts in (out_of_order, invalid_cost, unverified_row):
        projection = reduce_chat_projection(receipts, verification_state="verified")
        assert projection["data_status"] == "unavailable"
        assert projection["verification"] == {
            "state": "tampered",
            "finding": "chat_projection_input_invalid",
        }
        assert projection["messages"] == []
