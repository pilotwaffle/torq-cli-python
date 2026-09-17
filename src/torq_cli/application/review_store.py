"""Separate durable state for candidate review and application requests."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.safety.receipts import (
    fsync_directory,
    restrict_owner_only_directory,
    restrict_owner_only_file,
    signing_file_permissions_are_restricted,
)
from torq_cli.safety.task_workspace import canonical_json, digest_bytes, digest_json
from torq_cli.safety.task_workspace import validate_disjoint_roots

_MAX_STORE = 8_388_608


class ReviewStore:
    """Closed v1 store; P2's TaskStore schema remains untouched."""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root.absolute()
        validate_disjoint_roots([self.root])
        self.root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(self.root)
        validate_disjoint_roots([self.root])
        self.path = self.root / "candidate-review.json"
        self._lock = threading.RLock()
        if not os.path.lexists(self.path):
            self._write(
                {
                    "schema": "torq-candidate-review-store-v1",
                    "installation_id": "installation-" + secrets.token_hex(16),
                    "next_ordinal": 1,
                    "corrections": {},
                    "child_plans": {},
                    "review_requests": {},
                    "decisions": {},
                    "apply_requests": {},
                    "applications": {},
                }
            )

    def _read(self) -> dict[str, Any]:
        metadata = self.path.stat(follow_symlinks=False)
        if (
            not self.path.is_file()
            or self.path.is_symlink()
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_STORE
            or not signing_file_permissions_are_restricted(self.path)
        ):
            raise ValueError("candidate_review_store_unsafe")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > _MAX_STORE
                or (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise ValueError("candidate_review_store_unsafe")
            raw = bytearray()
            while len(raw) <= _MAX_STORE:
                chunk = os.read(descriptor, min(65_536, _MAX_STORE + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_STORE or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("candidate_review_store_changed")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, child in pairs:
                if key in value:
                    raise ValueError("candidate_review_store_duplicate_key")
                value[key] = child
            return value

        def reject(_value: str) -> object:
            raise ValueError("candidate_review_store_non_finite")

        value = json.loads(bytes(raw), object_pairs_hook=unique, parse_constant=reject)
        keys = {
            "schema", "installation_id", "next_ordinal", "corrections", "child_plans",
            "review_requests", "decisions", "apply_requests", "applications",
        }
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or value.get("schema") != "torq-candidate-review-store-v1"
            or not isinstance(value.get("installation_id"), str)
            or not isinstance(value.get("next_ordinal"), int)
            or isinstance(value.get("next_ordinal"), bool)
            or value["next_ordinal"] <= 0
            or not all(isinstance(value.get(key), dict) for key in keys - {"schema", "installation_id", "next_ordinal"})
        ):
            raise ValueError("candidate_review_store_invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        temporary.write_text(canonical_json(value), encoding="utf-8", newline="\n")
        restrict_owner_only_file(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        restrict_owner_only_file(self.path)
        fsync_directory(self.root)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    @property
    def installation_id(self) -> str:
        return str(self.snapshot()["installation_id"])

    def correction(self, task_id: str, ready_sequence: int) -> dict[str, Any] | None:
        key = f"{task_id}:{ready_sequence}"
        with self._lock:
            value = self._read()["corrections"].get(key)
            return None if value is None else dict(value)

    def save_correction(
        self, task_id: str, ready_sequence: int, *, text: str, expected_revision: int
    ) -> dict[str, Any]:
        if not text.strip() or len(text) > 16_384 or "\x00" in text:
            raise ValueError("candidate_correction_invalid")
        key = f"{task_id}:{ready_sequence}"
        with self._lock:
            data = self._read()
            current = data["corrections"].get(key)
            revision = 0 if current is None else current["revision"]
            if revision != expected_revision:
                raise ValueError("candidate_correction_revision_conflict")
            value = {
                "task_id": task_id, "ready_sequence": ready_sequence, "revision": revision + 1,
                "text": text, "content_hash": digest_bytes(text.encode("utf-8")),
            }
            data["corrections"][key] = value
            self._write(data)
            return dict(value)

    def reserve(
        self,
        *,
        request_kind: str,
        request_id: str,
        body: Mapping[str, Any],
        id_prefix: str,
    ) -> tuple[dict[str, Any], bool]:
        if request_kind not in {"review", "apply", "child", "continuation"} or not request_id or len(request_id) > 128:
            raise ValueError("candidate_request_invalid")
        digest = digest_json(body)
        request_key = "apply_requests" if request_kind == "apply" else "review_requests"
        record_key = "applications" if request_kind == "apply" else "decisions"
        with self._lock:
            data = self._read()
            prior = data[request_key].get(request_id)
            if prior is not None:
                if prior.get("request_digest") != digest or prior.get("kind") != request_kind:
                    raise ValueError("candidate_request_replay_conflict")
                return dict(data[record_key][prior["record_id"]]), False
            record_id = id_prefix + secrets.token_hex(12)
            record = {
                "record_id": record_id,
                "kind": request_kind,
                "request_id": request_id,
                "request_digest": digest,
                "request": dict(body),
                "ordinal": data["next_ordinal"],
                "state": "reserved",
                "finding": None,
                "result": None,
            }
            data["next_ordinal"] += 1
            data[request_key][request_id] = {"kind": request_kind, "request_digest": digest, "record_id": record_id}
            data[record_key][record_id] = record
            self._write(data)
            return dict(record), True

    def update(self, record_id: str, *, application: bool, **changes: object) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            records = data["applications" if application else "decisions"]
            current = records.get(record_id)
            if not isinstance(current, dict):
                raise ValueError("candidate_record_unknown")
            current.update(changes)
            self._write(data)
            return dict(current)

    def replay(self, *, request_kind: str, request_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        request_key = "apply_requests" if request_kind == "apply" else "review_requests"
        record_key = "applications" if request_kind == "apply" else "decisions"
        expected = digest_json(body)
        with self._lock:
            data = self._read()
            prior = data[request_key].get(request_id)
            if (
                not isinstance(prior, dict)
                or prior.get("kind") != request_kind
                or prior.get("request_digest") != expected
                or not isinstance(prior.get("record_id"), str)
            ):
                raise ValueError("candidate_request_replay_missing")
            record = data[record_key].get(prior["record_id"])
            if not isinstance(record, dict):
                raise ValueError("candidate_review_store_invalid")
            return dict(record)

    def save_child_plan(self, plan_hash: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            data = self._read()
            existing = data["child_plans"].get(plan_hash)
            if existing is not None and existing != dict(value):
                raise ValueError("candidate_child_plan_hash_collision")
            data["child_plans"][plan_hash] = dict(value)
            self._write(data)

    def child_plan(self, plan_hash: str) -> dict[str, Any]:
        with self._lock:
            value = self._read()["child_plans"].get(plan_hash)
            if not isinstance(value, dict):
                raise ValueError("candidate_child_plan_unknown")
            return dict(value)

    def save_continuation_context(
        self, project_id: str, draft_revision: int, value: Mapping[str, Any]
    ) -> None:
        key = f"continuation:{project_id}:{draft_revision}"
        with self._lock:
            data = self._read()
            existing = data["child_plans"].get(key)
            if existing is not None and existing != dict(value):
                raise ValueError("candidate_continuation_context_conflict")
            data["child_plans"][key] = dict(value)
            self._write(data)

    def continuation_context(
        self, project_id: str, draft_revision: int
    ) -> dict[str, Any] | None:
        key = f"continuation:{project_id}:{draft_revision}"
        with self._lock:
            value = self._read()["child_plans"].get(key)
            return dict(value) if isinstance(value, dict) else None

    def set_active_continuation(
        self, project_id: str, value: Mapping[str, Any] | None
    ) -> None:
        key = f"continuation-active:{project_id}"
        with self._lock:
            data = self._read()
            if value is None:
                data["child_plans"].pop(key, None)
            else:
                data["child_plans"][key] = dict(value)
            self._write(data)

    def active_continuation(self, project_id: str) -> dict[str, Any] | None:
        key = f"continuation-active:{project_id}"
        with self._lock:
            value = self._read()["child_plans"].get(key)
            return dict(value) if isinstance(value, dict) else None


__all__ = ["ReviewStore"]
