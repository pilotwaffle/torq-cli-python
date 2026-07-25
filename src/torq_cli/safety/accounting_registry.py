"""Durable cross-run entitlement accounting and dispatch enrollment.

The registry is deliberately rooted beside, rather than inside, individual run
directories.  Its signed hash chain is the denominator for account-window
coverage and its separately anchored head detects journal truncation or replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from torq_cli.safety.entitlements import PlanWindow
from torq_cli.safety.receipts import FileRunKeyStore, verify_receipt_store


_ZERO_HASH = "0" * 64


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("entitlement_time_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("entitlement_time_invalid")
    return parsed.astimezone(UTC)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("accounting_anchor_short_write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@dataclass(frozen=True)
class CoverageReport:
    account: str
    resets_at: str
    enrolled: int
    verified: int
    statuses: Mapping[str, str]

    @property
    def complete(self) -> bool:
        return self.enrolled > 0 and self.verified == self.enrolled

    def as_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "resets_at": self.resets_at,
            "eligible_enrolled_runs": self.enrolled,
            "verified_eligible_runs": self.verified,
            "coverage": f"{self.verified}/{self.enrolled}",
            "complete": self.complete,
            "runs": dict(sorted(self.statuses.items())),
        }


class RegistryRollbackError(ValueError):
    """Raised when the journal no longer agrees with its signed anchor."""


class PersistentEntitlementLedger:
    """File-backed accounting broker shared by runs under an evidence root.

    Callers receive accounting operations and projections only. The long-lived
    signing identity remains name-mangled inside this serialization boundary,
    matching the evidence broker's no-key-bearing-client contract.
    """

    def __init__(
        self,
        evidence_root: Path,
        windows: Mapping[str, PlanWindow],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = evidence_root
        self._windows = dict(windows)
        self._providers: dict[str, str] = {}
        for account, window in self._windows.items():
            if account != window.account:
                raise ValueError("entitlement_account_key_mismatch")
            for provider in window.providers:
                if provider in self._providers:
                    raise ValueError(f"entitlement_provider_ambiguous:{provider}")
                self._providers[provider] = account
        self._clock = clock
        self._lock = RLock()
        self._run_id: str | None = None
        keys = FileRunKeyStore(evidence_root)
        self.__private = Ed25519PrivateKey.from_private_bytes(
            keys.get_or_create("accounting-registry")
        )
        public = self.__private.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        self.public_key = public
        self.root_key_id = hashlib.sha256(public).hexdigest()
        self.registry_path = evidence_root / "dispatch-registry.jsonl"
        self.anchor_path = evidence_root / ".torq-dispatch-registry-anchor.json"
        self.reconciliation_path = evidence_root / "entitlement-reconciliation.jsonl"
        self.reconciliation_anchor_path = (
            evidence_root / ".torq-entitlement-reconciliation-anchor.json"
        )

    @classmethod
    def from_config(
        cls,
        evidence_root: Path,
        raw: Mapping[str, object],
        *,
        clock: Callable[[], float] = time.time,
    ) -> PersistentEntitlementLedger:
        from torq_cli.safety.entitlements import InMemoryEntitlementLedger

        memory = InMemoryEntitlementLedger.from_config(raw)
        return cls(evidence_root, memory.windows, clock=clock)

    def bind_run(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("registry_run_id_required")
        self._run_id = run_id
        for window in self._windows.values():
            if window.settlement == "plan_covered":
                self.enroll(run_id, window.account, window.resets_at)

    def _signed(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **body,
            "signer_key_id": self.root_key_id,
            "signature": self.__private.sign(_canonical(body)).hex(),
        }

    def _verify_signed(self, value: Mapping[str, Any]) -> bool:
        body = dict(value)
        try:
            signature = bytes.fromhex(str(body.pop("signature")))
            key_id = str(body.pop("signer_key_id"))
            if key_id != self.root_key_id:
                return False
            self.__private.public_key().verify(signature, _canonical(body))
        except (InvalidSignature, KeyError, ValueError):
            return False
        return True

    @staticmethod
    def _read_lines(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise RegistryRollbackError("registry_record_invalid")
            rows.append(value)
        return rows

    def _verify_journal(self, path: Path, anchor_path: Path) -> list[dict[str, Any]]:
        rows = self._read_lines(path)
        previous = self._verify_rows(rows)
        if rows or anchor_path.exists():
            try:
                anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, UnicodeError) as exc:
                raise RegistryRollbackError("registry_anchor_missing") from exc
            if not isinstance(anchor, dict) or not self._verify_signed(anchor):
                raise RegistryRollbackError("registry_anchor_invalid")
            if anchor.get("count") != len(rows) or anchor.get("head") != previous:
                raise RegistryRollbackError("registry_rollback_detected")
        return rows

    def _verify_rows(self, rows: list[dict[str, Any]]) -> str:
        previous = _ZERO_HASH
        for sequence, row in enumerate(rows, 1):
            if not self._verify_signed(row):
                raise RegistryRollbackError("registry_signature_invalid")
            body = dict(row)
            body.pop("signature")
            body.pop("signer_key_id")
            record_hash = str(body.pop("record_hash", ""))
            if body.get("sequence") != sequence or body.get("previous_hash") != previous:
                raise RegistryRollbackError("registry_chain_invalid")
            if _hash(body) != record_hash:
                raise RegistryRollbackError("registry_hash_invalid")
            previous = record_hash
        return previous

    def _append(
        self,
        path: Path,
        anchor_path: Path,
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._verify_journal(path, anchor_path)
            previous = str(rows[-1]["record_hash"]) if rows else _ZERO_HASH
            body = {
                "sequence": len(rows) + 1,
                "previous_hash": previous,
                "recorded_at": _utc_now(),
                **event,
            }
            record_hash = _hash(body)
            signed = self._signed({**body, "record_hash": record_hash})
            encoded = _canonical(signed) + b"\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("accounting_registry_short_write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            anchor = self._signed(
                {"schema_version": "1.0.0", "head": record_hash, "count": len(rows) + 1}
            )
            _write_atomic(anchor_path, _canonical(anchor))
            return signed

    def verify_registry(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._verify_journal(self.registry_path, self.anchor_path))

    def enroll(
        self,
        run_id: str,
        account: str,
        resets_at: str,
        *,
        root_key_id: str | None = None,
    ) -> None:
        rows = self._verify_journal(self.registry_path, self.anchor_path)
        if any(
            row.get("event") == "run_enrolled"
            and row.get("run_id") == run_id
            and row.get("account") == account
            and row.get("resets_at") == resets_at
            for row in rows
        ):
            return
        self._append(
            self.registry_path,
            self.anchor_path,
            {
                "event": "run_enrolled",
                "entry_id": "enroll-" + uuid.uuid4().hex,
                "account": account,
                "run_id": run_id,
                "root_key_id": root_key_id or self.root_key_id,
                "resets_at": resets_at,
            },
        )

    def _account(self, provider: str) -> str | None:
        return self._providers.get(provider)

    def window(self, provider: str) -> PlanWindow:
        account = self._account(provider)
        if account is None:
            return PlanWindow(
                f"unknown:{provider}", (provider,), "unknown", 0, 0, "unknown"
            )
        base = self._windows[account]
        used, reserved = self._totals(account, base.resets_at)
        return replace(base, used=used, reserved=reserved)

    def coverage(self, provider: str) -> CoverageReport:
        account = self._account(provider)
        if account is None:
            return CoverageReport(f"unknown:{provider}", "unknown", 0, 0, {})
        window = self._windows[account]
        rows = self._verify_journal(self.registry_path, self.anchor_path)
        root_status: dict[str, tuple[str, bytes]] = {
            self.root_key_id: ("active", self.public_key)
        }
        for row in rows:
            if row.get("event") == "root_trust_changed":
                try:
                    public_key = bytes.fromhex(str(row.get("public_key")))
                except ValueError:
                    public_key = b""
                root_status[str(row.get("subject_root_key_id"))] = (
                    str(row.get("trust_status")),
                    public_key,
                )
        enrolled = {
            str(row["run_id"]): row
            for row in rows
            if row.get("event") == "run_enrolled"
            and row.get("account") == account
            and row.get("resets_at") == window.resets_at
        }
        statuses: dict[str, str] = {}
        for run_id, row in enrolled.items():
            trust, trusted_public_key = root_status.get(
                str(row.get("root_key_id")), ("distrusted", b"")
            )
            if trust == "distrusted":
                statuses[run_id] = trust
                continue
            run_root = self.root / run_id
            if not run_root.exists():
                statuses[run_id] = "missing"
                continue
            result = verify_receipt_store(
                run_root,
                trusted_public_key=(trusted_public_key or None),
                external_trust=True,
            )
            statuses[run_id] = (
                trust
                if result.status in {"verified", "live_catching_up"}
                else "unverifiable"
            )
        verified = sum(value in {"active", "trusted_legacy"} for value in statuses.values())
        return CoverageReport(account, window.resets_at, len(enrolled), verified, statuses)

    def set_root_trust(
        self,
        subject_root_key_id: str,
        status: str,
        *,
        actor: str,
        public_key: bytes | None = None,
    ) -> None:
        if status not in {"active", "trusted_legacy", "distrusted"}:
            raise ValueError("registry_root_trust_status_invalid")
        resolved_public_key = public_key or (
            self.public_key if subject_root_key_id == self.root_key_id else None
        )
        if (
            not subject_root_key_id
            or not actor
            or resolved_public_key is None
            or len(resolved_public_key) != 32
            or hashlib.sha256(resolved_public_key).hexdigest() != subject_root_key_id
        ):
            raise ValueError("registry_root_trust_change_invalid")
        self._append(
            self.registry_path,
            self.anchor_path,
            {
                "event": "root_trust_changed",
                "entry_id": "trust-" + uuid.uuid4().hex,
                "subject_root_key_id": subject_root_key_id,
                "trust_status": status,
                "public_key": resolved_public_key.hex(),
                "actor": actor,
            },
        )

    def enroll_legacy(
        self,
        run_id: str,
        account: str,
        resets_at: str,
        *,
        public_key: bytes,
        actor: str,
    ) -> str:
        key_id = hashlib.sha256(public_key).hexdigest()
        self.set_root_trust(
            key_id,
            "trusted_legacy",
            actor=actor,
            public_key=public_key,
        )
        self.enroll(run_id, account, resets_at, root_key_id=key_id)
        return key_id

    def preflight(self, provider: str) -> CoverageReport:
        report = self.coverage(provider)
        if not report.complete:
            raise ValueError(f"entitlement_coverage_incomplete:{report.account}")
        return report

    def reserve(self, provider: str, *, calls: int) -> None:
        if calls <= 0:
            raise ValueError("entitlement_calls_invalid")
        if self._run_id is None:
            raise ValueError("entitlement_run_unbound")
        account = self._account(provider)
        if account is None:
            raise ValueError("entitlement_provider_unknown")
        window = self._windows[account]
        if window.settlement != "plan_covered":
            return
        self.preflight(provider)
        current = self.window(provider)
        if current.used + current.reserved + calls > current.limit:
            raise ValueError("entitlement_window_exceeded")
        self._append(
            self.registry_path,
            self.anchor_path,
            {
                "event": "reservation_created",
                "entry_id": "reserve-" + uuid.uuid4().hex,
                "account": account,
                "run_id": self._run_id,
                "provider": provider,
                "calls": calls,
                "resets_at": window.resets_at,
            },
        )

    def reconcile(self, provider: str, *, calls: int) -> None:
        if calls < 0:
            raise ValueError("entitlement_calls_invalid")
        if self._run_id is None:
            raise ValueError("entitlement_run_unbound")
        account = self._account(provider)
        if account is None:
            raise ValueError("entitlement_provider_unknown")
        window = self._windows[account]
        rows = self._verify_journal(self.registry_path, self.anchor_path)
        reconciled = self._reconciled_ids()
        reservation = next(
            (
                row
                for row in rows
                if row.get("event") == "reservation_created"
                and row.get("account") == account
                and row.get("run_id") == self._run_id
                and row.get("provider") == provider
                and row.get("entry_id") not in reconciled
            ),
            None,
        )
        if reservation is None:
            raise ValueError("entitlement_reservation_missing")
        old_used, _ = self._totals(account, window.resets_at)
        if old_used + calls > window.limit:
            raise ValueError("entitlement_reconcile_invalid")
        self._append(
            self.reconciliation_path,
            self.reconciliation_anchor_path,
            {
                "event": "reservation_reconciled",
                "entry_id": "reconcile-" + uuid.uuid4().hex,
                "account": account,
                "run_id": self._run_id,
                "provider": provider,
                "source": "provider_response",
                "actor": "evidence_broker",
                "old_used": old_used,
                "new_used": old_used + calls,
                "calls": calls,
                "reservation_entry_ids": [reservation["entry_id"]],
                "resets_at": window.resets_at,
            },
        )

    def _reconciled_ids(self) -> set[str]:
        rows = self._verify_journal(
            self.reconciliation_path, self.reconciliation_anchor_path
        )
        return {
            str(entry_id)
            for row in rows
            for entry_id in row.get("reservation_entry_ids", [])
        }

    def _totals(self, account: str, resets_at: str) -> tuple[int, int]:
        registry = self._verify_journal(self.registry_path, self.anchor_path)
        reconciliation = self._verify_journal(
            self.reconciliation_path, self.reconciliation_anchor_path
        )
        reconciled_ids = {
            str(entry_id)
            for row in reconciliation
            if row.get("account") == account and row.get("resets_at") == resets_at
            for entry_id in row.get("reservation_entry_ids", [])
        }
        used = sum(
            int(row.get("calls", 0))
            for row in reconciliation
            if row.get("account") == account and row.get("resets_at") == resets_at
        )
        now = datetime.fromtimestamp(self._clock(), UTC)
        reserved = sum(
            int(row.get("calls", 0))
            for row in registry
            if row.get("event") == "reservation_created"
            and row.get("account") == account
            and row.get("resets_at") == resets_at
            and row.get("entry_id") not in reconciled_ids
            and _instant(str(row["resets_at"])) > now
        )
        return used, reserved

    def accounting_snapshot(self) -> dict[str, Any]:
        registry = self.verify_registry()
        accounts: dict[str, Any] = {}
        for account, base in sorted(self._windows.items()):
            used, reserved = self._totals(account, base.resets_at)
            coverage = self.coverage(base.providers[0])
            accounts[account] = {
                "providers": list(base.providers),
                "settlement": base.settlement,
                "used": used,
                "reserved": reserved,
                "limit": base.limit,
                "resets_at": base.resets_at,
                "used_source": base.used_source,
                "limit_source": base.limit_source,
                "coverage": coverage.as_dict(),
            }
        return {
            "schema": "torq-entitlement-accounting-v1",
            "root_key_id": self.root_key_id,
            "registry": {
                "head": (
                    registry[-1]["record_hash"]
                    if registry
                    else _ZERO_HASH
                ),
                "count": len(registry),
            },
            "accounts": accounts,
        }

    def reanchor(
        self,
        *,
        actor: str,
        operator_confirmation: str,
        quarantine_limit: int = 1000,
    ) -> None:
        """Recover a damaged registry while preserving an explicit quarantine record."""
        if not actor or not operator_confirmation:
            raise ValueError("registry_reanchor_confirmation_required")
        rows = self._read_lines(self.registry_path)
        head = self._verify_rows(rows)
        if len(rows) > quarantine_limit:
            raise ValueError("registry_reanchor_quarantine_limit_exceeded")
        old_anchor: Mapping[str, Any] = {}
        try:
            candidate = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            if isinstance(candidate, Mapping):
                old_anchor = candidate
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            pass
        record = self._signed(
            {
                "schema_version": "1.0.0",
                "event": "registry_reanchored",
                "old_head": old_anchor.get("head"),
                "old_count": old_anchor.get("count"),
                "new_head": head,
                "new_count": len(rows),
                "actor": actor,
                "operator_confirmation": operator_confirmation,
                "recorded_at": _utc_now(),
                "quarantined_entries": len(rows),
            }
        )
        reanchors = self.root / "dispatch-registry-reanchors.jsonl"
        encoded = _canonical(record) + b"\n"
        descriptor = os.open(
            reanchors,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("registry_reanchor_short_write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        anchor = self._signed(
            {"schema_version": "1.0.0", "head": head, "count": len(rows)}
        )
        _write_atomic(self.anchor_path, _canonical(anchor))


__all__ = [
    "CoverageReport",
    "PersistentEntitlementLedger",
    "RegistryRollbackError",
]
