"""Minimal production guard for the Foundation's explicit-read boundary."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from dataclasses import dataclass
from typing import Callable, NoReturn, cast
import unicodedata

PROHIBITED_IMPORTS = frozenset({
    "os", "subprocess", "socket", "urllib", "http", "requests", "httpx", "asyncio",
    "sqlite3",
})
_PROTECTED_SEGMENTS = frozenset({".env", ".git", "id_rsa", "credentials", "secrets", "keychain", "torq-console"})
_PROTECTED_ROOTS = ("e:/torq-console",)
_REPARSE_STATES = frozenset({"symlink", "junction", "reparse", "uncertain"})


class ProtectedPathError(PermissionError):
    finding_id = "protected_path_denied"


class LegacyConfigUnreadable(OSError):
    finding_id = "legacy_config_unreadable"


class LegacyConfigTooLarge(ValueError):
    finding_id = "legacy_config_schema_invalid"


@dataclass(frozen=True)
class PosixIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    regular: bool


MAX_LEGACY_CONFIG_BYTES = 65_536
POSIX_FINAL_FLAGS = (
    getattr(os, "O_RDONLY", 0)
    | getattr(os, "O_NONBLOCK", 0x800)
    | getattr(os, "O_NOFOLLOW", 0x20000)
    | getattr(os, "O_CLOEXEC", 0x80000)
)
POSIX_ANCESTOR_FLAGS = (
    getattr(os, "O_RDONLY", 0)
    | getattr(os, "O_DIRECTORY", 0x10000)
    | getattr(os, "O_NOFOLLOW", 0x20000)
    | getattr(os, "O_CLOEXEC", 0x80000)
)

FILE_READ_DATA = 0x00000001
FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
WINDOWS_FINAL_ACCESS = FILE_READ_DATA | FILE_READ_ATTRIBUTES
WINDOWS_ANCESTOR_FLAGS = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
FILE_NAME_NORMALIZED = 0x00000000
VOLUME_NAME_GUID = 0x00000001
_POSIX_SAFETY_ERRNOS = frozenset({
    getattr(errno, "ENOSYS", 38),
    getattr(errno, "ENOTSUP", 95),
    getattr(errno, "EOPNOTSUPP", 95),
    getattr(errno, "EINVAL", 22),
})
_WINDOWS_SAFETY_ERRORS = frozenset({1, 50, 120})


def _windows_safety_error(exc: OSError) -> bool:
    code = getattr(exc, "winerror", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    return code in _WINDOWS_SAFETY_ERRORS


def _posix_open_for_read(open_call: Callable[[], int]) -> int:
    try:
        return open_call()
    except ProtectedPathError:
        raise
    except (AttributeError, NotImplementedError, TypeError):
        _deny()
    except OSError as exc:
        if exc.errno == errno.ELOOP or exc.errno in _POSIX_SAFETY_ERRNOS:
            _deny()
        raise LegacyConfigUnreadable("legacy config cannot be opened") from exc


def _posix_directory_for_read(fd: int) -> bool:
    try:
        return _posix_is_directory(fd)
    except ProtectedPathError:
        raise
    except (AttributeError, NotImplementedError, TypeError, OSError):
        _deny()


def _posix_identity_for_read(fd: int) -> PosixIdentity:
    try:
        return _posix_fstat(fd)
    except ProtectedPathError:
        raise
    except (AttributeError, NotImplementedError, TypeError, OSError):
        _deny()


def _windows_open_for_read(open_call: Callable[[], int]) -> int:
    try:
        return open_call()
    except ProtectedPathError:
        raise
    except (AttributeError, NotImplementedError, TypeError):
        _deny()
    except OSError as exc:
        if _windows_safety_error(exc):
            _deny()
        raise LegacyConfigUnreadable("legacy config cannot be opened") from exc


def _windows_identity_for_read(handle: int) -> tuple[int, int, int, int, bool]:
    try:
        return _windows_identity(handle)
    except ProtectedPathError:
        raise
    except (AttributeError, NotImplementedError, TypeError, OSError):
        _deny()


def _close_posix_descriptors(descriptors: list[int]) -> None:
    failure = False
    for descriptor in reversed(descriptors):
        try:
            _posix_close(descriptor)
        except ProtectedPathError:
            failure = True
        except (AttributeError, NotImplementedError, TypeError, OSError):
            failure = True
    if failure:
        _deny()


def _close_windows_handles(handles: list[int]) -> None:
    failure = False
    for handle in reversed(handles):
        try:
            _windows_close(handle)
        except ProtectedPathError:
            failure = True
        except (AttributeError, NotImplementedError, TypeError, OSError):
            failure = True
    if failure:
        _deny()


def _posix_open(path: str, flags: int) -> int:
    try:
        return os.open(path, flags)
    except (AttributeError, NotImplementedError, TypeError):
        _deny()
    except OSError as exc:
        if exc.errno in _POSIX_SAFETY_ERRNOS:
            _deny()
        raise


def _posix_openat(parent_fd: int, path: str, flags: int) -> int:
    try:
        return os.open(path, flags, dir_fd=parent_fd)
    except (AttributeError, NotImplementedError, TypeError):
        _deny()
    except OSError as exc:
        if exc.errno in _POSIX_SAFETY_ERRNOS:
            _deny()
        raise


def _posix_is_directory(fd: int) -> bool:
    try:
        return stat.S_ISDIR(os.fstat(fd).st_mode)
    except (AttributeError, NotImplementedError, OSError):
        _deny()


def _posix_read(fd: int, size: int) -> bytes:
    try:
        return os.read(fd, size)
    except (AttributeError, NotImplementedError, TypeError):
        _deny()
    except OSError as exc:
        if exc.errno in _POSIX_SAFETY_ERRNOS:
            _deny()
        if exc.errno == errno.EINTR:
            raise
        raise LegacyConfigUnreadable("legacy config read failed") from exc


def _posix_fstat(fd: int) -> PosixIdentity:
    try:
        metadata = os.fstat(fd)
        return PosixIdentity(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
        )
    except (AttributeError, NotImplementedError, OSError):
        _deny()


def _posix_close(fd: int) -> None:
    try:
        os.close(fd)
    except (AttributeError, NotImplementedError, OSError):
        _deny()


def _windows_open(path: str, access: int, share: int, disposition: int, flags: int) -> int:
    if not _is_windows_platform():
        _deny()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    try:
        create_file = kernel32.CreateFileW
        create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create_file.restype = ctypes.c_void_p
        handle = create_file(path, access, share, None, disposition, flags, None)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        if error in _WINDOWS_SAFETY_ERRORS:
            _deny()
        raise OSError(error, "CreateFileW failed")
    return int(handle)


def _windows_read(handle: int, size: int) -> bytes:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    buffer = ctypes.create_string_buffer(size)
    count = ctypes.c_uint32()
    try:
        success = kernel32.ReadFile(ctypes.c_void_p(handle), buffer, size, ctypes.byref(count), None)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    if not success:
        error = ctypes.get_last_error()
        if error in _WINDOWS_SAFETY_ERRORS:
            _deny()
        raise LegacyConfigUnreadable("legacy config read failed")
    return buffer.raw[: count.value]


def _windows_long_path(path: str) -> str:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    buffer = ctypes.create_unicode_buffer(32768)
    try:
        length = kernel32.GetLongPathNameW(path, buffer, len(buffer))
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    if length == 0 or length >= len(buffer):
        _deny()
    return buffer.value


def _windows_volume_guid(path: str) -> str:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    root = path[:3] if len(path) >= 2 and path[1] == ":" else path
    buffer = ctypes.create_unicode_buffer(32768)
    try:
        success = kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer))
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    if not success:
        _deny()
    return buffer.value


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _WindowsFileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32), ("created", _FileTime), ("accessed", _FileTime),
        ("written", _FileTime), ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32),
    ]


def _windows_identity(handle: int) -> tuple[int, int, int, int, bool]:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    info = _WindowsFileInfo()
    try:
        if not kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
            _deny()
        file_type = kernel32.GetFileType(ctypes.c_void_p(handle))
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    is_regular = file_type == 1 and not (info.attributes & 0x10) and not (info.attributes & 0x400)
    return (
        info.volume,
        (info.index_high << 32) | info.index_low,
        (info.size_high << 32) | info.size_low,
        (info.written.high << 32) | info.written.low,
        is_regular and info.links == 1,
    )


def _windows_canonical_path(path: str, handle: int) -> str:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    buffer = ctypes.create_unicode_buffer(32768)
    try:
        length = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(handle), buffer, len(buffer), FILE_NAME_NORMALIZED | VOLUME_NAME_GUID,
        )
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    if length == 0 or length >= len(buffer):
        _deny()
    return buffer.value


def _windows_is_directory(handle: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    info = _WindowsFileInfo()
    try:
        if not kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
            _deny()
    except (AttributeError, NotImplementedError, OSError):
        _deny()
    return bool(info.attributes & 0x10) and not bool(info.attributes & 0x400)


def _windows_close(handle: int) -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
            _deny()
    except (AttributeError, NotImplementedError, OSError):
        _deny()


def _windows_compare_path(requested: str, observed: str) -> bool:
    def normalize(value: str) -> str:
        text = value.replace("/", "\\").casefold()
        if text.startswith("\\\\?\\"):
            text = text[4:]
        return text.rstrip("\\")

    return normalize(requested) == normalize(observed)


def _windows_to_volume_guid(path: str) -> str:
    text = path.replace("/", "\\")
    if text.casefold().startswith("\\\\?\\volume{"):
        return text
    if text.casefold().startswith("\\\\?\\"):
        text = text[4:]
    if len(text) >= 2 and text[1] == ":":
        root = text[:3]
        volume = _windows_volume_guid(root).rstrip("\\")
        return volume + text[2:]
    return text


def _windows_walk_paths(path: str) -> tuple[tuple[str, ...], str]:
    text = path.replace("/", "\\")
    if len(text) < 3 or text[1] != ":" or text[2] != "\\":
        _deny()
    parts = [part for part in text[3:].split("\\") if part]
    if not parts:
        _deny()
    current = text[:3]
    ancestors = [current]
    for part in parts[:-1]:
        current = current.rstrip("\\") + "\\" + part
        ancestors.append(current)
    return tuple(ancestors), current.rstrip("\\") + "\\" + parts[-1]


def _windows_reserved_or_unsafe(path: str) -> bool:
    text = path.replace("/", "\\")
    if ":" in text[2:]:
        return True
    for segment in text.split("\\"):
        if not segment or segment.endswith((".", " ")):
            continue
        stem = segment.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or (stem[:3] in {"com", "lpt"} and stem[3:].isdigit() and 1 <= int(stem[3:]) <= 9):
            return True
    return any(segment.endswith((".", " ")) for segment in text.split("\\") if segment)


def _read_posix_bounded(candidate: Path) -> bytes:
    normalized = str(candidate)
    parts = [part for part in normalized.split("/") if part]
    if not normalized.startswith("/") or not parts:
        _deny()
    descriptors: list[int] = []
    try:
        parent = _posix_open_for_read(lambda: _posix_open("/", POSIX_ANCESTOR_FLAGS))
        descriptors.append(parent)
        for component in parts[:-1]:
            def open_ancestor(parent_fd: int = parent, name: str = component) -> int:
                return _posix_openat(parent_fd, name, POSIX_ANCESTOR_FLAGS)

            parent = _posix_open_for_read(open_ancestor)
            descriptors.append(parent)
            if not _posix_directory_for_read(parent):
                _deny()
        def open_final() -> int:
            return _posix_openat(parent, parts[-1], POSIX_FINAL_FLAGS)

        fd = _posix_open_for_read(open_final)
        descriptors.append(fd)
        before = _posix_identity_for_read(fd)
        if not before.regular:
            _deny()
        data = bytearray()
        while True:
            try:
                chunk = _posix_read(fd, MAX_LEGACY_CONFIG_BYTES + 1 - len(data))
            except InterruptedError:
                continue
            except AttributeError:
                _deny()
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno in _POSIX_SAFETY_ERRNOS:
                    _deny()
                raise LegacyConfigUnreadable("legacy config read failed") from exc
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_LEGACY_CONFIG_BYTES:
                raise LegacyConfigTooLarge()
        after = _posix_identity_for_read(fd)
        if before != after:
            _deny()
        return bytes(data)
    finally:
        _close_posix_descriptors(descriptors)


def _read_windows_bounded(candidate: Path) -> bytes:
    requested = str(candidate).replace("/", "\\")
    if _windows_reserved_or_unsafe(requested):
        _deny()
    ancestors, final_path = _windows_walk_paths(requested)
    handles: list[int] = []
    try:
        for ancestor in ancestors:
            def open_ancestor(name: str = ancestor) -> int:
                return _windows_open(name, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, OPEN_EXISTING, WINDOWS_ANCESTOR_FLAGS)

            handle = _windows_open_for_read(open_ancestor)
            handles.append(handle)
            try:
                is_directory = _windows_is_directory(handle)
            except ProtectedPathError:
                raise
            except (AttributeError, NotImplementedError, TypeError, OSError):
                _deny()
            if not is_directory:
                _deny()
        def open_final() -> int:
            return _windows_open(final_path, WINDOWS_FINAL_ACCESS, FILE_SHARE_READ, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT)

        handle = _windows_open_for_read(open_final)
        handles.append(handle)
        before = _windows_identity_for_read(handle)
        if not before[-1]:
            _deny()
        try:
            long_path = _windows_long_path(requested)
            if not _windows_compare_path(requested, long_path):
                _deny()
            candidate_canonical = _windows_to_volume_guid(long_path)
            observed_canonical = _windows_canonical_path(final_path, handle)
        except ProtectedPathError:
            raise
        except (AttributeError, NotImplementedError, TypeError, OSError):
            _deny()
        if not _windows_compare_path(candidate_canonical, observed_canonical):
            _deny()
        data = bytearray()
        while True:
            try:
                chunk = _windows_read(handle, MAX_LEGACY_CONFIG_BYTES + 1 - len(data))
            except ProtectedPathError:
                raise
            except (AttributeError, NotImplementedError, TypeError):
                _deny()
            except OSError as exc:
                if _windows_safety_error(exc):
                    _deny()
                raise LegacyConfigUnreadable("legacy config read failed") from exc
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_LEGACY_CONFIG_BYTES:
                raise LegacyConfigTooLarge()
        after = _windows_identity_for_read(handle)
        if before != after:
            _deny()
        return bytes(data)
    finally:
        _close_windows_handles(handles)


def read_bounded_legacy_config(path: str) -> bytes:
    """Read one explicit normalized-config snapshot through a bounded final handle."""
    path_kind = _path_kind(path)
    if path_kind not in {"windows", "posix"}:
        _deny()
    normalized = _lexical_normalize(path, path_kind)
    if _is_protected_lexically(normalized, path_kind):
        _deny()
    candidate = _path_object(normalized, path_kind)
    if _is_windows_platform():
        return _read_windows_bounded(candidate)
    return _read_posix_bounded(candidate)


def _deny() -> NoReturn:
    raise ProtectedPathError("protected path access denied")


def _is_windows_platform() -> bool:
    return sys.platform.startswith("win")


def _path_kind(value: str | Path) -> str:
    raw = unicodedata.normalize("NFC", str(value))
    text = raw.replace("\\", "/")
    if raw.startswith("\\") or text.startswith("//"):
        return "denied"
    if len(text) >= 2 and text[1] == ":":
        is_ascii_drive = text[0].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return "windows" if _is_windows_platform() and is_ascii_drive and len(text) >= 4 and text[2] == "/" else "denied"
    if text.startswith("/"):
        return "denied" if _is_windows_platform() else "posix"
    return "denied"


def _lexical_normalize(value: str | Path, kind: str | None = None) -> str:
    kind = kind or _path_kind(value)
    text = unicodedata.normalize("NFC", str(value))
    if kind == "windows":
        text = text.replace("\\", "/").casefold()
    elif kind == "posix":
        text = text
    else:
        return ""

    anchor = ""
    if len(text) >= 2 and text[1] == ":":
        anchor, text = text[:2], text[2:]
        text = text.lstrip("/")
    elif text.startswith("//"):
        anchor, text = "//", text[2:]
    elif text.startswith("/"):
        anchor, text = "/", text[1:]

    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not anchor:
                parts.append(part)
            continue
        parts.append(part)

    joined = "/".join(parts)
    if anchor == "//":
        return "//" + joined
    if anchor == "/":
        return "/" + joined
    if anchor:
        return anchor + ("/" + joined if joined else "")
    return joined


def _is_fully_qualified_local_absolute(value: str | Path) -> bool:
    return _path_kind(value) in {"windows", "posix"}


def _path_object(normalized: str, kind: str) -> Path:
    if kind == "posix":
        return cast(Path, PurePosixPath(normalized))
    return Path(normalized)


def _is_protected_lexically(normalized: str, kind: str | None = None) -> bool:
    kind = kind or _path_kind(normalized)
    segments = [segment for segment in normalized.split("/") if segment]
    if kind == "windows":
        segments = [segment.casefold() for segment in segments]
    if any(segment in _PROTECTED_SEGMENTS for segment in segments):
        return True
    roots = _PROTECTED_ROOTS if kind == "windows" else ()
    return any(normalized == root or normalized.startswith(root + "/") for root in roots)


def _candidate_components(normalized: str) -> tuple[str, ...]:
    parts = [part for part in normalized.split("/") if part]
    components: list[str] = []
    if normalized.startswith("//"):
        current = "//"
    elif len(normalized) >= 2 and normalized[1] == ":":
        current = normalized[:2] + "/"
        parts = parts[1:]
    elif normalized.startswith("/"):
        current = "/"
    else:
        current = ""
    for part in parts:
        current = current.rstrip("/") + "/" + part
        components.append(current.rstrip("/"))
    return tuple(components)


def _classify_component(component: str) -> str:
    candidate = Path(component)
    try:
        if candidate.is_symlink():
            return "symlink"
        is_junction = getattr(candidate, "is_junction", None)
        if callable(is_junction) and is_junction():
            return "junction"
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except TypeError:
            metadata = candidate.lstat()
        if getattr(metadata, "st_file_attributes", 0) & 0x400:
            return "reparse"
    except FileNotFoundError:
        return "ordinary"
    except (OSError, RuntimeError, ValueError):
        return "uncertain"
    return "ordinary"


def assert_read_allowed(
    path: str,
    resolver: Callable[[Path], Path] | None = None,
    component_classifier: Callable[[str], str] | None = None,
) -> Path:
    path_kind = _path_kind(path)
    if path_kind not in {"windows", "posix"}:
        _deny()
    lexical = _lexical_normalize(path, path_kind)
    if _is_protected_lexically(lexical, path_kind):
        _deny()

    candidate = resolver(_path_object(lexical, path_kind)) if resolver is not None else _path_object(lexical, path_kind)
    candidate_kind = _path_kind(candidate)
    if candidate_kind not in {"windows", "posix"} or candidate_kind != path_kind:
        _deny()
    candidate_lexical = _lexical_normalize(candidate, candidate_kind)
    if _is_protected_lexically(candidate_lexical, candidate_kind):
        _deny()

    classifier = component_classifier or _classify_component
    for component in _candidate_components(candidate_lexical):
        if classifier(component) in _REPARSE_STATES:
            _deny()
    return _path_object(candidate_lexical, candidate_kind)


class ReadOnlyConfigReader:
    """Reads only the explicit config path supplied by the operator."""

    def __init__(
        self,
        resolver: Callable[[Path], Path] | None = None,
        component_classifier: Callable[[str], str] | None = None,
    ) -> None:
        self._resolver = resolver
        self._component_classifier = component_classifier

    def read_bytes(self, path: str) -> bytes:
        candidate = assert_read_allowed(
            path,
            resolver=self._resolver,
            component_classifier=self._component_classifier,
        )
        return read_bounded_legacy_config(str(candidate))

    def read_utf8(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8", errors="strict")
