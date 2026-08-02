"""Pure projection of verified chat lifecycle receipts for UI consumers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from typing import Any

_VERIFIED_STATES = frozenset({"verified", "live_catching_up"})
_MESSAGE_TRANSITIONS: Mapping[str, str | None] = {
    "user_message_committed": "user",
    "assistant_message_committed": "assistant",
    "system_message_committed": "system",
    "message_committed": None,
    "chat_message_committed": None,
    "turn_submitted": "user",
    "turn_completed": "assistant",
}
_PROVISIONAL_TRANSITIONS = frozenset(
    {
        "assistant_output_delta",
        "assistant_output_provisional",
        "turn_output_delta",
        "turn_output_provisional",
        "stdout",
        "stderr",
        "output_delta",
    }
)
_COST_TRANSITIONS = frozenset({"turn_usage_recorded", "turn_settled", "usage_recorded"})
_SAFE_ROLES = frozenset({"user", "assistant", "system"})
_MAX_LABEL = 128
_MAX_CONTENT = 1_048_576
_MAX_ATTACHMENTS = 6
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}")


def _unavailable(state: str, finding: str | None) -> dict[str, Any]:
    return {
        "schema": "torq-chat-projection-v1",
        "verification": {"state": state, "finding": finding},
        "data_status": "unavailable",
        "messages": [],
        "active_turn": None,
        "active_turn_id": None,
        "last_sequence": 0,
        "status": "unavailable",
        "cancellation": {
            "state": "unavailable",
            "turn_id": None,
            "requested_sequence": None,
            "terminal_sequence": None,
        },
        "cost": None,
        "provisional": {"durable": False, "suppressed_events": 0},
    }


def _visible_text(value: object, *, limit: int, multiline: bool) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    kept: list[str] = []
    for character in normalized:
        if multiline and character in {"\n", "\t"}:
            kept.append(character)
            continue
        if unicodedata.category(character).startswith("C"):
            continue
        kept.append(character)
    visible = "".join(kept)
    if multiline:
        visible = visible.strip()
    else:
        visible = " ".join(visible.split())
    if not visible:
        return None
    if len(visible) > limit:
        visible = visible[: limit - 1].rstrip() + "…"
    return visible


def _label(value: object) -> str | None:
    return _visible_text(value, limit=_MAX_LABEL, multiline=False)


def _content(payload: Mapping[str, Any]) -> tuple[str, bool] | None:
    for key in ("content", "message", "text"):
        raw = payload.get(key)
        visible = _visible_text(raw, limit=_MAX_CONTENT, multiline=True)
        if visible is not None:
            return visible, isinstance(raw, str) and len(raw.strip()) > _MAX_CONTENT
    return None


def _safe_name(value: object) -> str | None:
    label = _label(value)
    if label is None:
        return None
    # Treat both separators as path syntax even when projecting on the other OS.
    leaf = PurePath(label.replace("\\", "/")).name
    return _label(leaf)


def _attachment_metadata(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    attachments: list[dict[str, Any]] = []
    for raw in value[:_MAX_ATTACHMENTS]:
        if not isinstance(raw, Mapping):
            continue
        attachment_id = _label(raw.get("attachment_id", raw.get("id")))
        name = _safe_name(raw.get("name", raw.get("source_name")))
        media_type = _label(raw.get("media_type"))
        size = raw.get("size_bytes", raw.get("content_bytes"))
        digest = raw.get("sha256", raw.get("hash"))
        row: dict[str, Any] = {}
        if attachment_id is not None:
            row["attachment_id"] = attachment_id
        if name is not None:
            row["name"] = name
        if media_type is not None:
            row["media_type"] = media_type
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            row["size_bytes"] = size
        if isinstance(digest, str) and _SHA256.fullmatch(digest):
            row["sha256"] = digest
        if row:
            attachments.append(row)
    return attachments


def _money(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        raise ValueError("chat_cost_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("chat_cost_invalid") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("chat_cost_invalid")
    return result


def _money_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _sequence(receipt: Mapping[str, Any]) -> int:
    value = receipt.get("sequence")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("chat_receipt_sequence_invalid")
    return value


def _turn_id(payload: Mapping[str, Any]) -> str | None:
    return _label(payload.get("turn_id"))


def reduce_chat_projection(
    receipts: Sequence[Mapping[str, Any]],
    *,
    verification_state: str,
    verification_finding: str | None = None,
) -> dict[str, Any]:
    """Fold an authenticated receipt prefix into a safe, durable transcript.

    The caller must authenticate the receipt store first. ``live_catching_up``
    is accepted only for the already verified covered prefix supplied here;
    provisional stream events are never promoted into durable messages.
    """
    if verification_state not in _VERIFIED_STATES:
        return _unavailable(verification_state, verification_finding)

    messages: list[dict[str, Any]] = []
    seen_message_ids: set[str] = set()
    active_turn: dict[str, Any] | None = None
    cancellation: dict[str, Any] = {
        "state": "none",
        "turn_id": None,
        "requested_sequence": None,
        "terminal_sequence": None,
    }
    billed = Decimal(0)
    metered = Decimal(0)
    cost_seen = False
    billed_known = True
    metered_known = True
    provisional_count = 0
    previous_sequence = 0

    try:
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                raise ValueError("chat_receipt_invalid")
            sequence = _sequence(receipt)
            if sequence <= previous_sequence:
                raise ValueError("chat_receipt_order_invalid")
            previous_sequence = sequence
            row_verified = receipt.get("verified")
            if row_verified is not None and row_verified is not True:
                raise ValueError("chat_receipt_unverified")
            transition_value = receipt.get("event", receipt.get("transition"))
            if not isinstance(transition_value, str):
                raise ValueError("chat_receipt_transition_invalid")
            transition = transition_value
            payload = receipt.get("body", receipt.get("payload"))
            if not isinstance(payload, Mapping):
                raise ValueError("chat_receipt_payload_invalid")
            turn_id = _turn_id(payload)

            if transition in _PROVISIONAL_TRANSITIONS:
                provisional_count += 1
                if active_turn is not None and (
                    turn_id is None or turn_id == active_turn["turn_id"]
                ):
                    active_turn["state"] = "streaming"
                    active_turn["updated_sequence"] = sequence
                continue

            if transition in _MESSAGE_TRANSITIONS:
                role = _MESSAGE_TRANSITIONS[transition]
                if role is None:
                    raw_role = payload.get("role")
                    role = raw_role if isinstance(raw_role, str) else None
                content_result = _content(payload)
                if role not in _SAFE_ROLES or content_result is None:
                    continue
                content, truncated = content_result
                raw_message_id = _label(payload.get("message_id"))
                message_id = raw_message_id or f"receipt-{sequence}"
                if message_id in seen_message_ids:
                    raise ValueError("chat_message_duplicate")
                seen_message_ids.add(message_id)
                messages.append(
                    {
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "role": role,
                        "content": content,
                        "truncated": truncated,
                        "attachments": _attachment_metadata(payload.get("attachments")),
                        "sequence": sequence,
                        "observed_at": _label(receipt.get("observed_at")),
                        "durable": True,
                    }
                )

            if (
                transition
                in {
                    "turn_accepted",
                    "turn_dispatch_started",
                    "turn_started",
                }
                and turn_id
            ):
                state = "accepted" if transition == "turn_accepted" else "running"
                active_turn = {
                    "turn_id": turn_id,
                    "state": state,
                    "started_sequence": sequence,
                    "updated_sequence": sequence,
                }
            elif transition in {
                "turn_cancel_requested",
                "turn_cancellation_requested",
                "cancellation_requested",
            }:
                cancellation = {
                    "state": "requested",
                    "turn_id": turn_id,
                    "requested_sequence": sequence,
                    "terminal_sequence": None,
                }
                if active_turn is not None:
                    active_turn["state"] = "cancelling"
                    active_turn["updated_sequence"] = sequence
            elif transition in {"turn_cancellation_uncertain", "cancellation_uncertain"}:
                cancellation.update(
                    {
                        "state": "uncertain",
                        "turn_id": turn_id or cancellation["turn_id"],
                        "terminal_sequence": sequence,
                    }
                )
                if active_turn is not None:
                    active_turn["state"] = "cancellation_uncertain"
                    active_turn["updated_sequence"] = sequence
            elif transition in {
                "turn_cancelled",
                "turn_cancellation_confirmed",
                "cancellation_confirmed",
            }:
                cancellation.update(
                    {
                        "state": "confirmed",
                        "turn_id": turn_id or cancellation["turn_id"],
                        "terminal_sequence": sequence,
                    }
                )
                active_turn = None
            elif transition in {
                "turn_completed",
                "turn_failed",
                "turn_rejected",
            }:
                active_turn = None

            if transition in _COST_TRANSITIONS or transition == "turn_completed":
                billed_value = payload.get("billed_usd", payload.get("cost_usd"))
                metered_value = payload.get("metered_usd", payload.get("metered_equivalent_usd"))
                if billed_value is not None or metered_value is not None or "usage" in payload:
                    cost_seen = True
                    if billed_value is None:
                        billed_known = False
                    else:
                        billed += _money(billed_value)
                    if metered_value is None:
                        metered_known = False
                    else:
                        metered += _money(metered_value)
    except (TypeError, ValueError):
        return _unavailable("tampered", "chat_projection_input_invalid")

    if active_turn is not None:
        status = str(active_turn["state"])
    elif cancellation["state"] == "uncertain":
        status = "cancellation_uncertain"
    else:
        status = "ready"
    return {
        "schema": "torq-chat-projection-v1",
        "verification": {"state": verification_state, "finding": None},
        "data_status": "available",
        "messages": messages,
        "active_turn": active_turn,
        "active_turn_id": None if active_turn is None else active_turn["turn_id"],
        "last_sequence": previous_sequence,
        "status": status,
        "cancellation": cancellation,
        "cost": {
            "status": (
                "available"
                if cost_seen and billed_known and metered_known
                else "partially_available"
                if cost_seen and (billed_known or metered_known)
                else "unavailable"
            ),
            "billed_usd": _money_text(billed) if cost_seen and billed_known else None,
            "metered_equivalent_usd": (
                _money_text(metered) if cost_seen and metered_known else None
            ),
        },
        "provisional": {
            "durable": False,
            "suppressed_events": provisional_count,
        },
    }


__all__ = ["reduce_chat_projection"]
