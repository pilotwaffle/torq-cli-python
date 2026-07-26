"""Signed, hash-chained durable evidence for governed chat turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from torq_cli.core.canonical_json import canonical_json
from torq_cli.safety.receipts import (
    _restrict_private_key,
    _restrict_signing_directory,
    verify_receipt_store,
)

_CHAT_EVENTS = frozenset(
    {
        "turn_submitted",
        "turn_started",
        "turn_cancellation_requested",
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "turn_cancellation_uncertain",
    }
)
_CHAT_FILE = "chat-receipts.jsonl"
_CHAT_HEAD_DIRECTORY = ".torq-chat-heads"
_CHAT_HEAD_SCHEMA = "torq-chat-head-v1"
_CHAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_CHAT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/markdown",
        "text/plain",
    }
)
_TERMINAL = frozenset(
    {"turn_completed", "turn_failed", "turn_cancelled", "turn_cancellation_uncertain"}
)


def _path_is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse)


def _validate_regular_file(info: os.stat_result, finding: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _path_is_reparse(info):
        raise ValueError(finding)


def _open_secure_file(path: Path, flags: int, mode: int, finding: str) -> int:
    """Open a single-link regular file without following a substituted path."""
    binary = getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL | binary | nofollow, mode)
    except FileExistsError:
        before = os.lstat(path)
        _validate_regular_file(before, finding)
        descriptor = os.open(path, flags | binary | nofollow, mode)
        try:
            after = os.fstat(descriptor)
            _validate_regular_file(after, finding)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError(finding)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


def _open_secure_existing(path: Path, flags: int, mode: int, finding: str) -> int:
    before = os.lstat(path)
    _validate_regular_file(before, finding)
    descriptor = os.open(
        path,
        flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        after = os.fstat(descriptor)
        _validate_regular_file(after, finding)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(finding)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_secure_bytes(path: Path, finding: str) -> bytes:
    descriptor = _open_secure_existing(path, os.O_RDONLY, 0o600, finding)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


@contextmanager
def _journal_lock(run_root: Path) -> Iterator[None]:
    directory = run_root.parent / _CHAT_HEAD_DIRECTORY / run_root.name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_signing_directory(directory, "chat_lock_directory_unsafe")
    path = directory / "writer.lock"
    descriptor = _open_secure_file(path, os.O_RDWR, 0o600, "chat_lock_path_unsafe")
    _restrict_private_key(path)
    try:
        if os.path.getsize(path) == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)  # type: ignore[attr-defined]
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            os.close(descriptor)


class ChatEvidenceJournal:
    """Append chat evidence signed by the run's certified operator identity."""

    def __init__(self, run_root: Path, operator_private_key: bytes) -> None:
        if len(operator_private_key) != 32:
            raise ValueError("chat_signing_key_invalid")
        self.run_root = run_root.resolve()
        self.run_id = self.run_root.name
        self.path = self.run_root / _CHAT_FILE
        self._private = Ed25519PrivateKey.from_private_bytes(operator_private_key)
        self._lock = threading.RLock()
        certified = _certified_operator_key(self.run_root)
        if self._private.public_key().public_bytes_raw() != certified:
            raise ValueError("chat_signing_identity_mismatch")
        with _journal_lock(self.run_root):
            self._rows = list(_verify_chat_evidence_unlocked(self.run_root))
            if self._rows:
                _write_external_head(
                    self.run_root,
                    sequence=self._rows[-1]["sequence"],
                    digest=str(self._rows[-1]["hash"]),
                    private=self._private,
                )

    def append(self, event: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate, sign, fsync, and return one durable event."""
        if event not in _CHAT_EVENTS or not isinstance(body, Mapping):
            raise ValueError("chat_evidence_event_invalid")
        sanitized = json.loads(canonical_json(dict(body)))
        if not isinstance(sanitized, dict):
            raise ValueError("chat_evidence_body_invalid")
        with self._lock:
            with _journal_lock(self.run_root):
                current = list(_verify_chat_evidence_unlocked(self.run_root))
                if current != self._rows:
                    raise ValueError("chat_evidence_external_change")
                if current:
                    _write_external_head(
                        self.run_root,
                        sequence=current[-1]["sequence"],
                        digest=str(current[-1]["hash"]),
                        private=self._private,
                    )
                _validate_next(current, event, sanitized)
                unsigned: dict[str, Any] = {
                    "schema": "torq-chat-evidence-v1",
                    "run_id": self.run_id,
                    "sequence": len(current) + 1,
                    "event": event,
                    "previous_hash": None if not current else current[-1]["hash"],
                    "body": sanitized,
                }
                digest = "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest()
                signed = {
                    **unsigned,
                    "hash": digest,
                    "signature": self._private.sign(canonical_json(unsigned)).hex(),
                }
                encoded = canonical_json(signed) + b"\n"
                descriptor = _open_secure_file(
                    self.path,
                    os.O_APPEND | os.O_WRONLY,
                    0o600,
                    "chat_evidence_path_unsafe",
                )
                try:
                    _restrict_private_key(self.path)
                    if os.write(descriptor, encoded) != len(encoded):
                        raise OSError("chat_evidence_short_write")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _write_external_head(
                    self.run_root,
                    sequence=signed["sequence"],
                    digest=digest,
                    private=self._private,
                )
                self._rows.append(signed)
                return signed

    def rows(self) -> tuple[Mapping[str, Any], ...]:
        """Return a freshly verified immutable evidence view."""
        with self._lock:
            with _journal_lock(self.run_root):
                return tuple(_verify_chat_evidence_unlocked(self.run_root))


def _certified_operator_key(run_root: Path) -> bytes:
    verification = verify_receipt_store(run_root)
    if verification.status not in {"verified", "live_catching_up"}:
        raise ValueError("chat_run_evidence_untrusted")
    try:
        certificate = json.loads((run_root / "run-certificate.json").read_bytes())
        writers = certificate["writers"]
        operator = writers["operator_gateway"]
        public = bytes.fromhex(str(operator["public_key"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("chat_certificate_invalid") from exc
    if len(public) != 32:
        raise ValueError("chat_certificate_invalid")
    return public


def _validate_next(rows: list[dict[str, Any]], event: str, body: dict[str, Any]) -> None:
    turn_id = body.get("turn_id")
    if not isinstance(turn_id, str) or _CHAT_ID.fullmatch(turn_id) is None:
        raise ValueError("chat_turn_id_invalid")
    allowed: dict[str, frozenset[str]] = {
        "turn_submitted": frozenset({"turn_id", "role", "content", "attachments"}),
        "turn_started": frozenset({"turn_id", "worker_pid", "context_hash"}),
        "turn_cancellation_requested": frozenset({"turn_id"}),
        "turn_completed": frozenset(
            {
                "turn_id",
                "role",
                "content",
                "usage",
                "provider",
                "model",
                "settlement",
                "billed_usd",
                "metered_usd",
                "pricing_status",
                "rate_table_version",
                "rate_table_hash",
            }
        ),
        "turn_failed": frozenset({"turn_id", "reason", "returncode"}),
        "turn_cancelled": frozenset({"turn_id", "returncode", "forced", "containment_state"}),
        "turn_cancellation_uncertain": frozenset(
            {"turn_id", "returncode", "forced", "containment_state", "reason"}
        ),
    }
    if set(body) - allowed[event]:
        raise ValueError("chat_evidence_body_field_forbidden")
    if event == "turn_submitted":
        content = body.get("content")
        attachments = body.get("attachments")
        if (
            set(body) != {"turn_id", "role", "content", "attachments"}
            or body.get("role") != "user"
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 65_536
            or not isinstance(attachments, list)
            or len(attachments) > 6
        ):
            raise ValueError("chat_evidence_body_invalid")
        for item in attachments:
            if (
                not isinstance(item, dict)
                or set(item) != {"attachment_id", "name", "media_type", "size_bytes", "sha256"}
                or not isinstance(item.get("name"), str)
                or not item["name"]
                or len(item["name"]) > 255
                or any(character in item["name"] for character in ("/", "\\", "\x00"))
                or item.get("media_type") not in _CHAT_MEDIA_TYPES
                or not isinstance(item.get("size_bytes"), int)
                or isinstance(item.get("size_bytes"), bool)
                or not 0 < item["size_bytes"] <= 5 * 1024 * 1024
                or not isinstance(item.get("attachment_id"), str)
                or _CHAT_ID.fullmatch(item["attachment_id"]) is None
                or not isinstance(item.get("sha256"), str)
                or _SHA256.fullmatch(item["sha256"]) is None
            ):
                raise ValueError("chat_evidence_attachment_invalid")
    elif event == "turn_started":
        worker_pid = body.get("worker_pid")
        context_hash = body.get("context_hash")
        if (
            set(body)
            not in (
                {"turn_id", "worker_pid"},
                {"turn_id", "worker_pid", "context_hash"},
            )
            or not isinstance(worker_pid, int)
            or isinstance(worker_pid, bool)
            or worker_pid <= 0
            or context_hash is not None
            and (
                not isinstance(context_hash, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", context_hash) is None
            )
        ):
            raise ValueError("chat_evidence_body_invalid")
    elif event == "turn_completed":
        content = body.get("content")
        if (
            body.get("role") != "assistant"
            or not isinstance(content, str)
            or len(content) > 1_048_576
        ):
            raise ValueError("chat_evidence_body_invalid")
        if "usage" in body:
            usage = body.get("usage")
            if usage != "unreported" and (
                not isinstance(usage, dict)
                or set(usage) != {"input_tokens", "output_tokens", "reasoning_tokens"}
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                    for value in usage.values()
                )
            ):
                raise ValueError("chat_evidence_usage_invalid")
            for key in ("provider", "model", "settlement", "pricing_status"):
                if not isinstance(body.get(key), str) or _CHAT_ID.fullmatch(body[key]) is None:
                    raise ValueError("chat_evidence_accounting_invalid")
            for key in ("billed_usd", "metered_usd"):
                value = body.get(key)
                if value is not None and (
                    not isinstance(value, str) or _MONEY.fullmatch(value) is None
                ):
                    raise ValueError("chat_evidence_accounting_invalid")
            version = body.get("rate_table_version")
            digest = body.get("rate_table_hash")
            if version is not None and not isinstance(version, str):
                raise ValueError("chat_evidence_accounting_invalid")
            if digest is not None and (
                not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValueError("chat_evidence_accounting_invalid")
    elif event == "turn_failed":
        reason = body.get("reason")
        returncode = body.get("returncode")
        if (
            not isinstance(reason, str)
            or _CHAT_ID.fullmatch(reason) is None
            or returncode is not None
            and (not isinstance(returncode, int) or isinstance(returncode, bool))
        ):
            raise ValueError("chat_evidence_body_invalid")
    elif event in {"turn_cancelled", "turn_cancellation_uncertain"}:
        returncode = body.get("returncode")
        forced = body.get("forced")
        containment = body.get("containment_state")
        recovery = event == "turn_cancellation_uncertain" and body.get("reason") in {
            "coordinator_restarted",
            "termination_observation_failed",
        }
        if recovery:
            if set(body) != {"turn_id", "reason"}:
                raise ValueError("chat_evidence_body_invalid")
        elif (
            returncode is not None
            and (not isinstance(returncode, int) or isinstance(returncode, bool))
            or event == "turn_cancelled"
            and returncode is None
            or not isinstance(forced, bool)
            or not isinstance(containment, str)
            or event == "turn_cancelled"
            and containment != "known_empty"
            or event == "turn_cancellation_uncertain"
            and containment == "known_empty"
        ):
            raise ValueError("chat_evidence_body_invalid")

    active_turn: str | None = None
    active_events: list[str] = []
    for row in rows:
        row_event = str(row["event"])
        row_body = row["body"]
        if row_event == "turn_submitted":
            active_turn = str(row_body["turn_id"])
            active_events = [row_event]
        elif active_turn == row_body.get("turn_id"):
            active_events.append(row_event)
            if row_event in _TERMINAL:
                active_turn = None
                active_events = []
    if event == "turn_submitted":
        if active_turn is not None:
            raise ValueError("chat_turn_active")
        if any(row["body"].get("turn_id") == turn_id for row in rows):
            raise ValueError("chat_turn_id_reused")
        return
    if active_turn != turn_id:
        raise ValueError("chat_turn_not_active")
    previous = active_events[-1]
    if event == "turn_started" and previous != "turn_submitted":
        raise ValueError("chat_transition_invalid")
    if event == "turn_cancellation_requested" and previous != "turn_started":
        raise ValueError("chat_transition_invalid")
    if event == "turn_completed" and previous != "turn_started":
        raise ValueError("chat_transition_invalid")
    if event == "turn_cancelled" and previous != "turn_cancellation_requested":
        raise ValueError("chat_transition_invalid")
    if event == "turn_cancellation_uncertain" and previous not in {
        "turn_submitted",
        "turn_started",
        "turn_cancellation_requested",
    }:
        raise ValueError("chat_transition_invalid")
    if event == "turn_failed" and previous not in {"turn_submitted", "turn_started"}:
        raise ValueError("chat_transition_invalid")


def _head_path(run_root: Path) -> Path:
    return run_root.parent / _CHAT_HEAD_DIRECTORY / run_root.name / "head.v1.json"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_signing_directory(path.parent, "chat_head_directory_unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _restrict_private_key(temporary)
        if os.write(descriptor, content) != len(content):
            raise OSError("chat_head_short_write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _restrict_private_key(path)


def _write_external_head(
    run_root: Path,
    *,
    sequence: object,
    digest: str,
    private: Ed25519PrivateKey,
) -> None:
    body = {
        "schema": _CHAT_HEAD_SCHEMA,
        "run_id": run_root.name,
        "sequence": sequence,
        "hash": digest,
    }
    value = {**body, "signature": private.sign(canonical_json(body)).hex()}
    _atomic_write(_head_path(run_root), canonical_json(value) + b"\n")


def _verify_external_head(
    run_root: Path,
    rows: list[dict[str, Any]],
    verifier: Ed25519PublicKey,
) -> None:
    path = _head_path(run_root)
    if not rows:
        if path.exists():
            raise ValueError("chat_evidence_rollback_detected")
        return
    try:
        head = json.loads(_read_secure_bytes(path, "chat_head_path_unsafe"))
        if not isinstance(head, dict):
            raise ValueError("chat_head_invalid")
        signature = bytes.fromhex(str(head.pop("signature")))
        if head.get("schema") != _CHAT_HEAD_SCHEMA or head.get("run_id") != run_root.name:
            raise ValueError("chat_evidence_head_invalid")
        verifier.verify(signature, canonical_json(head))
        latest_matches = (
            head.get("sequence") == rows[-1]["sequence"] and head.get("hash") == rows[-1]["hash"]
        )
        recoverable_lag = (
            len(rows) >= 2
            and head.get("sequence") == rows[-2]["sequence"]
            and head.get("hash") == rows[-2]["hash"]
        )
        if not latest_matches and not recoverable_lag:
            raise ValueError("chat_evidence_rollback_detected")
    except FileNotFoundError as exc:
        if len(rows) == 1:
            return
        raise ValueError("chat_evidence_head_missing") from exc
    except InvalidSignature as exc:
        raise ValueError("chat_evidence_head_untrusted") from exc
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("chat_"):
            raise
        raise ValueError("chat_evidence_head_invalid") from exc


def _verify_chat_evidence_unlocked(run_root: Path) -> tuple[dict[str, Any], ...]:
    """Verify the complete chat chain against the certified run identity."""
    resolved_root = run_root.resolve()
    path = resolved_root / _CHAT_FILE
    public = _certified_operator_key(resolved_root)
    verifier = Ed25519PublicKey.from_public_bytes(public)
    if not path.exists():
        _verify_external_head(resolved_root, [], verifier)
        return ()
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    try:
        content = _read_secure_bytes(path, "chat_evidence_path_unsafe").decode("utf-8")
        for expected, line in enumerate(content.splitlines(), 1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("chat_evidence_invalid")
            signature = bytes.fromhex(str(value.pop("signature")))
            digest = str(value.pop("hash"))
            if (
                value.get("schema") != "torq-chat-evidence-v1"
                or value.get("run_id") != run_root.name
                or value.get("sequence") != expected
                or value.get("event") not in _CHAT_EVENTS
                or value.get("previous_hash") != previous
                or not isinstance(value.get("body"), dict)
            ):
                raise ValueError("chat_evidence_invalid")
            expected_hash = "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()
            if digest != expected_hash:
                raise ValueError("chat_evidence_hash_invalid")
            verifier.verify(signature, canonical_json(value))
            restored = {**value, "hash": digest, "signature": signature.hex()}
            _validate_next(rows, str(value["event"]), dict(value["body"]))
            rows.append(restored)
            previous = digest
    except InvalidSignature as exc:
        raise ValueError("chat_evidence_signature_invalid") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("chat_evidence_invalid") from exc
    _verify_external_head(resolved_root, rows, verifier)
    return tuple(rows)


def verify_chat_evidence(run_root: Path) -> tuple[dict[str, Any], ...]:
    """Verify chat evidence while excluding concurrent writers."""
    resolved = run_root.resolve()
    with _journal_lock(resolved):
        return _verify_chat_evidence_unlocked(resolved)


__all__ = ["ChatEvidenceJournal", "verify_chat_evidence"]
