"""Shared per-user primary transaction marker under a kernel-held lease."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.domain.hermetic import LegacyConfigTooLarge, LegacyConfigUnreadable, ProtectedPathError, read_bounded_legacy_config
from torq_cli.safety.primary_transaction import AnchoredPrimary
from torq_cli.safety.receipts import (
    fsync_directory,
    restrict_owner_only_directory,
    restrict_owner_only_file,
    signing_file_permissions_are_restricted,
)
from torq_cli.safety.task_workspace import canonical_json, validate_disjoint_roots

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HASH = re.compile(r"sha256:[a-f0-9]{64}")


class PrimaryMarkerAuthority:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        validate_disjoint_roots([self.root])
        self.root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(self.root)
        validate_disjoint_roots([self.root])

    def marker_path(self, primary: AnchoredPrimary) -> Path:
        return self.root / (primary.identity.digest().removeprefix("sha256:") + ".json")

    def read(self, primary: AnchoredPrimary) -> dict[str, Any] | None:
        path = self.marker_path(primary)
        if not os.path.lexists(path):
            return None
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_nlink != 1
            or metadata.st_size > 16_384
            or not signing_file_permissions_are_restricted(path)
        ):
            raise ValueError("task_apply_marker_unsafe")
        try:
            raw = read_bounded_legacy_config(str(path))
        except (LegacyConfigTooLarge, LegacyConfigUnreadable, ProtectedPathError) as exc:
            raise ValueError("task_apply_marker_unsafe") from exc
        after = path.stat(follow_symlinks=False)
        if (
            (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(raw) != metadata.st_size
        ):
            raise ValueError("task_apply_marker_changed")
        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, child in pairs:
                if key in result:
                    raise ValueError("task_apply_marker_invalid")
                result[key] = child
            return result

        def reject(_value: str) -> object:
            raise ValueError("task_apply_marker_invalid")
        try:
            value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
        except json.JSONDecodeError as exc:
            raise ValueError("task_apply_marker_invalid") from exc
        keys = {
            "schema", "primary_identity_hash", "installation_id", "application_id",
            "authority_locator", "journal_hash", "phase", "revision",
        }
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or value.get("schema") != "torq-primary-pending-v1"
            or value.get("primary_identity_hash") != primary.identity.digest()
            or value.get("phase") not in {"reserved", "prepared", "started", "recovering", "terminal"}
            or not isinstance(value.get("installation_id"), str)
            or _ID.fullmatch(value["installation_id"]) is None
            or not isinstance(value.get("application_id"), str)
            or _ID.fullmatch(value["application_id"]) is None
            or not isinstance(value.get("authority_locator"), str)
            or _ID.fullmatch(value["authority_locator"]) is None
            or not isinstance(value.get("revision"), int)
            or isinstance(value.get("revision"), bool)
            or value["revision"] <= 0
            or not (
                value.get("journal_hash") is None and value.get("phase") == "reserved"
                or isinstance(value.get("journal_hash"), str)
                and _HASH.fullmatch(value["journal_hash"]) is not None
                and value.get("phase") != "reserved"
            )
        ):
            raise ValueError("task_apply_marker_invalid")
        return value

    def assert_clear(self, project_root: Path) -> None:
        with AnchoredPrimary(project_root) as primary:
            if self.read(primary) is not None:
                raise ValueError("task_apply_recovery_pending")

    def write(self, primary: AnchoredPrimary, value: Mapping[str, Any]) -> dict[str, Any]:
        path = self.marker_path(primary)
        current = self.read(primary)
        revision = 1 if current is None else int(current["revision"]) + 1
        record = dict(value)
        record["revision"] = revision
        # Validate the exact outbound shape before it becomes a durable block.
        if (
            set(record) != {
                "schema", "primary_identity_hash", "installation_id", "application_id",
                "authority_locator", "journal_hash", "phase", "revision",
            }
            or record.get("schema") != "torq-primary-pending-v1"
            or record.get("primary_identity_hash") != primary.identity.digest()
            or not isinstance(record.get("installation_id"), str)
            or _ID.fullmatch(record["installation_id"]) is None
            or not isinstance(record.get("application_id"), str)
            or _ID.fullmatch(record["application_id"]) is None
            or not isinstance(record.get("authority_locator"), str)
            or _ID.fullmatch(record["authority_locator"]) is None
            or record.get("phase") not in {"reserved", "prepared", "started", "recovering", "terminal"}
            or not (
                record.get("journal_hash") is None and record.get("phase") == "reserved"
                or isinstance(record.get("journal_hash"), str)
                and _HASH.fullmatch(record["journal_hash"]) is not None
                and record.get("phase") != "reserved"
            )
        ):
            raise ValueError("task_apply_marker_invalid")
        if current is not None and (
            current.get("installation_id") != record.get("installation_id")
            or current.get("application_id") != record.get("application_id")
        ):
            raise ValueError("task_apply_marker_owned_elsewhere")
        temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        temporary.write_text(canonical_json(record), encoding="utf-8", newline="\n")
        restrict_owner_only_file(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        restrict_owner_only_file(path)
        fsync_directory(self.root)
        return record

    def clear(self, primary: AnchoredPrimary, *, installation_id: str, application_id: str) -> None:
        path = self.marker_path(primary)
        current = self.read(primary)
        if current is None:
            return
        if current.get("installation_id") != installation_id or current.get("application_id") != application_id:
            raise ValueError("task_apply_marker_owned_elsewhere")
        path.unlink()
        fsync_directory(self.root)


__all__ = ["PrimaryMarkerAuthority"]
