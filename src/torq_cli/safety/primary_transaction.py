"""Handle-anchored bounded writes for explicit candidate application."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import Any

from torq_cli.safety.task_workspace import (
    MAX_TASK_FILES,
    MAX_TASK_FILE_BYTES,
    digest_bytes,
    validate_relative_path,
)

_READ_CHUNK = 65_536

if sys.platform != "win32":
    import fcntl

    try:
        _POSIX_DIRECTORY = os.O_DIRECTORY
        _POSIX_NOFOLLOW = os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - platform capability gate
        raise RuntimeError("task_apply_platform_unsupported") from exc

    def _posix_lock(descriptor: int, *, release: bool = False) -> None:
        operation = fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)

    def _posix_xattrs(descriptor: int) -> list[str]:
        reader = getattr(os, "listxattr", None)
        if reader is None:
            raise RuntimeError("task_apply_platform_unsupported")
        return list(reader(descriptor))

    def _posix_apply_policy(descriptor: int, policy: dict[str, object]) -> None:
        mode, uid, gid = (policy.get(key) for key in ("mode", "uid", "gid"))
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (mode, uid, gid)):
            raise ValueError("task_apply_permission_invalid")
        assert isinstance(mode, int) and isinstance(uid, int) and isinstance(gid, int)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
else:
    _POSIX_DIRECTORY = 0
    _POSIX_NOFOLLOW = 0
    def _posix_lock(descriptor: int, *, release: bool = False) -> None:
        del descriptor, release
        raise ValueError("task_apply_platform_unsupported")

    def _posix_xattrs(descriptor: int) -> list[str]:
        del descriptor
        raise ValueError("task_apply_platform_unsupported")

    def _posix_apply_policy(descriptor: int, policy: dict[str, object]) -> None:
        del descriptor, policy
        raise ValueError("task_apply_platform_unsupported")


@dataclass(frozen=True, slots=True)
class PrimaryIdentity:
    platform: str
    volume: int
    file_id: int

    def value(self) -> dict[str, object]:
        return {"platform": self.platform, "volume": self.volume, "file_id": self.file_id}

    def digest(self) -> str:
        raw = f"{self.platform}:{self.volume}:{self.file_id}".encode("ascii")
        return digest_bytes(raw)


@dataclass(frozen=True, slots=True)
class AnchoredFile:
    path: str
    content: bytes | None
    content_hash: str | None
    permission_policy: dict[str, object] | None
    parent_identity: dict[str, int]
    target_identity: dict[str, int] | None


def _bounded_read_fd(descriptor: int) -> bytes:
    result = bytearray()
    while len(result) <= MAX_TASK_FILE_BYTES:
        try:
            chunk = os.read(descriptor, min(_READ_CHUNK, MAX_TASK_FILE_BYTES + 1 - len(result)))
        except InterruptedError:
            continue
        if not chunk:
            break
        result.extend(chunk)
    if len(result) > MAX_TASK_FILE_BYTES:
        raise ValueError("task_apply_file_too_large")
    return bytes(result)


def _write_all_fd(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        try:
            count = os.write(descriptor, view[written:])
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError("anchored write made no progress")
        written += count


def _posix_identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "volume": int(metadata.st_dev),
        "file_id": int(metadata.st_ino),
        "size": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def _posix_parent_identity(metadata: os.stat_result) -> dict[str, int]:
    return {"volume": int(metadata.st_dev), "file_id": int(metadata.st_ino)}


def _posix_stable_identity(metadata: os.stat_result) -> dict[str, int]:
    return {"volume": int(metadata.st_dev), "file_id": int(metadata.st_ino)}


def _win_identity(info: tuple[int, int, int, bool, int, int, int]) -> dict[str, int]:
    return {
        "volume": info[0], "file_id": info[1], "size": info[5], "mtime": info[6]
    }


def _win_parent_identity(info: tuple[int, int, int, bool, int, int, int]) -> dict[str, int]:
    return {"volume": info[0], "file_id": info[1]}


def _win_stable_identity(info: tuple[int, int, int, bool, int, int, int]) -> dict[str, int]:
    return {"volume": info[0], "file_id": info[1]}


class AnchoredPrimary:
    """Hold the physical project root and mutate only handle-relative leaves."""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._root_handle: int | None = None
        self._mutex: int | None = None
        self._closed = False
        if sys.platform == "win32":
            self._open_windows()
        else:
            self._open_posix()

    @property
    def identity(self) -> PrimaryIdentity:
        return self._identity

    def _open_posix(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("task_apply_primary_unsafe")
        flags = os.O_RDONLY | _POSIX_DIRECTORY | _POSIX_NOFOLLOW
        descriptor = os.open("/", flags)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(child)
                    raise ValueError("task_apply_primary_unsafe")
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("task_apply_primary_unsafe")
        try:
            _posix_lock(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise RuntimeError("task_apply_primary_busy") from exc
        self._root_handle = descriptor
        self._identity = PrimaryIdentity("posix", int(metadata.st_dev), int(metadata.st_ino))

    def _open_windows(self) -> None:
        if not self.root.drive or self.root.anchor != self.root.drive + "\\":
            raise ValueError("task_apply_primary_unsafe")
        drive_type = _kernel32().GetDriveTypeW(ctypes.c_wchar_p(self.root.anchor))
        if drive_type == 4:  # DRIVE_REMOTE
            raise ValueError("task_apply_primary_remote_unsupported")
        handle = _win_open_path(
            self.root.anchor, _GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
            _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            for part in self.root.parts[1:]:
                child = _nt_open_relative(
                    handle, part, _GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                    directory=True, create=False,
                )
                info = _win_info(child)
                if not info[3] or info[2] & _FILE_ATTRIBUTE_REPARSE_POINT:
                    _win_close(child)
                    raise ValueError("task_apply_primary_unsafe")
                _win_close(handle)
                handle = child
        except BaseException:
            _win_close(handle)
            raise
        info = _win_info(handle)
        identity = PrimaryIdentity("windows", info[0], info[1])
        subject = _win_current_user_sid_string()
        name = "Global\\TorqPrimary-" + hashlib.sha256(
            f"{subject}:{identity.volume}:{identity.file_id}".encode("ascii")
        ).hexdigest()
        mutex = _win_mutex(name)
        self._root_handle = handle
        self._mutex = mutex
        self._identity = identity

    def _posix_parent(self, relative: str) -> tuple[int, str]:
        assert self._root_handle is not None
        path = PurePosixPath(validate_relative_path(relative))
        descriptor = os.dup(self._root_handle)
        flags = os.O_RDONLY | _POSIX_DIRECTORY | _POSIX_NOFOLLOW
        try:
            for part in path.parts[:-1]:
                child = os.open(part, flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(child)
                    raise ValueError("task_apply_parent_unsafe")
                os.close(descriptor)
                descriptor = child
            return descriptor, path.name
        except FileNotFoundError as exc:
            os.close(descriptor)
            raise ValueError("task_apply_parent_missing") from exc
        except BaseException:
            os.close(descriptor)
            raise

    def _windows_parent(self, relative: str) -> tuple[list[int], str]:
        assert self._root_handle is not None
        path = PurePosixPath(validate_relative_path(relative))
        handles = [self._root_handle]
        try:
            for part in path.parts[:-1]:
                child = _nt_open_relative(
                    handles[-1],
                    part,
                    _GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                    directory=True,
                    create=False,
                )
                info = _win_info(child)
                if not info[3] or info[2] & _FILE_ATTRIBUTE_REPARSE_POINT:
                    _win_close(child)
                    raise ValueError("task_apply_parent_unsafe")
                handles.append(child)
            return handles, path.name
        except FileNotFoundError as exc:
            for handle in reversed(handles[1:]):
                _win_close(handle)
            raise ValueError("task_apply_parent_missing") from exc
        except BaseException:
            for handle in reversed(handles[1:]):
                _win_close(handle)
            raise

    def inspect(self, relative: str) -> AnchoredFile:
        if sys.platform == "win32":
            return self._inspect_windows(relative)
        return self._inspect_posix(relative)

    def _inspect_posix(self, relative: str) -> AnchoredFile:
        parent, leaf = self._posix_parent(relative)
        try:
            return self._inspect_posix_at(parent, leaf, relative)
        finally:
            os.close(parent)

    def _inspect_posix_at(self, parent: int, leaf: str, relative: str) -> AnchoredFile:
        parent_metadata = os.fstat(parent)
        flags = os.O_RDONLY | _POSIX_NOFOLLOW
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent)
        except FileNotFoundError:
            return AnchoredFile(relative, None, None, None, _posix_parent_identity(parent_metadata), None)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("task_apply_target_unsafe")
            if before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise ValueError("task_apply_metadata_unsupported")
            if _posix_xattrs(descriptor):
                raise ValueError("task_apply_metadata_unsupported")
            content = _bounded_read_fd(descriptor)
            after = os.fstat(descriptor)
            if _posix_identity(before) != _posix_identity(after) or len(content) != before.st_size:
                raise ValueError("task_apply_source_changed")
            policy = {
                "platform": "posix", "mode": stat.S_IMODE(before.st_mode),
                "uid": int(before.st_uid), "gid": int(before.st_gid),
            }
            return AnchoredFile(
                relative, content, digest_bytes(content), policy,
                _posix_parent_identity(parent_metadata), _posix_identity(before),
            )
        finally:
            os.close(descriptor)

    def _inspect_windows(self, relative: str) -> AnchoredFile:
        handles, leaf = self._windows_parent(relative)
        try:
            return self._inspect_windows_at(handles[-1], leaf, relative)
        finally:
            for handle in reversed(handles[1:]):
                _win_close(handle)

    def _inspect_windows_at(self, parent: int, leaf: str, relative: str) -> AnchoredFile:
        parent_info = _win_info(parent)
        try:
            handle = _nt_open_relative(
                parent, leaf, _GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                directory=False, create=False,
            )
        except FileNotFoundError:
            return AnchoredFile(relative, None, None, None, _win_parent_identity(parent_info), None)
        try:
            before = _win_info(handle)
            allowed = _FILE_ATTRIBUTE_ARCHIVE | _FILE_ATTRIBUTE_NORMAL
            if before[3] or before[2] & ~allowed or before[4] != 1:
                raise ValueError("task_apply_metadata_unsupported")
            _win_require_single_stream(handle)
            content = _win_read(handle)
            after = _win_info(handle)
            if _win_identity(before) != _win_identity(after) or len(content) != before[5]:
                raise ValueError("task_apply_source_changed")
            policy = {
                "platform": "windows", "attributes": before[2] & allowed,
                "security": _win_security(handle).hex(),
            }
            return AnchoredFile(
                relative, content, digest_bytes(content), policy,
                _win_parent_identity(parent_info), _win_identity(before),
            )
        finally:
            _win_close(handle)

    def create_permission_policy(self) -> dict[str, object]:
        if sys.platform == "win32":
            return {"platform": "windows", "attributes": _FILE_ATTRIBUTE_NORMAL, "security": _win_owner_security().hex()}
        return {"platform": "posix", "mode": 0o600, "uid": os.getuid(), "gid": os.getgid()}

    def replace(
        self,
        relative: str,
        *,
        expected_before_hash: str | None,
        expected_parent_identity: dict[str, int],
        expected_target_identity: dict[str, int] | None,
        content: bytes,
        permission_policy: dict[str, object],
        application_id: str,
        operation_index: int,
        staged_callback: Callable[[dict[str, int]], None] | None = None,
        rollback: bool = False,
    ) -> AnchoredFile:
        if len(content) > MAX_TASK_FILE_BYTES:
            raise ValueError("task_apply_file_too_large")
        if sys.platform == "win32":
            return self._replace_windows(
                relative, expected_before_hash, expected_parent_identity, expected_target_identity,
                content, permission_policy, application_id, operation_index,
                staged_callback,
                rollback,
            )
        return self._replace_posix(
            relative, expected_before_hash, expected_parent_identity, expected_target_identity,
            content, permission_policy, application_id, operation_index,
            staged_callback,
            rollback,
        )

    def _replace_posix(
        self,
        relative: str,
        expected: str | None,
        expected_parent: dict[str, int],
        expected_target: dict[str, int] | None,
        content: bytes,
        policy: dict[str, object],
        application_id: str,
        index: int,
        staged_callback: Callable[[dict[str, int]], None] | None,
        rollback: bool,
    ) -> AnchoredFile:
        parent, leaf = self._posix_parent(relative)
        temporary = (
            f".torq-rb-{application_id}-{index}.tmp"
            if rollback else f".torq-{application_id}-{index}.tmp"
        )
        descriptor: int | None = None
        renamed = False
        try:
            before = self._inspect_posix_at(parent, leaf, relative)
            if (
                before.content_hash != expected
                or before.parent_identity != expected_parent
                or before.target_identity != expected_target
            ):
                raise ValueError("task_apply_source_changed")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _POSIX_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            _write_all_fd(descriptor, content)
            if policy.get("platform") != "posix":
                raise ValueError("task_apply_permission_invalid")
            _posix_apply_policy(descriptor, policy)
            os.fsync(descriptor)
            staged_identity = _posix_stable_identity(os.fstat(descriptor))
            os.close(descriptor)
            descriptor = None
            staged = self._inspect_posix_at(parent, temporary, relative)
            if (
                staged.content_hash != digest_bytes(content)
                or staged.target_identity is None
                or {key: staged.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
            ):
                raise ValueError("task_apply_staged_mismatch")
            if staged_callback is not None:
                staged_callback(staged_identity)
            staged_recheck = self._inspect_posix_at(parent, temporary, relative)
            if (
                staged_recheck.content_hash != digest_bytes(content)
                or staged_recheck.target_identity is None
                or {key: staged_recheck.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
                or _posix_parent_identity(os.fstat(parent)) != expected_parent
            ):
                raise ValueError("task_apply_staged_mismatch")
            current = self._inspect_posix_at(parent, leaf, relative)
            if current.content_hash != expected or current.target_identity != expected_target:
                raise ValueError("task_apply_source_changed")
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            renamed = True
            os.fsync(parent)
            result = self._inspect_posix_at(parent, leaf, relative)
            if (
                result.content_hash != digest_bytes(content)
                or result.target_identity is None
                or {key: result.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
            ):
                raise ValueError("task_apply_result_mismatch")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not renamed:
                try:
                    os.unlink(temporary, dir_fd=parent)
                    os.fsync(parent)
                except FileNotFoundError:
                    pass
            os.close(parent)
        return result

    def _replace_windows(
        self,
        relative: str,
        expected: str | None,
        expected_parent: dict[str, int],
        expected_target: dict[str, int] | None,
        content: bytes,
        policy: dict[str, object],
        application_id: str,
        index: int,
        staged_callback: Callable[[dict[str, int]], None] | None,
        rollback: bool,
    ) -> AnchoredFile:
        handles, leaf = self._windows_parent(relative)
        temporary = (
            f".torq-rb-{application_id}-{index}.tmp"
            if rollback else f".torq-{application_id}-{index}.tmp"
        )
        temp_handle: int | None = None
        renamed = False
        try:
            before = self._inspect_windows_at(handles[-1], leaf, relative)
            if (
                before.content_hash != expected
                or before.parent_identity != expected_parent
                or before.target_identity != expected_target
            ):
                raise ValueError("task_apply_source_changed")
            temp_handle = _nt_open_relative(
                handles[-1],
                temporary,
                _GENERIC_READ | _GENERIC_WRITE | _DELETE | _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _SYNCHRONIZE,
                directory=False,
                create=True,
            )
            _win_write(temp_handle, content)
            if policy.get("platform") != "windows" or not isinstance(policy.get("security"), str):
                raise ValueError("task_apply_permission_invalid")
            _win_set_security(temp_handle, bytes.fromhex(str(policy["security"])))
            _win_set_attributes(temp_handle, policy.get("attributes"))
            _win_flush(temp_handle)
            staged_identity = _win_stable_identity(_win_info(temp_handle))
            staged = self._inspect_windows_at(handles[-1], temporary, relative)
            if (
                staged.content_hash != digest_bytes(content)
                or staged.target_identity is None
                or {key: staged.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
            ):
                raise ValueError("task_apply_staged_mismatch")
            if staged_callback is not None:
                staged_callback(staged_identity)
            staged_recheck = self._inspect_windows_at(handles[-1], temporary, relative)
            if (
                staged_recheck.content_hash != digest_bytes(content)
                or staged_recheck.target_identity is None
                or {key: staged_recheck.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
                or _win_parent_identity(_win_info(handles[-1])) != expected_parent
            ):
                raise ValueError("task_apply_staged_mismatch")
            current = self._inspect_windows_at(handles[-1], leaf, relative)
            if current.content_hash != expected or current.target_identity != expected_target:
                raise ValueError("task_apply_source_changed")
            _win_rename_relative(temp_handle, handles[-1], leaf, replace=expected is not None)
            renamed = True
            _win_flush(handles[-1])
            result = self._inspect_windows_at(handles[-1], leaf, relative)
            if (
                result.content_hash != digest_bytes(content)
                or result.target_identity is None
                or {key: result.target_identity[key] for key in ("volume", "file_id")}
                != staged_identity
            ):
                raise ValueError("task_apply_result_mismatch")
        finally:
            if temp_handle is not None:
                if not renamed:
                    try:
                        _win_delete(temp_handle)
                    except OSError:
                        pass
                _win_close(temp_handle)
            for handle in reversed(handles[1:]):
                _win_close(handle)
        return result

    def remove(
        self,
        relative: str,
        *,
        expected_hash: str,
        expected_parent_identity: dict[str, int],
        expected_target_identity: dict[str, int],
    ) -> None:
        if sys.platform == "win32":
            handles, leaf = self._windows_parent(relative)
            try:
                parent_info = _win_info(handles[-1])
                if _win_parent_identity(parent_info) != expected_parent_identity:
                    raise ValueError("task_apply_recovery_conflict")
                handle = _nt_open_relative(
                    handles[-1], leaf, _GENERIC_READ | _DELETE | _READ_CONTROL | _SYNCHRONIZE,
                    directory=False, create=False,
                )
                try:
                    before = _win_info(handle)
                    content = _win_read(handle)
                    after = _win_info(handle)
                    if (
                        _win_identity(before) != expected_target_identity
                        or _win_identity(before) != _win_identity(after)
                        or digest_bytes(content) != expected_hash
                    ):
                        raise ValueError("task_apply_recovery_conflict")
                    _win_delete(handle)
                finally:
                    _win_close(handle)
                _win_flush(handles[-1])
            finally:
                for handle in reversed(handles[1:]):
                    _win_close(handle)
        else:
            parent, leaf = self._posix_parent(relative)
            try:
                current = self._inspect_posix_at(parent, leaf, relative)
                if (
                    current.content_hash != expected_hash
                    or current.parent_identity != expected_parent_identity
                    or current.target_identity != expected_target_identity
                ):
                    raise ValueError("task_apply_recovery_conflict")
                os.unlink(leaf, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)

    @staticmethod
    def _staged_leaf(application_id: str, operation_index: int) -> str:
        if (
            re.fullmatch(r"application-[a-f0-9]{24}", application_id) is None
            or not isinstance(operation_index, int)
            or isinstance(operation_index, bool)
            or operation_index < 0
            or operation_index >= MAX_TASK_FILES
        ):
            raise ValueError("task_apply_staged_name_invalid")
        return f".torq-{application_id}-{operation_index}.tmp"

    def inspect_staged(
        self, relative: str, *, application_id: str, operation_index: int
    ) -> AnchoredFile:
        temporary = self._staged_leaf(application_id, operation_index)
        if sys.platform == "win32":
            handles, _leaf = self._windows_parent(relative)
            try:
                return self._inspect_windows_at(handles[-1], temporary, relative)
            finally:
                for handle in reversed(handles[1:]):
                    _win_close(handle)
        parent, _leaf = self._posix_parent(relative)
        try:
            return self._inspect_posix_at(parent, temporary, relative)
        finally:
            os.close(parent)

    def inspect_rollback_staged(
        self, relative: str, *, application_id: str, operation_index: int
    ) -> AnchoredFile:
        self._staged_leaf(application_id, operation_index)
        temporary = f".torq-rb-{application_id}-{operation_index}.tmp"
        if sys.platform == "win32":
            handles, _leaf = self._windows_parent(relative)
            try:
                return self._inspect_windows_at(handles[-1], temporary, relative)
            finally:
                for handle in reversed(handles[1:]):
                    _win_close(handle)
        parent, _leaf = self._posix_parent(relative)
        try:
            return self._inspect_posix_at(parent, temporary, relative)
        finally:
            os.close(parent)

    def commit_rollback_staged(
        self,
        relative: str,
        *,
        application_id: str,
        operation_index: int,
        expected_before_hash: str,
        expected_before_identity: dict[str, int],
        expected_parent_identity: dict[str, int],
        staged_hash: str,
        staged_identity: dict[str, int],
        permission_policy: dict[str, object],
    ) -> AnchoredFile:
        self._staged_leaf(application_id, operation_index)
        temporary = f".torq-rb-{application_id}-{operation_index}.tmp"
        if sys.platform == "win32":
            handles, leaf = self._windows_parent(relative)
            temp_handle: int | None = None
            try:
                current = self._inspect_windows_at(handles[-1], leaf, relative)
                staged = self._inspect_windows_at(handles[-1], temporary, relative)
                if (
                    current.content_hash != expected_before_hash
                    or current.target_identity != expected_before_identity
                    or current.parent_identity != expected_parent_identity
                    or staged.content_hash != staged_hash
                    or staged.target_identity is None
                    or {key: staged.target_identity[key] for key in ("volume", "file_id")}
                    != staged_identity
                    or staged.parent_identity != expected_parent_identity
                    or staged.permission_policy != permission_policy
                ):
                    raise ValueError("task_apply_recovery_conflict")
                temp_handle = _nt_open_relative(
                    handles[-1], temporary,
                    _GENERIC_READ | _DELETE | _READ_CONTROL | _SYNCHRONIZE,
                    directory=False, create=False,
                )
                _win_rename_relative(temp_handle, handles[-1], leaf, replace=True)
                _win_flush(handles[-1])
                result = self._inspect_windows_at(handles[-1], leaf, relative)
            finally:
                if temp_handle is not None:
                    _win_close(temp_handle)
                for handle in reversed(handles[1:]):
                    _win_close(handle)
        else:
            parent, leaf = self._posix_parent(relative)
            try:
                current = self._inspect_posix_at(parent, leaf, relative)
                staged = self._inspect_posix_at(parent, temporary, relative)
                if (
                    current.content_hash != expected_before_hash
                    or current.target_identity != expected_before_identity
                    or current.parent_identity != expected_parent_identity
                    or staged.content_hash != staged_hash
                    or staged.target_identity is None
                    or {key: staged.target_identity[key] for key in ("volume", "file_id")}
                    != staged_identity
                    or staged.parent_identity != expected_parent_identity
                    or staged.permission_policy != permission_policy
                ):
                    raise ValueError("task_apply_recovery_conflict")
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                result = self._inspect_posix_at(parent, leaf, relative)
            finally:
                os.close(parent)
        if (
            result.content_hash != staged_hash
            or result.target_identity is None
            or {key: result.target_identity[key] for key in ("volume", "file_id")}
            != staged_identity
        ):
            raise ValueError("task_apply_recovery_conflict")
        return result

    def remove_staged(
        self,
        relative: str,
        *,
        application_id: str,
        operation_index: int,
        expected_hash: str,
        expected_parent_identity: dict[str, int],
        expected_target_identity: dict[str, int],
    ) -> None:
        temporary = self._staged_leaf(application_id, operation_index)
        if sys.platform == "win32":
            handles, _leaf = self._windows_parent(relative)
            try:
                if _win_parent_identity(_win_info(handles[-1])) != expected_parent_identity:
                    raise ValueError("task_apply_recovery_conflict")
                handle = _nt_open_relative(
                    handles[-1], temporary,
                    _GENERIC_READ | _DELETE | _READ_CONTROL | _SYNCHRONIZE,
                    directory=False, create=False,
                )
                try:
                    before = _win_info(handle)
                    content = _win_read(handle)
                    after = _win_info(handle)
                    if (
                        _win_identity(before) != expected_target_identity
                        or _win_identity(before) != _win_identity(after)
                        or digest_bytes(content) != expected_hash
                    ):
                        raise ValueError("task_apply_recovery_conflict")
                    _win_delete(handle)
                finally:
                    _win_close(handle)
                _win_flush(handles[-1])
            finally:
                for handle in reversed(handles[1:]):
                    _win_close(handle)
            return
        parent, _leaf = self._posix_parent(relative)
        try:
            current = self._inspect_posix_at(parent, temporary, relative)
            if (
                current.content_hash != expected_hash
                or current.parent_identity != expected_parent_identity
                or current.target_identity != expected_target_identity
            ):
                raise ValueError("task_apply_recovery_conflict")
            os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if sys.platform == "win32":
            if self._mutex is not None:
                _win_release_mutex(self._mutex)
                _win_close(self._mutex)
            if self._root_handle is not None:
                _win_close(self._root_handle)
        elif self._root_handle is not None:
            _posix_lock(self._root_handle, release=True)
            os.close(self._root_handle)

    def __enter__(self) -> AnchoredPrimary:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


# Windows native declarations are kept in this module so the public boundary
# has no pathname fallback once the primary root is held.
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_CREATE = 2
_FILE_OPEN = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_READONLY = 0x00000001
_FILE_ATTRIBUTE_ARCHIVE = 0x00000020
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_ENCRYPTED = 0x00004000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_WAIT_OBJECT_0 = 0


class _WinFileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _WinFileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32), ("created", _WinFileTime), ("accessed", _WinFileTime),
        ("written", _WinFileTime), ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong), ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileRenameInfo(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_int), ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32), ("FileName", ctypes.c_wchar * 1),
    ]


def _win_dll(name: str) -> Any:
    factory = getattr(ctypes, "WinDLL", None)
    if factory is None:
        raise RuntimeError("task_apply_platform_unsupported")
    return factory(name, use_last_error=True)


def _win_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _kernel32() -> Any:
    return _win_dll("kernel32")


def _advapi32() -> Any:
    return _win_dll("advapi32")


def _win_open_path(path: str, access: int, disposition: int, flags: int) -> int:
    create = _kernel32().CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    handle = create(path, access, _FILE_SHARE_ALL, None, disposition, flags, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        error = _win_last_error()
        raise OSError(error, "CreateFileW failed")
    return int(handle)


def _nt_open_relative(parent: int, name: str, access: int, *, directory: bool, create: bool) -> int:
    buffer = ctypes.create_unicode_buffer(name)
    encoded_bytes = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(encoded_bytes, encoded_bytes + 2, ctypes.cast(buffer, ctypes.c_void_p))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), ctypes.c_void_p(parent), ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE, None, None,
    )
    io = _IoStatusBlock()
    result = ctypes.c_void_p()
    ntdll = _win_dll("ntdll")
    call = ntdll.NtCreateFile
    call.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    call.restype = ctypes.c_long
    options = (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE) | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    status = call(
        ctypes.byref(result), access, ctypes.byref(attributes), ctypes.byref(io), None,
        _FILE_ATTRIBUTE_NORMAL, _FILE_SHARE_ALL, _FILE_CREATE if create else _FILE_OPEN,
        options, None, 0,
    )
    if status < 0 or result.value is None:
        converter = ntdll.RtlNtStatusToDosError
        converter.argtypes = [ctypes.c_long]
        converter.restype = ctypes.c_ulong
        error = int(converter(status))
        if error in {2, 3}:
            raise FileNotFoundError(error, name)
        if error in {80, 183}:
            raise FileExistsError(error, name)
        raise OSError(error, f"NtCreateFile failed: {name}")
    return int(result.value)


def _win_info(handle: int) -> tuple[int, int, int, bool, int, int, int]:
    info = _WinFileInfo()
    call = _kernel32().GetFileInformationByHandle
    if not call(ctypes.c_void_p(handle), ctypes.byref(info)):
        raise OSError(_win_last_error(), "GetFileInformationByHandle failed")
    file_id = (info.index_high << 32) | info.index_low
    is_directory = bool(info.attributes & 0x10)
    size = (info.size_high << 32) | info.size_low
    modified = (info.written.high << 32) | info.written.low
    return (
        int(info.volume), int(file_id), int(info.attributes), is_directory,
        int(info.links), int(size), int(modified),
    )


def _win_read(handle: int) -> bytes:
    call = _kernel32().ReadFile
    result = bytearray()
    while len(result) <= MAX_TASK_FILE_BYTES:
        buffer = ctypes.create_string_buffer(min(_READ_CHUNK, MAX_TASK_FILE_BYTES + 1 - len(result)))
        count = ctypes.c_uint32()
        if not call(ctypes.c_void_p(handle), buffer, len(buffer), ctypes.byref(count), None):
            raise OSError(_win_last_error(), "ReadFile failed")
        if count.value == 0:
            break
        result.extend(buffer.raw[: count.value])
    if len(result) > MAX_TASK_FILE_BYTES:
        raise ValueError("task_apply_file_too_large")
    return bytes(result)


def _win_write(handle: int, content: bytes) -> None:
    call = _kernel32().WriteFile
    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + _READ_CHUNK]
        buffer = ctypes.create_string_buffer(chunk)
        count = ctypes.c_uint32()
        if not call(ctypes.c_void_p(handle), buffer, len(chunk), ctypes.byref(count), None):
            raise OSError(_win_last_error(), "WriteFile failed")
        if count.value == 0:
            raise OSError("WriteFile made no progress")
        offset += count.value


def _win_flush(handle: int) -> None:
    if not _kernel32().FlushFileBuffers(ctypes.c_void_p(handle)):
        error = _win_last_error()
        # Directory handles commonly reject FlushFileBuffers; no weaker path
        # rename is introduced, and file data is already flushed.
        if error not in {1, 5, 87}:
            raise OSError(error, "FlushFileBuffers failed")


def _win_security(handle: int) -> bytes:
    requested = 0x00000001 | 0x00000002 | 0x00000004
    needed = ctypes.c_uint32()
    call = _advapi32().GetKernelObjectSecurity
    call(ctypes.c_void_p(handle), requested, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise OSError(_win_last_error(), "GetKernelObjectSecurity size failed")
    buffer = ctypes.create_string_buffer(needed.value)
    if not call(ctypes.c_void_p(handle), requested, buffer, len(buffer), ctypes.byref(needed)):
        raise OSError(_win_last_error(), "GetKernelObjectSecurity failed")
    # Auto-inheritance control flags describe how Windows obtained the DACL,
    # not the owner/group/DACL authority that this policy preserves. Setting a
    # copied descriptor can legitimately clear these flags, so normalize them
    # before binding or comparing the policy.
    normalize = _advapi32().SetSecurityDescriptorControl
    normalize.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_ushort]
    normalize.restype = ctypes.c_int
    if not normalize(buffer, 0x0C00, 0):
        raise OSError(_win_last_error(), "SetSecurityDescriptorControl failed")
    return buffer.raw[: needed.value]


def _win_set_security(handle: int, descriptor: bytes) -> None:
    requested = 0x00000001 | 0x00000002 | 0x00000004
    buffer = ctypes.create_string_buffer(descriptor)
    if not _advapi32().SetKernelObjectSecurity(ctypes.c_void_p(handle), requested, buffer):
        raise OSError(_win_last_error(), "SetKernelObjectSecurity failed")


def _win_current_user_sid_string() -> str:
    token_query = 0x0008
    token_user = 1
    token = ctypes.c_void_p()
    current = _kernel32().GetCurrentProcess
    current.argtypes = []
    current.restype = ctypes.c_void_p
    open_token = _advapi32().OpenProcessToken
    open_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    open_token.restype = ctypes.c_int
    if not open_token(current(), token_query, ctypes.byref(token)):
        raise OSError(_win_last_error(), "OpenProcessToken failed")
    try:
        needed = ctypes.c_uint32()
        get_information = _advapi32().GetTokenInformation
        get_information.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_information.restype = ctypes.c_int
        get_information(token, token_user, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise OSError(_win_last_error(), "GetTokenInformation size failed")
        buffer = ctypes.create_string_buffer(needed.value)
        if not get_information(
            token, token_user, buffer, len(buffer), ctypes.byref(needed)
        ):
            raise OSError(_win_last_error(), "GetTokenInformation failed")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        rendered = ctypes.c_wchar_p()
        convert_sid = _advapi32().ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert_sid.restype = ctypes.c_int
        if not convert_sid(sid, ctypes.byref(rendered)):
            raise OSError(_win_last_error(), "ConvertSidToStringSidW failed")
        try:
            if rendered.value is None:
                raise OSError("ConvertSidToStringSidW returned no value")
            return rendered.value
        finally:
            _kernel32().LocalFree(rendered)
    finally:
        if token.value is not None:
            _win_close(int(token.value))


def _win_owner_security() -> bytes:
    sid = _win_current_user_sid_string()
    sddl = f"O:{sid}G:{sid}D:P(A;;FA;;;{sid})"
    descriptor = ctypes.c_void_p()
    convert = _advapi32().ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    convert.restype = ctypes.c_int
    if not convert(
        ctypes.c_wchar_p(sddl), 1, ctypes.byref(descriptor), None
    ):
        raise OSError(
            _win_last_error(),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
        )
    try:
        get_length = _advapi32().GetSecurityDescriptorLength
        get_length.argtypes = [ctypes.c_void_p]
        get_length.restype = ctypes.c_uint32
        length = get_length(descriptor)
        if not isinstance(length, int) or length <= 0:
            raise OSError(_win_last_error(), "GetSecurityDescriptorLength failed")
        return ctypes.string_at(descriptor, length)
    finally:
        _kernel32().LocalFree(descriptor)


class _WinBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


def _win_set_attributes(handle: int, value: object) -> None:
    allowed = _FILE_ATTRIBUTE_ARCHIVE | _FILE_ATTRIBUTE_NORMAL
    if not isinstance(value, int) or isinstance(value, bool) or value & ~allowed:
        raise ValueError("task_apply_permission_invalid")
    info = _WinBasicInfo(0, 0, 0, 0, value)
    if not _kernel32().SetFileInformationByHandle(
        ctypes.c_void_p(handle), 0, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(_win_last_error(), "FileBasicInfo failed")


def _win_require_single_stream(handle: int) -> None:
    # FILE_STREAM_INFO names include the unnamed stream as ::$DATA.
    buffer = ctypes.create_string_buffer(4096)
    call = _kernel32().GetFileInformationByHandleEx
    if not call(ctypes.c_void_p(handle), 7, buffer, len(buffer)):
        raise OSError(_win_last_error(), "FileStreamInfo failed")
    offset = 0
    names: list[str] = []
    while True:
        next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
        name_length = int.from_bytes(buffer.raw[offset + 4 : offset + 8], "little")
        name = buffer.raw[offset + 24 : offset + 24 + name_length].decode("utf-16-le")
        names.append(name)
        if next_offset == 0:
            break
        offset += next_offset
        if offset >= len(buffer):
            raise ValueError("task_apply_metadata_unsupported")
    if names != ["::$DATA"]:
        raise ValueError("task_apply_metadata_unsupported")


def _win_rename_relative(handle: int, parent: int, name: str, *, replace: bool) -> None:
    encoded = name.encode("utf-16-le")
    offset = _FileRenameInfo.FileName.offset
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_FileRenameInfo) + len(encoded))
    header = _FileRenameInfo.from_buffer(buffer)
    header.ReplaceIfExists = int(replace)
    header.RootDirectory = ctypes.c_void_p(parent)
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    io = _IoStatusBlock()
    ntdll = _win_dll("ntdll")
    call = ntdll.NtSetInformationFile
    call.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
    call.restype = ctypes.c_long
    status = call(ctypes.c_void_p(handle), ctypes.byref(io), buffer, len(buffer), 10)
    if status < 0:
        converter = ntdll.RtlNtStatusToDosError
        converter.argtypes = [ctypes.c_long]
        converter.restype = ctypes.c_ulong
        raise OSError(int(converter(status)), "FileRenameInformation failed")


def _win_delete(handle: int) -> None:
    value = ctypes.c_ubyte(1)
    if not _kernel32().SetFileInformationByHandle(ctypes.c_void_p(handle), 4, ctypes.byref(value), 1):
        raise OSError(_win_last_error(), "FileDispositionInfo failed")


def _win_mutex(name: str) -> int:
    kernel = _kernel32()
    call = kernel.CreateMutexW
    call.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    call.restype = ctypes.c_void_p
    handle = call(None, 0, name)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(_win_last_error(), "CreateMutexW failed")
    already_exists = _win_last_error() == 183
    if already_exists:
        _win_close(int(handle))
        raise RuntimeError("task_apply_primary_busy")
    wait = kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0)
    if wait != _WAIT_OBJECT_0:
        _win_close(int(handle))
        raise RuntimeError("task_apply_primary_busy")
    return int(handle)


def _win_release_mutex(handle: int) -> None:
    if not _kernel32().ReleaseMutex(ctypes.c_void_p(handle)):
        raise OSError(_win_last_error(), "ReleaseMutex failed")


def _win_close(handle: int) -> None:
    if not _kernel32().CloseHandle(ctypes.c_void_p(handle)):
        raise OSError(_win_last_error(), "CloseHandle failed")


__all__ = ["AnchoredFile", "AnchoredPrimary", "PrimaryIdentity"]
