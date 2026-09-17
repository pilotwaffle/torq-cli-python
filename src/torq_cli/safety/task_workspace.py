"""Bounded, link-intolerant source snapshots for candidate tasks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from torq_cli.core.redaction import PatternRegistry
from torq_cli.domain.hermetic import (
    LegacyConfigTooLarge,
    LegacyConfigUnreadable,
    ProtectedPathError,
    read_bounded_legacy_config,
)
from torq_cli.safety.receipts import restrict_owner_only_directory, restrict_owner_only_file

MAX_TASK_FILES = 32
MAX_TASK_FILE_BYTES = 65_536
MAX_TASK_TOTAL_BYTES = 2_097_152
SUPPORTED_SUFFIXES = frozenset({".py", ".json", ".md", ".txt"})
PROTECTED_PARTS = frozenset(
    {
        ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".config",
        "keys", "secrets", "evidence", "credentials", "node_modules", "__pycache__",
    }
)
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_json(value: object) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def validate_relative_path(value: str) -> str:
    if not value or len(value) > 240 or "\\" in value or "\x00" in value:
        raise ValueError("task_path_invalid")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError("task_path_noncanonical")
    path = PurePosixPath(value)
    if value != path.as_posix() or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("task_path_invalid")
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold().rstrip(" .")
        folded = part.casefold()
        if ":" in part or stem in _WINDOWS_DEVICES or folded in PROTECTED_PARTS or folded.startswith(".env."):
            raise ValueError("task_path_protected")
        if part.endswith((" ", ".")):
            raise ValueError("task_path_invalid")
    if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
        raise ValueError("task_path_extension_unsupported")
    return path.as_posix()


def _is_reparse(metadata: os.stat_result) -> bool:
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & reparse)


def _assert_regular_unlinked(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_symlink() or _is_reparse(metadata):
        raise ValueError("task_source_file_unsafe")
    if metadata.st_size > MAX_TASK_FILE_BYTES:
        raise ValueError("task_source_file_too_large")
    return metadata


def _assert_no_linked_ancestor(root: Path, path: Path) -> None:
    current = path
    while True:
        metadata = current.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or current.is_symlink() or _is_reparse(metadata):
            raise ValueError("task_source_linked")
        if current == root:
            return
        if root not in current.parents:
            raise ValueError("task_path_escape")
        current = current.parent


def _assert_root_ancestry_safe(path: Path) -> None:
    current = path.absolute()
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            return
        current = parent
    while True:
        metadata = current.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or current.is_symlink() or _is_reparse(metadata):
            raise ValueError("task_root_linked")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_bounded_regular(path: Path, expected: os.stat_result) -> bytes:
    try:
        raw = read_bounded_legacy_config(str(path.absolute()))
    except LegacyConfigTooLarge as exc:
        raise ValueError("task_source_file_too_large") from exc
    except (LegacyConfigUnreadable, ProtectedPathError) as exc:
        raise ValueError("task_source_file_unsafe") from exc
    after = path.stat(follow_symlinks=False)
    _assert_root_ancestry_safe(path)
    if (
        len(raw) > MAX_TASK_FILE_BYTES
        or after.st_size != expected.st_size
        or after.st_mtime_ns != expected.st_mtime_ns
        or after.st_ino != expected.st_ino
        or after.st_dev != expected.st_dev
    ):
        raise ValueError("task_source_changed")
    return raw


def snapshot_candidate_exact(candidate_root: Path, expected_paths: Sequence[str]) -> SourceSnapshot:
    expected = {validate_relative_path(item) for item in expected_paths}
    found: set[str] = set()
    allowed_directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    pending: list[tuple[Path, str]] = [(candidate_root, "")]
    visited = 0
    while pending:
        directory, prefix = pending.pop()
        _assert_root_ancestry_safe(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_TASK_FILES * 4:
                    raise ValueError("task_candidate_inventory_too_large")
                relative = f"{prefix}/{entry.name}".lstrip("/").replace("\\", "/")
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse(metadata):
                    raise ValueError("task_candidate_inventory_unsafe")
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in allowed_directories:
                        raise ValueError("task_candidate_inventory_extra")
                    pending.append((Path(entry.path), relative))
                elif stat.S_ISREG(metadata.st_mode):
                    found.add(validate_relative_path(relative))
                else:
                    raise ValueError("task_candidate_inventory_unsafe")
    if found != expected:
        raise ValueError("task_candidate_inventory_mismatch")
    return snapshot_source(candidate_root, sorted(found))


def validate_disjoint_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    for root in roots:
        _assert_root_ancestry_safe(root)
    resolved = tuple(root.resolve(strict=False) for root in roots)
    folded = [os.path.normcase(str(root)) for root in resolved]
    if len(folded) != len(set(folded)):
        raise ValueError("task_root_overlap")
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ValueError("task_root_overlap")
    return resolved


def verify_output_scope(
    project_root: Path,
    input_paths: Sequence[str],
    output_paths: Sequence[str],
    *,
    expected_absent: Sequence[str] | None = None,
) -> tuple[str, ...]:
    inputs = {validate_relative_path(item) for item in input_paths}
    outputs = {validate_relative_path(item) for item in output_paths}
    absent: list[str] = []
    for relative in sorted(outputs):
        target = project_root.joinpath(*PurePosixPath(relative).parts)
        if os.path.lexists(target):
            if relative not in inputs:
                raise ValueError("task_output_context_required")
            _assert_no_linked_ancestor(project_root.absolute(), target.absolute())
            _assert_regular_unlinked(target)
        else:
            current = target.parent
            while not os.path.lexists(current):
                current = current.parent
            _assert_root_ancestry_safe(current)
            absent.append(relative)
    result = tuple(absent)
    if expected_absent is not None and result != tuple(sorted(expected_absent)):
        raise ValueError("task_output_scope_changed")
    return result


@dataclass(frozen=True, slots=True)
class SourceEntry:
    path: str
    content: str
    content_bytes: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    entries: tuple[SourceEntry, ...]
    manifest_hash: str

    def bundle(self) -> list[dict[str, object]]:
        return [
            {
                "path": item.path,
                "content": item.content,
                "content_bytes": item.content_bytes,
                "content_hash": item.content_hash,
            }
            for item in self.entries
        ]


def snapshot_source(project_root: Path, paths: Sequence[str]) -> SourceSnapshot:
    root = project_root.absolute()
    if not root.exists() or not root.is_dir():
        raise ValueError("task_project_missing")
    _assert_root_ancestry_safe(root)
    if len(paths) > MAX_TASK_FILES:
        raise ValueError("task_scope_count_invalid")
    safe_paths = [validate_relative_path(item) for item in paths]
    if len({item.casefold() for item in safe_paths}) != len(safe_paths):
        raise ValueError("task_scope_path_collision")
    registry = PatternRegistry.default()
    entries: list[SourceEntry] = []
    total = 0
    for relative in sorted(safe_paths, key=str.casefold):
        target = root.joinpath(*PurePosixPath(relative).parts)
        _assert_no_linked_ancestor(root, target)
        metadata = _assert_regular_unlinked(target)
        raw = _read_bounded_regular(target, metadata)
        if len(raw) != metadata.st_size:
            raise ValueError("task_source_changed")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("task_source_not_utf8") from exc
        scanned, findings = registry.scan(content)
        if findings or scanned != content:
            raise ValueError("task_source_sensitive")
        total += len(raw)
        if total > MAX_TASK_TOTAL_BYTES:
            raise ValueError("task_scope_too_large")
        entries.append(SourceEntry(relative, content, len(raw), digest_bytes(raw)))
    manifest = [{"path": row.path, "content_bytes": row.content_bytes, "content_hash": row.content_hash} for row in entries]
    return SourceSnapshot(tuple(entries), digest_json(manifest))


def materialize_candidate(
    candidate_root: Path,
    base: SourceSnapshot,
    operations: Sequence[Mapping[str, object]],
    *,
    allowed_output_paths: Sequence[str],
) -> SourceSnapshot:
    if candidate_root.exists():
        raise ValueError("task_candidate_exists")
    allowed = {validate_relative_path(item) for item in allowed_output_paths}
    base_by_path = {item.path: item for item in base.entries}
    if not operations or len(operations) > MAX_TASK_FILES:
        raise ValueError("task_operation_count_invalid")
    seen: set[str] = set()
    final = dict(base_by_path)
    registry = PatternRegistry.default()
    for operation in operations:
        if set(operation) != {"operation", "path", "base_hash", "content"}:
            raise ValueError("task_operation_schema_invalid")
        path_value = operation["path"]
        if not isinstance(path_value, str):
            raise ValueError("task_operation_path_invalid")
        relative = validate_relative_path(path_value)
        folded = relative.casefold()
        if folded in seen or relative not in allowed:
            raise ValueError("task_operation_path_invalid")
        seen.add(folded)
        action = operation["operation"]
        current = base_by_path.get(relative)
        base_hash = operation["base_hash"]
        if action == "replace":
            if current is None or base_hash != current.content_hash:
                raise ValueError("task_operation_base_stale")
        elif action == "create":
            if current is not None or base_hash is not None:
                raise ValueError("task_operation_create_invalid")
        else:
            raise ValueError("task_operation_unsupported")
        content = operation["content"]
        if not isinstance(content, str):
            raise ValueError("task_operation_content_invalid")
        raw = content.encode("utf-8")
        if len(raw) > MAX_TASK_FILE_BYTES:
            raise ValueError("task_operation_content_too_large")
        scanned, findings = registry.scan(content)
        if findings or scanned != content:
            raise ValueError("task_candidate_sensitive")
        hashed = digest_bytes(raw)
        if current is not None and hashed == current.content_hash:
            raise ValueError("task_operation_unchanged")
        final[relative] = SourceEntry(relative, content, len(raw), hashed)
    if not seen.issubset({item.casefold() for item in allowed}):
        raise ValueError("task_operation_scope_invalid")
    if len(final) > MAX_TASK_FILES or sum(row.content_bytes for row in final.values()) > MAX_TASK_TOTAL_BYTES:
        raise ValueError("task_candidate_too_large")
    candidate_root.mkdir(parents=True, mode=0o700)
    restrict_owner_only_directory(candidate_root)
    for relative, row in sorted(final.items()):
        target = candidate_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(target.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            raw = row.content.encode("utf-8")
            if os.write(descriptor, raw) != len(raw):
                raise OSError("task_candidate_short_write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        restrict_owner_only_file(target)
    manifest = [{"path": row.path, "content_bytes": row.content_bytes, "content_hash": row.content_hash} for row in sorted(final.values(), key=lambda item: item.path.casefold())]
    return SourceSnapshot(tuple(sorted(final.values(), key=lambda item: item.path.casefold())), digest_json(manifest))


__all__ = [
    "MAX_TASK_FILES",
    "MAX_TASK_FILE_BYTES",
    "MAX_TASK_TOTAL_BYTES",
    "SUPPORTED_SUFFIXES",
    "SourceEntry",
    "SourceSnapshot",
    "canonical_json",
    "digest_bytes",
    "digest_json",
    "materialize_candidate",
    "snapshot_source",
    "snapshot_candidate_exact",
    "validate_disjoint_roots",
    "validate_relative_path",
    "verify_output_scope",
]
