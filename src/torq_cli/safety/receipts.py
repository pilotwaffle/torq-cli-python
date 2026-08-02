"""Hash-chained, redacted receipts and signed terminal manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from collections.abc import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from torq_cli.core.canonical_json import canonical_json
from torq_cli.core.redaction import PatternRegistry
from torq_cli.domain.evidence_transitions import (
    transition_authority_finding,
    transition_rule,
)
from torq_cli.domain.run_evidence import (
    validate_receipt_payload,
    validate_v2_receipt_contract,
)

_PRIVATE_KEY_NAME = ".torq-receipt-signing-key"
_PUBLIC_KEY_NAME = ".torq-receipt-signing-key.pub"
_LEGACY_RECEIPT_SCHEMA_VERSION = "1.0.0"
_AUTHORITY_RECEIPT_SCHEMA_VERSION = "1.1.0"
_RECEIPT_SCHEMA_VERSION = "2.0.0"
_LEGACY_CERTIFICATE_SCHEMA_VERSION = "1.0.0"
_ANCHORLESS_CERTIFICATE_SCHEMA_VERSION = "2.0.0"
_ROLLBACK_CERTIFICATE_SCHEMA_VERSION = "3.0.0"
_CERTIFICATE_SCHEMA_VERSION = "4.0.0"
_ROLLBACK_CERTIFICATE_SCHEMA_VERSIONS = frozenset(
    {_ROLLBACK_CERTIFICATE_SCHEMA_VERSION, _CERTIFICATE_SCHEMA_VERSION}
)
_MANIFEST_ANCHOR_SCHEMA_VERSION = "1.0.0"
_MANIFEST_ROLLBACK_POLICY = "external-signed-head-v1"
_CANONICAL_ENCODING_MARKER = "UTF-8 \u2713"
_RECEIPT_AUTHORITIES = frozenset({"worker", "supervisor_derived"})
_SUPERVISOR_TRANSITIONS = frozenset({"stage_interrupted", "run_decision"})
_WRITER_ROLES = frozenset(
    {"orchestrator", "supervisor", "operator_gateway", "recovery"}
)
_EVIDENCE_BASES = frozenset({"observed", "derived", "submitted"})
_RUN_CERTIFICATE_NAME = "run-certificate.json"
_RUN_IDENTITIES_DIR = ".torq-run-identities"
_ARTIFACT_FORMAT = b"TORQAEAD1"
_BINARY = getattr(os, "O_BINARY", 0)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def _valid_run_id(run_id: str) -> bool:
    stem = run_id.split(".", 1)[0].casefold()
    return (
        _RUN_ID.fullmatch(run_id) is not None
        and run_id not in {".", ".."}
        and not run_id.endswith(".")
        and stem not in _WINDOWS_DEVICE_NAMES
    )


def _receipt_authority_finding(
    schema_version: object,
    transition: object,
    authority: object,
) -> str | None:
    if schema_version == _LEGACY_RECEIPT_SCHEMA_VERSION:
        return "receipt_authority_version_invalid" if authority is not None else None
    if schema_version != _AUTHORITY_RECEIPT_SCHEMA_VERSION:
        return "receipt_schema_unsupported"
    if not isinstance(authority, str) or authority not in _RECEIPT_AUTHORITIES:
        return "receipt_authority_invalid"
    if authority == "supervisor_derived" and transition not in _SUPERVISOR_TRANSITIONS:
        return "receipt_authority_transition_invalid"
    return None


def _writer_contract_finding(
    writer_role: object,
    evidence_basis: object,
    transition: object,
    payload: object,
    *,
    legacy: bool = False,
) -> str | None:
    if writer_role not in _WRITER_ROLES:
        return "receipt_writer_role_invalid"
    if evidence_basis not in _EVIDENCE_BASES:
        return "receipt_evidence_basis_invalid"
    if not isinstance(transition, str) or not isinstance(payload, Mapping):
        return "receipt_payload_invalid"
    authority_finding = transition_authority_finding(
        writer_role,
        transition,
        evidence_basis,
        payload,
        legacy=legacy,
    )
    if authority_finding is not None:
        return authority_finding
    return validate_receipt_payload(
        transition,
        payload,
        writer_role=str(writer_role),
        legacy=legacy,
    )


def _key_id(public_key: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_key).hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value)


def _canonical_for_verification(value: Mapping[str, Any]) -> bytes:
    """Reproduce legacy signed bytes while keeping new writes strict JSON."""
    try:
        return _canonical(value)
    except ValueError:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=True,
        ).encode()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return _canonical(value).decode("ascii")


def _fsync_directory(path: Path) -> None:
    """Durably commit directory metadata where the platform exposes it."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        create_file = _windows_dll("kernel32").CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            raise OSError(_windows_last_error(), "receipt_directory_open_failed")
        try:
            if not _windows_dll("kernel32").FlushFileBuffers(handle):
                error = _windows_last_error()
                # Some Windows filesystems reject directory flushes. Atomic
                # replacement remains valid there; file data was flushed first.
                if error not in {1, 5, 87}:
                    raise OSError(error, "receipt_directory_fsync_failed")
        finally:
            _windows_dll("kernel32").CloseHandle(handle)
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int = 0o600,
    observer: Callable[[str], None] | None = None,
    restrict: Callable[[Path], None] | None = None,
) -> None:
    """Write, flush, replace, and directory-flush one security artifact."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("receipt_atomic_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.chmod(temporary, mode)
        if restrict is not None:
            restrict(temporary)
        if observer is not None:
            observer("temporary_fsynced")
        os.replace(temporary, path)
        os.chmod(path, mode)
        if restrict is not None:
            restrict(path)
        if observer is not None:
            observer("replaced")
        _fsync_directory(path.parent)
        if observer is not None:
            observer("directory_fsynced")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _windows_dll(name: str) -> Any:
    import ctypes

    return getattr(ctypes, "WinDLL")(name, use_last_error=True)


def _windows_last_error() -> int:
    import ctypes

    return int(getattr(ctypes, "get_last_error")())


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class TokenUser(ctypes.Structure):
        _fields_ = (("user", SidAndAttributes),)

    advapi32 = _windows_dll("advapi32")
    kernel32 = _windows_dll("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(_windows_last_error(), "open_process_token_failed")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(_windows_last_error(), "token_user_size_failed")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            size,
            ctypes.byref(size),
        ):
            raise OSError(_windows_last_error(), "token_user_read_failed")
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        rendered = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.user.sid, ctypes.byref(rendered)):
            raise OSError(_windows_last_error(), "sid_render_failed")
        try:
            if rendered.value is None:
                raise OSError("sid_render_empty")
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.CloseHandle(token)


def _set_windows_acl_for_sid(path: Path, sid: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = _windows_dll("advapi32")
    kernel32 = _windows_dll("kernel32")
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        None,
    ):
        raise OSError(_windows_last_error(), "security_descriptor_create_failed")
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        owner_defaulted = wintypes.BOOL()
        owner_sid = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner_sid),
            ctypes.byref(owner_defaulted),
        ) or not owner_sid.value:
            raise OSError(_windows_last_error(), "security_descriptor_owner_failed")
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present.value:
            raise OSError(_windows_last_error(), "security_descriptor_dacl_failed")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,
            0x80000005,
            owner_sid,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(result, "private_key_acl_failed")
    finally:
        kernel32.LocalFree(descriptor)


def _set_windows_owner_only_acl(path: Path) -> None:
    _set_windows_acl_for_sid(path, _windows_current_user_sid())


def _windows_acl_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = _windows_dll("advapi32")
    kernel32 = _windows_dll("kernel32")
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000004,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "private_key_acl_read_failed")
    try:
        rendered = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            1,
            0x00000004,
            ctypes.byref(rendered),
            None,
        ):
            raise OSError(_windows_last_error(), "private_key_acl_render_failed")
        try:
            if rendered.value is None:
                raise OSError("private_key_acl_empty")
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.LocalFree(descriptor)


def _windows_acl_is_owner_only(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        )

    class AceHeader(ctypes.Structure):
        _fields_ = (
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        )

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = (
            ("header", AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        )

    advapi32 = _windows_dll("advapi32")
    kernel32 = _windows_dll("kernel32")
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi32.EqualSid.restype = wintypes.BOOL

    descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000005,
        ctypes.byref(owner_sid),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "private_key_acl_read_failed")
    expected_sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(
        _windows_current_user_sid(), ctypes.byref(expected_sid)
    ):
        kernel32.LocalFree(descriptor)
        raise OSError(_windows_last_error(), "private_key_sid_create_failed")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise OSError(_windows_last_error(), "private_key_acl_control_failed")
        if not control.value & 0x1000 or not dacl.value:
            return False
        if not owner_sid.value or not advapi32.EqualSid(owner_sid, expected_sid):
            return False
        info = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), 2
        ):
            raise OSError(_windows_last_error(), "private_key_acl_info_failed")
        if info.ace_count != 1:
            return False
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise OSError(_windows_last_error(), "private_key_acl_ace_failed")
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(AccessAllowedAce)).contents
        if ace.header.ace_type != 0 or ace.header.ace_flags & 0x10:
            return False
        if ace.mask != 0x001F01FF:
            return False
        sid_pointer = ctypes.c_void_p(
            int(ace_pointer.value or 0) + AccessAllowedAce.sid_start.offset
        )
        return bool(advapi32.EqualSid(sid_pointer, expected_sid))
    finally:
        kernel32.LocalFree(expected_sid)
        kernel32.LocalFree(descriptor)


def signing_file_permissions_are_restricted(path: Path) -> bool:
    """Return whether a signing-identity file is limited to the current OS user."""
    if os.name == "nt":
        return _windows_acl_is_owner_only(path)
    return path.stat(follow_symlinks=False).st_mode & 0o077 == 0


def private_key_permissions_are_restricted(path: Path) -> bool:
    """Backward-compatible name for the signing-file permission check."""
    return signing_file_permissions_are_restricted(path)


def _restrict_signing_file(path: Path, failure: str) -> None:
    if os.name == "nt":
        _set_windows_owner_only_acl(path)
    else:
        os.chmod(path, 0o600)
    if not signing_file_permissions_are_restricted(path):
        raise PermissionError(failure)


def _restrict_signing_directory(path: Path, failure: str) -> None:
    if os.name == "nt":
        _set_windows_owner_only_acl(path)
    else:
        os.chmod(path, 0o700)
    if not signing_file_permissions_are_restricted(path):
        raise PermissionError(failure)


def restrict_owner_only_directory(path: Path) -> None:
    """Restrict a sensitive runtime directory to the current OS identity."""
    _restrict_signing_directory(path, "runtime_directory_permissions_unsafe")


def _restrict_private_key(path: Path) -> None:
    _restrict_signing_file(path, "receipt_signing_key_permissions_unsafe")


def _restrict_trust_anchor(path: Path) -> None:
    _restrict_signing_file(path, "receipt_trust_anchor_permissions_unsafe")


def restrict_receipt_trust_anchor(path: Path) -> None:
    """Apply the receipt verifier's platform-specific public-anchor policy."""
    _restrict_trust_anchor(path)


def _manifest_identity_directory(evidence_root: Path, run_id: str) -> Path:
    identity = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return evidence_root / _RUN_IDENTITIES_DIR / identity


def _manifest_anchor_path(evidence_root: Path, run_id: str) -> Path:
    return _manifest_identity_directory(evidence_root, run_id) / "manifest-head.v1.json"


def _manifest_bytes_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_manifest_anchor(
    evidence_root: Path,
    run_id: str,
    trusted_public_key: bytes,
) -> dict[str, Any]:
    directory = _manifest_identity_directory(evidence_root, run_id)
    path = _manifest_anchor_path(evidence_root, run_id)
    for candidate in (directory, path):
        metadata = candidate.stat(follow_symlinks=False)
        type_safe = (
            stat.S_ISDIR(metadata.st_mode)
            if candidate == directory
            else stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
        )
        if not type_safe:
            raise PermissionError("manifest_anchor_unsafe")
        if not signing_file_permissions_are_restricted(candidate):
            raise PermissionError("manifest_anchor_unsafe")
    encoded = path.read_bytes()
    value = json.loads(encoded)
    if not isinstance(value, dict) or encoded != _canonical(value):
        raise ValueError("manifest_anchor_invalid")
    body = dict(value)
    try:
        signature = bytes.fromhex(str(body.pop("root_signature")))
        Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(
            signature,
            _canonical(body),
        )
    except (InvalidSignature, KeyError, ValueError) as exc:
        raise ValueError("manifest_anchor_invalid") from exc
    generation = body.get("manifest_generation")
    if (
        body.get("anchor_schema_version") != _MANIFEST_ANCHOR_SCHEMA_VERSION
        or body.get("run_id") != run_id
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or body.get("root_key_id") != _key_id(trusted_public_key)
    ):
        raise ValueError("manifest_anchor_invalid")
    return value


@dataclass(frozen=True)
class RunKeys:
    manifest: bytes
    orchestrator: bytes
    supervisor: bytes
    operator_gateway: bytes
    recovery: bytes
    artifact: bytes


class RunKeyStore(Protocol):
    def get_or_create(self, run_id: str) -> bytes: ...

    def get_or_create_run_keys(self, run_id: str) -> RunKeys: ...


class ReceiptWriter(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def root(self) -> Path: ...

    @property
    def receipts_path(self) -> Path: ...

    @property
    def sequence(self) -> int: ...

    def append(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
    ) -> dict[str, Any]: ...

    def write_artifact(self, name: str, content: str) -> Path: ...

    def read_artifact(self, path: Path) -> str: ...

    def covered_receipts(self) -> tuple[dict[str, Any], ...]: ...

    def terminalize(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
        expected_sequence: int | None = None,
        expected_manifest_generation: int | None = None,
    ) -> dict[str, Any]: ...

    def seal(self) -> Path: ...

    def resolve_action(
        self,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]: ...

    @staticmethod
    def hash_file(path: Path) -> str: ...


class MemoryRunKeyStore:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._run_keys: dict[str, RunKeys] = {}

    def get_or_create(self, run_id: str) -> bytes:
        del run_id
        return self._key

    def get_or_create_run_keys(self, run_id: str) -> RunKeys:
        if run_id not in self._run_keys:
            self._run_keys[run_id] = RunKeys(
                *(secrets.token_bytes(32) for _ in range(6))
            )
        return self._run_keys[run_id]


class FileRunKeyStore:
    """Persistent run-root signing identity protected by local filesystem ACLs."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root
        self.private_key_path = evidence_root / _PRIVATE_KEY_NAME
        self.run_identities_path = evidence_root / _RUN_IDENTITIES_DIR

    @staticmethod
    def _read_regular_key(path: Path) -> bytes:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("receipt_signing_key_unsafe")
        encoded = path.read_bytes().strip()
        if len(encoded) != 64:
            raise ValueError("receipt_signing_key_invalid")
        try:
            return bytes.fromhex(encoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("receipt_signing_key_invalid") from exc

    def get_or_create(self, run_id: str) -> bytes:
        del run_id
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        try:
            _restrict_private_key(self.private_key_path)
            return self._read_regular_key(self.private_key_path)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            encoded = key.hex().encode("ascii")
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _BINARY
            )
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.private_key_path, flags, 0o600)
            except FileExistsError:
                _restrict_private_key(self.private_key_path)
                return self._read_regular_key(self.private_key_path)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("receipt_signing_key_short_write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _restrict_private_key(self.private_key_path)
            return key

    @staticmethod
    def _get_or_create_secret(path: Path) -> bytes:
        try:
            _restrict_private_key(path)
            return FileRunKeyStore._read_regular_key(path)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            encoded = key.hex().encode("ascii")
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _BINARY
            )
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                _restrict_private_key(path)
                return FileRunKeyStore._read_regular_key(path)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("receipt_run_key_short_write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _restrict_private_key(path)
            return key

    def get_or_create_run_keys(self, run_id: str) -> RunKeys:
        identity = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self.run_identities_path.mkdir(parents=True, exist_ok=True)
        _restrict_signing_directory(
            self.run_identities_path,
            "receipt_run_identity_permissions_unsafe",
        )
        directory = self.run_identities_path / identity
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_signing_directory(
            directory,
            "receipt_run_identity_permissions_unsafe",
        )
        return RunKeys(
            manifest=self._get_or_create_secret(directory / "manifest.key"),
            orchestrator=self._get_or_create_secret(directory / "orchestrator.key"),
            supervisor=self._get_or_create_secret(directory / "supervisor.key"),
            operator_gateway=self._get_or_create_secret(
                directory / "operator-gateway.key"
            ),
            recovery=self._get_or_create_secret(directory / "recovery.key"),
            artifact=self._get_or_create_secret(directory / "artifact.key"),
        )


@dataclass(frozen=True)
class Verification:
    ok: bool
    finding: str | None = None


@dataclass(frozen=True)
class StoreVerification:
    status: str
    finding: str | None

    @property
    def exit_code(self) -> int:
        return {
            "verified": 0,
            "live_catching_up": 0,
            "tampered": 3,
            "incomplete": 4,
            "unreadable": 4,
        }[self.status]


class ReceiptChain:
    schema_version = _RECEIPT_SCHEMA_VERSION

    def __init__(
        self,
        evidence_root: Path,
        run_id: str,
        keys: RunKeyStore,
        *,
        profile_version: str,
        policy_version: str,
        commit_observer: Callable[[str], None] | None = None,
    ) -> None:
        if not _valid_run_id(run_id):
            raise ValueError("run_id_invalid")
        self.run_id = run_id
        self.root = evidence_root / run_id
        if self.root.resolve(strict=False).parent != evidence_root.resolve(strict=False):
            raise ValueError("run_id_invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        self.receipts_path = self.root / "receipts.jsonl"
        self.key = keys.get_or_create(run_id)
        self._commit_observer = commit_observer
        self._pin_signing_identity(evidence_root)
        self.run_keys = keys.get_or_create_run_keys(run_id)
        self.certificate_path = self._write_run_certificate()
        self.profile_version = profile_version
        self.policy_version = policy_version
        self.registry = PatternRegistry.default()
        self._lock = RLock()
        self._sequence = 0
        self._previous: str | None = None
        self._sealed = False
        self._manifest_generation = 0
        certificate = json.loads(self.certificate_path.read_text(encoding="utf-8"))
        self._rollback_protected = (
            isinstance(certificate, Mapping)
            and certificate.get("certificate_schema_version")
            in _ROLLBACK_CERTIFICATE_SCHEMA_VERSIONS
            and certificate.get("manifest_rollback_policy")
            == _MANIFEST_ROLLBACK_POLICY
        )
        if self._rollback_protected:
            self._initialize_or_resume_anchored_store()

    def _pin_signing_identity(self, evidence_root: Path) -> None:
        public_key = Ed25519PrivateKey.from_private_bytes(self.key).public_key().public_bytes_raw()
        pin_path = evidence_root / _PUBLIC_KEY_NAME
        encoded = public_key.hex().encode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(pin_path, flags, 0o600)
        except FileExistsError:
            metadata = pin_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("receipt_trust_anchor_unsafe")
            _restrict_trust_anchor(pin_path)
            if not hmac.compare_digest(pin_path.read_bytes().strip(), encoded):
                raise ValueError("receipt_signing_key_mismatch")
            return
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("receipt_trust_anchor_short_write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _restrict_trust_anchor(pin_path)

    def _write_run_certificate(self) -> Path:
        writer_keys = {
            "orchestrator": self.run_keys.orchestrator,
            "supervisor": self.run_keys.supervisor,
            "operator_gateway": self.run_keys.operator_gateway,
            "recovery": self.run_keys.recovery,
        }
        writers: dict[str, dict[str, str]] = {}
        for role, private_bytes in writer_keys.items():
            public_key = (
                Ed25519PrivateKey.from_private_bytes(private_bytes)
                .public_key()
                .public_bytes_raw()
            )
            writers[role] = {
                "key_id": _key_id(public_key),
                "public_key": public_key.hex(),
            }
        manifest_public = (
            Ed25519PrivateKey.from_private_bytes(self.run_keys.manifest)
            .public_key()
            .public_bytes_raw()
        )
        body = {
            "certificate_schema_version": _CERTIFICATE_SCHEMA_VERSION,
            "canonical_encoding_marker": _CANONICAL_ENCODING_MARKER,
            "run_id": self.run_id,
            "manifest_rollback_policy": _MANIFEST_ROLLBACK_POLICY,
            "manifest_key": {
                "key_id": _key_id(manifest_public),
                "public_key": manifest_public.hex(),
            },
            "writers": writers,
            "artifact_key_id": "sha256:"
            + hashlib.sha256(self.run_keys.artifact).hexdigest(),
        }
        signed = {
            **body,
            "root_signature": Ed25519PrivateKey.from_private_bytes(self.key)
            .sign(_canonical(body))
            .hex(),
        }
        target = self.root / _RUN_CERTIFICATE_NAME
        encoded = _canonical(signed)
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("run_certificate_invalid") from exc
            if (
                isinstance(existing, Mapping)
                and existing.get("certificate_schema_version")
                == _LEGACY_CERTIFICATE_SCHEMA_VERSION
            ):
                raise ValueError("run_certificate_legacy_read_only")
            if (
                isinstance(existing, Mapping)
                and existing.get("certificate_schema_version")
                == _ANCHORLESS_CERTIFICATE_SCHEMA_VERSION
            ):
                return target
            if (
                isinstance(existing, Mapping)
                and existing.get("certificate_schema_version")
                == _ROLLBACK_CERTIFICATE_SCHEMA_VERSION
            ):
                raise ValueError("run_certificate_legacy_read_only")
            if not hmac.compare_digest(target.read_bytes(), encoded):
                raise ValueError("run_certificate_mismatch")
            return target
        _atomic_write(target, encoded)
        return target

    def _anchor_body(
        self,
        manifest: Mapping[str, Any] | None,
        manifest_bytes: bytes | None,
    ) -> dict[str, Any]:
        manifest_public = (
            Ed25519PrivateKey.from_private_bytes(self.run_keys.manifest)
            .public_key()
            .public_bytes_raw()
        )
        return {
            "anchor_schema_version": _MANIFEST_ANCHOR_SCHEMA_VERSION,
            "run_id": self.run_id,
            "certificate_hash": self.hash_file(self.certificate_path),
            "manifest_key_id": _key_id(manifest_public),
            "manifest_generation": (
                int(manifest["manifest_generation"]) if manifest is not None else 0
            ),
            "manifest_hash": (
                _manifest_bytes_hash(manifest_bytes)
                if manifest_bytes is not None
                else None
            ),
            "receipt_count": int(manifest["receipt_count"]) if manifest else 0,
            "terminal_receipt_hash": (
                manifest.get("terminal_receipt_hash") if manifest else None
            ),
            "sealed": bool(manifest.get("sealed")) if manifest else False,
            "root_key_id": _key_id(
                Ed25519PrivateKey.from_private_bytes(self.key)
                .public_key()
                .public_bytes_raw()
            ),
        }

    def _write_manifest_anchor(
        self,
        manifest: Mapping[str, Any] | None,
        manifest_bytes: bytes | None,
    ) -> None:
        directory = _manifest_identity_directory(self.root.parent, self.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_signing_directory(directory, "manifest_anchor_unsafe")
        body = self._anchor_body(manifest, manifest_bytes)
        signed = {
            **body,
            "root_signature": Ed25519PrivateKey.from_private_bytes(self.key)
            .sign(_canonical(body))
            .hex(),
        }
        commit_observer = self._commit_observer
        anchor_observer = (
            (lambda stage: commit_observer("anchor_" + stage))
            if commit_observer is not None
            else None
        )
        _atomic_write(
            _manifest_anchor_path(self.root.parent, self.run_id),
            _canonical(signed),
            observer=anchor_observer,
            restrict=_restrict_trust_anchor,
        )

    def _initialize_or_resume_anchored_store(self) -> None:
        anchor_path = _manifest_anchor_path(self.root.parent, self.run_id)
        manifest_path = self.root / "terminal-manifest.json"
        trusted = (
            Ed25519PrivateKey.from_private_bytes(self.key)
            .public_key()
            .public_bytes_raw()
        )
        if not anchor_path.exists():
            if manifest_path.exists() or self.receipts_path.exists():
                raise ValueError("manifest_anchor_missing")
            self._write_manifest_anchor(None, None)
            return
        verification = verify_receipt_store(
            self.root,
            trusted_public_key=trusted,
        ) if manifest_path.exists() or self.receipts_path.exists() else None
        if (
            verification is not None
            and verification.status == "incomplete"
            and verification.finding == "manifest_anchor_update_pending"
        ):
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, Mapping):
                raise ValueError("manifest_anchor_update_invalid")
            self._write_manifest_anchor(manifest, manifest_bytes)
            verification = verify_receipt_store(
                self.root,
                trusted_public_key=trusted,
            )
        if (
            verification is not None
            and verification.status == "live_catching_up"
            and verification.finding == "manifest_coverage_lag"
        ):
            self._recover_authenticated_uncovered_tail()
            verification = verify_receipt_store(
                self.root,
                trusted_public_key=trusted,
            )
        if verification is not None:
            if verification.status != "verified":
                raise ValueError(
                    "receipt_store_not_writable:"
                    + str(verification.finding or verification.status)
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._sequence = int(manifest["receipt_count"])
            self._previous = manifest.get("terminal_receipt_hash")
            self._sealed = bool(manifest.get("sealed"))
            self._manifest_generation = int(manifest["manifest_generation"])

    def _recover_authenticated_uncovered_tail(self) -> None:
        """Advance one anchored manifest over an authenticated crash tail."""
        manifest_path = self.root / "terminal-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping) or bool(manifest.get("sealed")):
            raise ValueError("receipt_store_not_writable:uncovered_tail_invalid")
        covered_count = manifest.get("receipt_count")
        generation = manifest.get("manifest_generation")
        if (
            not isinstance(covered_count, int)
            or isinstance(covered_count, bool)
            or covered_count <= 0
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
        ):
            raise ValueError("receipt_store_not_writable:uncovered_tail_invalid")
        lines = self.receipts_path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= covered_count:
            raise ValueError("receipt_store_not_writable:uncovered_tail_missing")

        try:
            receipts = [json.loads(line) for line in lines]
            if not all(isinstance(receipt, dict) for receipt in receipts):
                raise ValueError("uncovered_tail_invalid")
            previous = manifest.get("terminal_receipt_hash")
            for expected_sequence, raw_receipt in enumerate(
                receipts[covered_count:],
                start=covered_count + 1,
            ):
                receipt = dict(raw_receipt)
                receipt_hash = receipt.pop("receipt_hash")
                if (
                    receipt.get("sequence") != expected_sequence
                    or receipt.get("previous_receipt_hash") != previous
                    or receipt.get("run_id") != self.run_id
                    or receipt.get("schema_version") != self.schema_version
                    or receipt.get("profile_version") != self.profile_version
                    or receipt.get("policy_version") != self.policy_version
                ):
                    raise ValueError("uncovered_tail_continuity_invalid")
                verified_hash = "sha256:" + hashlib.sha256(
                    _canonical_for_verification(receipt)
                ).hexdigest()
                if not isinstance(receipt_hash, str) or not hmac.compare_digest(
                    verified_hash,
                    receipt_hash,
                ):
                    raise ValueError("uncovered_tail_hash_invalid")
                writer_role = receipt.get("writer_role")
                evidence_basis = receipt.get("evidence_basis")
                writer_finding = _writer_contract_finding(
                    writer_role,
                    evidence_basis,
                    receipt.get("transition"),
                    receipt.get("payload"),
                )
                if writer_finding is not None or writer_role not in _WRITER_ROLES:
                    raise ValueError("uncovered_tail_writer_invalid")
                writer_private = getattr(self.run_keys, str(writer_role))
                writer_public = (
                    Ed25519PrivateKey.from_private_bytes(writer_private)
                    .public_key()
                    .public_bytes_raw()
                )
                if receipt.get("writer_key_id") != _key_id(writer_public):
                    raise ValueError("uncovered_tail_writer_invalid")
                writer_signature = bytes.fromhex(str(receipt.get("writer_signature")))
                writer_body = dict(receipt)
                writer_body.pop("writer_signature")
                Ed25519PublicKey.from_public_bytes(writer_public).verify(
                    writer_signature,
                    _canonical_for_verification(writer_body),
                )
                payload = receipt.get("payload")
                if isinstance(payload, Mapping) and "artifact" in payload:
                    resolved_root = self.root.resolve()
                    artifact = (
                        resolved_root / str(payload["artifact"])
                    ).resolve(strict=False)
                    if (
                        not artifact.is_relative_to(resolved_root)
                        or not artifact.exists()
                        or self.hash_file(artifact) != payload.get("artifact_hash")
                    ):
                        raise ValueError("uncovered_tail_artifact_invalid")
                previous = receipt_hash
            lifecycle_finding = validate_v2_receipt_contract(receipts, sealed=False)
            if lifecycle_finding is not None:
                raise ValueError(lifecycle_finding)
        except (InvalidSignature, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "receipt_store_not_writable:uncovered_tail_invalid"
            ) from exc
        except ValueError as exc:
            raise ValueError(
                "receipt_store_not_writable:uncovered_tail_invalid"
            ) from exc

        old_sequence = self._sequence
        old_previous = self._previous
        old_generation = self._manifest_generation
        self._sequence = len(receipts)
        self._previous = previous
        self._manifest_generation = generation
        try:
            self._write_manifest(sealed=False)
        except Exception:
            self._sequence = old_sequence
            self._previous = old_previous
            self._manifest_generation = old_generation
            raise

    @staticmethod
    def hash_file(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def sequence(self) -> int:
        """Return the last successfully appended receipt sequence."""
        return self._sequence

    @staticmethod
    def _hash(payload: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()

    def _assert_store_writable(self) -> None:
        manifest = self.root / "terminal-manifest.json"
        if not self.receipts_path.exists() and not manifest.exists():
            return
        if not self._rollback_protected:
            raise ValueError("receipt_store_legacy_read_only")
        trusted = (
            Ed25519PrivateKey.from_private_bytes(self.key)
            .public_key()
            .public_bytes_raw()
        )
        verification = verify_receipt_store(
            self.root,
            trusted_public_key=trusted,
        )
        if verification.status != "verified":
            raise ValueError(
                "receipt_store_not_writable:"
                + str(verification.finding or verification.status)
            )

    def _sanitize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
        except ValueError as exc:
            raise ValueError("receipt_payload_non_finite") from exc
        except TypeError as exc:
            # A payload the encoder cannot represent is a governed refusal like
            # any other, not an escaping TypeError callers have to special-case.
            raise ValueError("receipt_payload_unserializable") from exc
        clean, _ = self.registry.scan(serialized)
        value = json.loads(clean)
        if not isinstance(value, dict):
            raise TypeError("receipt payload must be an object")
        return value

    def append(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
    ) -> dict[str, Any]:
        if self._sealed:
            raise ValueError("receipt_after_terminal_decision")
        if evidence_basis is None:
            decision = payload.get("decision") if transition == "run_decision" else None
            rule = transition_rule(writer_role, transition, decision)
            evidence_basis = rule.evidence_basis if rule is not None else "observed"
        writer_finding = _writer_contract_finding(
            writer_role,
            evidence_basis,
            transition,
            payload,
        )
        if writer_finding is not None:
            raise ValueError(writer_finding)
        with self._lock:
            self._assert_store_writable()
            clean = self._sanitize(payload)
            payload_finding = validate_receipt_payload(
                transition,
                clean,
                writer_role=writer_role,
            )
            if payload_finding is not None:
                raise ValueError(payload_finding)
            self._sequence += 1
            writer_private = getattr(self.run_keys, writer_role)
            writer_public = (
                Ed25519PrivateKey.from_private_bytes(writer_private)
                .public_key()
                .public_bytes_raw()
            )
            receipt = {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "profile_version": self.profile_version,
                "policy_version": self.policy_version,
                "sequence": self._sequence,
                "previous_receipt_hash": self._previous,
                "transition": transition,
                "writer_role": writer_role,
                "evidence_basis": evidence_basis,
                "writer_key_id": _key_id(writer_public),
                "payload": clean,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            writer_signature = (
                Ed25519PrivateKey.from_private_bytes(writer_private)
                .sign(_canonical(receipt))
                .hex()
            )
            signed_receipt = {**receipt, "writer_signature": writer_signature}
            receipt_hash = self._hash(signed_receipt)
            envelope = {**signed_receipt, "receipt_hash": receipt_hash}
            prior_receipts: list[dict[str, Any]] = []
            if self.receipts_path.exists():
                prior_receipts = [
                    json.loads(line)
                    for line in self.receipts_path.read_text(encoding="utf-8").splitlines()
                ]
            lifecycle_finding = validate_v2_receipt_contract(
                [*prior_receipts, envelope],
                sealed=False,
            )
            if lifecycle_finding is not None:
                self._sequence -= 1
                raise ValueError(lifecycle_finding)
            with self.receipts_path.open("a", encoding="utf-8") as stream:
                stream.write(_canonical_json(envelope) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                if self._commit_observer is not None:
                    self._commit_observer("receipt_fsynced")
            os.chmod(self.receipts_path, 0o600)
            self._previous = receipt_hash
            self._write_manifest(sealed=False)
            return envelope

    def resolve_action(
        self,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        """Atomically resolve one sealed action contract and close the run."""
        if not action_id or not resolution or not resolver_identity:
            raise ValueError("action_resolution_invalid")
        with self._lock:
            self._assert_store_writable()
            opened_sequence: int | None = None
            outcome_map: dict[str, str] | None = None
            already_resolved = False
            for receipt in self.covered_receipts():
                payload = receipt.get("payload", {})
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("action_id") != action_id
                ):
                    continue
                if receipt.get("transition") == "action_opened":
                    opened_sequence = int(receipt["sequence"])
                    candidate_map = payload.get("outcome_map")
                    if isinstance(candidate_map, Mapping):
                        outcome_map = {
                            str(key): str(value)
                            for key, value in candidate_map.items()
                        }
                elif receipt.get("transition") == "action_resolved":
                    already_resolved = True
            if opened_sequence is None:
                raise ValueError("action_open_missing")
            if already_resolved:
                raise ValueError("action_already_resolved")
            if outcome_map is None or resolution not in outcome_map:
                raise ValueError("action_resolution_unauthorized")
            resolved = self.append(
                "action_resolved",
                {
                    "action_id": action_id,
                    "resolution": resolution,
                    "resolver_identity": resolver_identity,
                    "opened_sequence": opened_sequence,
                    "provider_dispatch": False,
                },
                writer_role="operator_gateway",
                evidence_basis="submitted",
            )
            decision = self.terminalize(
                "run_decision",
                {
                    "decision": outcome_map[resolution],
                    "outcome": resolution,
                    "action_id": action_id,
                    "action_resolved_sequence": resolved["sequence"],
                    "provider_dispatch": False,
                },
                writer_role="operator_gateway",
                evidence_basis="derived",
            )
            self.seal()
            return {
                "action_id": action_id,
                "action_resolved_sequence": resolved["sequence"],
                "run_decision_sequence": decision["sequence"],
                "status": outcome_map[resolution],
            }

    def write_artifact(self, name: str, content: str) -> Path:
        with self._lock:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None:
                raise ValueError("artifact_name_invalid")
            clean, _ = self.registry.scan(content)
            nonce = secrets.token_bytes(12)
            plain = clean.encode()
            cipher = AESGCM(self.run_keys.artifact).encrypt(
                nonce,
                plain,
                self.run_id.encode("utf-8"),
            )
            artifact_root = self.root / "artifacts"
            artifact_root.mkdir(parents=True, exist_ok=True)
            if artifact_root.resolve() != self.root.resolve() / "artifacts":
                raise ValueError("artifact_directory_unsafe")
            target = artifact_root / (name + ".enc")
            if target.exists():
                raise ValueError("artifact_name_collision")
            _atomic_write(target, _ARTIFACT_FORMAT + nonce + cipher)
            return target

    def terminalize(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
        expected_sequence: int | None = None,
        expected_manifest_generation: int | None = None,
    ) -> dict[str, Any]:
        """Atomically close command admission, finalize pending input, and decide."""
        if transition not in {"run_decision", "run_abandoned"}:
            raise ValueError("terminal_transition_invalid")
        with self._lock:
            sequence_changed = (
                expected_sequence is not None
                and expected_sequence != self._sequence
            )
            generation_changed = (
                expected_manifest_generation is not None
                and expected_manifest_generation != self._manifest_generation
            )
            if sequence_changed or generation_changed:
                raise ValueError("recovery_confirmation_stale")
            receipts = self.covered_receipts()
            finalized = {
                str(receipt.get("payload", {}).get("command_id"))
                for receipt in receipts
                if receipt.get("transition") in {"context_injected", "command_unapplied"}
                and isinstance(receipt.get("payload"), Mapping)
            }
            pending = [
                receipt
                for receipt in receipts
                if receipt.get("transition") == "command_accepted"
                and isinstance(receipt.get("payload"), Mapping)
                and str(receipt["payload"].get("command_id")) not in finalized
            ]
            if pending:
                self.append(
                    "terminalization_started",
                    {
                        "reason": "pending_commands",
                        "provider_dispatch": False,
                    },
                    writer_role="orchestrator",
                    evidence_basis="derived",
                )
                for receipt in pending:
                    command = receipt["payload"]
                    assert isinstance(command, Mapping)
                    self.append(
                        "command_unapplied",
                        {
                            "command_id": command["command_id"],
                            "accepted_sequence": receipt["sequence"],
                            "target_role": command["target_role"],
                            "reason": "run_terminating",
                            "last_covered_sequence": self.sequence,
                            "provider_dispatch": False,
                        },
                        writer_role="orchestrator",
                        evidence_basis="derived",
                    )
            final_payload = dict(payload)
            if transition == "run_abandoned" and "last_covered_sequence" not in final_payload:
                final_payload["last_covered_sequence"] = self.sequence
            return self.append(
                transition,
                final_payload,
                writer_role=writer_role,
                evidence_basis=evidence_basis,
            )

    def read_artifact(self, path: Path) -> str:
        """Authenticate, decrypt, and decode an artifact belonging to this run."""
        resolved_root = self.root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ValueError("artifact_path_escape")
        payload = resolved.read_bytes()
        if not payload.startswith(_ARTIFACT_FORMAT) or len(payload) < 38:
            raise ValueError("artifact_format_invalid")
        offset = len(_ARTIFACT_FORMAT)
        nonce = payload[offset : offset + 12]
        cipher = payload[offset + 12 :]
        plain = AESGCM(self.run_keys.artifact).decrypt(
            nonce,
            cipher,
            self.run_id.encode("utf-8"),
        )
        return plain.decode("utf-8")

    def covered_receipts(self) -> tuple[dict[str, Any], ...]:
        """Return only the receipt prefix committed by the signed manifest."""
        with self._lock:
            manifest = json.loads(
                (self.root / "terminal-manifest.json").read_text(encoding="utf-8")
            )
            count = manifest.get("receipt_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("manifest_receipt_count_invalid")
            lines = self.receipts_path.read_text(encoding="utf-8").splitlines()
            if len(lines) < count:
                raise ValueError("receipt_chain_truncated")
            return tuple(json.loads(line) for line in lines[:count])

    def _write_manifest(self, *, sealed: bool) -> Path:
        target = self.root / "terminal-manifest.json"
        previous_manifest_hash = (
            _manifest_bytes_hash(target.read_bytes()) if target.exists() else None
        )
        next_generation = self._manifest_generation + 1
        manifest = {
            "schema_version": self.schema_version,
            "canonical_encoding_marker": _CANONICAL_ENCODING_MARKER,
            "run_id": self.run_id,
            "manifest_generation": next_generation,
            "previous_manifest_hash": previous_manifest_hash,
            "terminal_receipt_hash": self._previous,
            "receipt_count": self._sequence,
            "sealed": sealed,
            "manifest_key_id": _key_id(
                Ed25519PrivateKey.from_private_bytes(self.run_keys.manifest)
                .public_key()
                .public_bytes_raw()
            ),
            "certificate_hash": self.hash_file(self.certificate_path),
        }
        private = Ed25519PrivateKey.from_private_bytes(self.run_keys.manifest)
        signed = {
            **manifest,
            "signature": private.sign(_canonical(manifest)).hex(),
        }
        encoded = _canonical(signed)
        _atomic_write(target, encoded, observer=self._commit_observer)
        self._write_manifest_anchor(signed, encoded)
        self._manifest_generation = next_generation
        return target

    def seal(self) -> Path:
        with self._lock:
            self._assert_store_writable()
            receipts = [
                json.loads(line)
                for line in self.receipts_path.read_text(encoding="utf-8").splitlines()
            ]
            lifecycle_finding = validate_v2_receipt_contract(receipts, sealed=True)
            if lifecycle_finding is not None:
                raise ValueError(lifecycle_finding)
            target = self._write_manifest(sealed=True)
            self._sealed = True
            return target

    def verify(self, manifest_path: Path) -> Verification:
        trusted_public_key = (
            Ed25519PrivateKey.from_private_bytes(self.key)
            .public_key()
            .public_bytes_raw()
        )
        result = verify_receipt_store(
            manifest_path.parent,
            trusted_public_key=trusted_public_key,
        )
        return Verification(
            result.status in {"verified", "live_catching_up"},
            result.finding,
        )


def _trusted_public_key_from_private_identity(evidence_root: Path) -> bytes:
    private_path = evidence_root / _PRIVATE_KEY_NAME
    metadata = private_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("receipt_signing_key_unsafe")
    if not signing_file_permissions_are_restricted(private_path):
        raise PermissionError("receipt_signing_key_permissions_unsafe")
    encoded = private_path.read_bytes().strip()
    if len(encoded) != 64:
        raise ValueError("receipt_signing_key_invalid")
    private_key = bytes.fromhex(encoded.decode("ascii"))
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes_raw()
    )


def verify_receipt_store(
    root: Path,
    *,
    trusted_public_key: bytes | None = None,
    external_trust: bool = False,
) -> StoreVerification:
    receipts_path = root / "receipts.jsonl"
    manifest_path = root / "terminal-manifest.json"
    if not receipts_path.exists() or not manifest_path.exists():
        return StoreVerification("incomplete", "evidence_missing")
    try:
        manifest_bytes = manifest_path.read_bytes()
        signed_preview = json.loads(manifest_bytes)
        if not isinstance(signed_preview, dict):
            return StoreVerification("unreadable", "evidence_unreadable")
        receipt_count = signed_preview.get("receipt_count")
        if not isinstance(receipt_count, int) or isinstance(receipt_count, bool):
            return StoreVerification("unreadable", "manifest_receipt_count_invalid")
        all_lines = receipts_path.read_text(encoding="utf-8").splitlines()
        if not all_lines or receipt_count <= 0:
            return StoreVerification("incomplete", "receipt_chain_truncated")
        if len(all_lines) < receipt_count:
            return StoreVerification("incomplete", "receipt_chain_truncated")
        uncovered_tail = len(all_lines) > receipt_count
        if uncovered_tail and bool(signed_preview.get("sealed")):
            return StoreVerification("tampered", "receipt_after_terminal_decision")
        lines = all_lines[:receipt_count]
        previous: str | None = None
        versions: tuple[object, object, object] | None = None
        receipts: list[dict[str, Any]] = []
        for expected_sequence, line in enumerate(lines, start=1):
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                return StoreVerification("unreadable", "evidence_unreadable")
            receipt_hash = envelope.pop("receipt_hash")
            if envelope.get("sequence") != expected_sequence:
                return StoreVerification("tampered", "sequence_discontinuity")
            if envelope.get("previous_receipt_hash") != previous:
                return StoreVerification("tampered", "receipt_chain_broken")
            verified_hash = "sha256:" + hashlib.sha256(
                _canonical_for_verification(envelope)
            ).hexdigest()
            if verified_hash != receipt_hash:
                return StoreVerification("tampered", "receipt_hash_mismatch")
            schema_version = envelope.get("schema_version")
            if schema_version in {
                _LEGACY_RECEIPT_SCHEMA_VERSION,
                _AUTHORITY_RECEIPT_SCHEMA_VERSION,
            }:
                authority_finding = _receipt_authority_finding(
                    schema_version,
                    envelope.get("transition"),
                    envelope.get("authority"),
                )
                if authority_finding is not None:
                    return StoreVerification("tampered", authority_finding)
            elif schema_version != _RECEIPT_SCHEMA_VERSION:
                return StoreVerification("unreadable", "receipt_schema_unsupported")
            current_versions = (envelope.get("schema_version"), envelope.get("profile_version"), envelope.get("policy_version"))
            if versions is not None and current_versions != versions:
                return StoreVerification("tampered", "version_inconsistency")
            versions = current_versions
            payload = envelope.get("payload", {})
            if isinstance(payload, dict) and "artifact" in payload:
                resolved_root = root.resolve()
                artifact = (resolved_root / str(payload["artifact"])).resolve(strict=False)
                if not artifact.is_relative_to(resolved_root):
                    return StoreVerification("tampered", "artifact_path_escape")
                if not artifact.exists() or ReceiptChain.hash_file(artifact) != payload.get("artifact_hash"):
                    return StoreVerification("tampered", "artifact_hash_mismatch")
            previous = receipt_hash
            receipts.append(envelope)

        if external_trust and trusted_public_key is None:
            return StoreVerification("incomplete", "external_trust_key_missing")
        if trusted_public_key is None:
            try:
                trusted_public_key = _trusted_public_key_from_private_identity(root.parent)
            except FileNotFoundError:
                return StoreVerification("incomplete", "trust_identity_missing")
            except (OSError, PermissionError, ValueError):
                return StoreVerification("tampered", "trust_identity_unsafe")
        if not external_trust:
            pin_path = root.parent / _PUBLIC_KEY_NAME
            try:
                pin_metadata = pin_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                return StoreVerification("incomplete", "trust_anchor_missing")
            if not stat.S_ISREG(pin_metadata.st_mode) or pin_metadata.st_nlink != 1:
                return StoreVerification("tampered", "trust_anchor_unsafe")
            try:
                anchor_restricted = signing_file_permissions_are_restricted(pin_path)
            except OSError:
                return StoreVerification("tampered", "trust_anchor_unsafe")
            if not anchor_restricted:
                return StoreVerification("tampered", "trust_anchor_unsafe")
            pinned_public_key = bytes.fromhex(
                pin_path.read_text(encoding="ascii").strip()
            )
            if not hmac.compare_digest(pinned_public_key, trusted_public_key):
                return StoreVerification("tampered", "trust_anchor_substituted")

        signed = json.loads(manifest_bytes)
        if not isinstance(signed, dict):
            return StoreVerification("unreadable", "evidence_unreadable")
        signature = bytes.fromhex(str(signed.pop("signature")))
        if signed.get("terminal_receipt_hash") != previous or signed.get("receipt_count") != len(lines):
            return StoreVerification("incomplete", "manifest_coverage_mismatch")
        schema_version = versions[0] if versions is not None else None
        if schema_version == _RECEIPT_SCHEMA_VERSION:
            certificate_path = root / _RUN_CERTIFICATE_NAME
            if not certificate_path.exists():
                return StoreVerification("incomplete", "run_certificate_missing")
            certificate_bytes = certificate_path.read_bytes()
            certificate = json.loads(certificate_bytes)
            if not isinstance(certificate, dict):
                return StoreVerification("unreadable", "run_certificate_invalid")
            certificate_file_hash = ReceiptChain.hash_file(certificate_path)
            full_certificate = dict(certificate)
            root_signature = bytes.fromhex(str(certificate.pop("root_signature")))
            try:
                Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(
                    root_signature,
                    _canonical(certificate),
                )
            except InvalidSignature:
                return StoreVerification("tampered", "run_certificate_signature_invalid")
            certificate_schema_version = certificate.get(
                "certificate_schema_version"
            )
            if (
                certificate_schema_version not in {
                    _LEGACY_CERTIFICATE_SCHEMA_VERSION,
                    _ANCHORLESS_CERTIFICATE_SCHEMA_VERSION,
                    _ROLLBACK_CERTIFICATE_SCHEMA_VERSION,
                    _CERTIFICATE_SCHEMA_VERSION,
                }
                or certificate.get("run_id") != signed.get("run_id")
                or certificate.get("run_id") != root.name
                or signed.get("schema_version") != _RECEIPT_SCHEMA_VERSION
                or signed.get("certificate_hash")
                != certificate_file_hash
                or (
                    certificate_schema_version == _CERTIFICATE_SCHEMA_VERSION
                    and (
                        certificate.get("canonical_encoding_marker")
                        != _CANONICAL_ENCODING_MARKER
                        or signed.get("canonical_encoding_marker")
                        != _CANONICAL_ENCODING_MARKER
                    )
                )
            ):
                return StoreVerification("tampered", "run_certificate_invalid")
            if (
                certificate_schema_version
                not in _ROLLBACK_CERTIFICATE_SCHEMA_VERSIONS
                and _manifest_anchor_path(
                    root.parent,
                    str(certificate.get("run_id")),
                ).exists()
            ):
                return StoreVerification("tampered", "run_certificate_rollback_detected")
            if (
                certificate_schema_version in {
                    _ANCHORLESS_CERTIFICATE_SCHEMA_VERSION,
                    _ROLLBACK_CERTIFICATE_SCHEMA_VERSION,
                    _CERTIFICATE_SCHEMA_VERSION,
                }
                and certificate_bytes != _canonical(full_certificate)
            ):
                return StoreVerification("tampered", "run_certificate_noncanonical")
            manifest_key = certificate.get("manifest_key")
            writers = certificate.get("writers")
            if not isinstance(manifest_key, dict) or not isinstance(writers, dict):
                return StoreVerification("tampered", "run_certificate_invalid")
            manifest_public = bytes.fromhex(str(manifest_key.get("public_key")))
            if (
                manifest_key.get("key_id") != _key_id(manifest_public)
                or signed.get("manifest_key_id") != manifest_key.get("key_id")
            ):
                return StoreVerification("tampered", "manifest_key_untrusted")
            try:
                Ed25519PublicKey.from_public_bytes(manifest_public).verify(
                    signature,
                    _canonical(signed),
                )
            except InvalidSignature:
                return StoreVerification("tampered", "manifest_signature_invalid")
            if certificate_schema_version in _ROLLBACK_CERTIFICATE_SCHEMA_VERSIONS:
                if (
                    certificate.get("manifest_rollback_policy")
                    != _MANIFEST_ROLLBACK_POLICY
                    or manifest_bytes != _canonical(signed_preview)
                ):
                    return StoreVerification("tampered", "manifest_anchor_invalid")
                generation = signed.get("manifest_generation")
                if (
                    not isinstance(generation, int)
                    or isinstance(generation, bool)
                    or generation <= 0
                ):
                    return StoreVerification("tampered", "manifest_generation_invalid")
                try:
                    anchor = _read_manifest_anchor(
                        root.parent,
                        str(signed.get("run_id")),
                        trusted_public_key,
                    )
                except FileNotFoundError:
                    return StoreVerification("tampered", "manifest_anchor_missing")
                except PermissionError:
                    return StoreVerification("tampered", "manifest_anchor_unsafe")
                except (OSError, ValueError, json.JSONDecodeError):
                    return StoreVerification("tampered", "manifest_anchor_invalid")
                anchor_body = dict(anchor)
                anchor_body.pop("root_signature", None)
                manifest_hash = _manifest_bytes_hash(manifest_bytes)
                binding_matches = (
                    anchor_body.get("certificate_hash") == certificate_file_hash
                    and anchor_body.get("manifest_key_id")
                    == manifest_key.get("key_id")
                )
                exact_head = (
                    anchor_body.get("manifest_generation") == generation
                    and anchor_body.get("manifest_hash") == manifest_hash
                    and anchor_body.get("receipt_count") == signed.get("receipt_count")
                    and anchor_body.get("terminal_receipt_hash")
                    == signed.get("terminal_receipt_hash")
                    and anchor_body.get("sealed") == bool(signed.get("sealed"))
                )
                if not binding_matches:
                    return StoreVerification("tampered", "manifest_anchor_invalid")
                if not exact_head:
                    pending = (
                        generation
                        == int(anchor_body.get("manifest_generation", -2)) + 1
                        and signed.get("previous_manifest_hash")
                        == anchor_body.get("manifest_hash")
                    )
                    if pending:
                        return StoreVerification(
                            "incomplete",
                            "manifest_anchor_update_pending",
                        )
                    return StoreVerification("tampered", "manifest_rollback_detected")
            if set(writers) != _WRITER_ROLES:
                return StoreVerification("tampered", "run_certificate_invalid")
            certified_key_ids = {str(manifest_key.get("key_id"))}
            certified_key_ids.update(
                str(writer.get("key_id"))
                for writer in writers.values()
                if isinstance(writer, dict)
            )
            artifact_key_id = certificate.get("artifact_key_id")
            if (
                not isinstance(artifact_key_id, str)
                or not artifact_key_id.startswith("sha256:")
                or artifact_key_id in certified_key_ids
            ):
                return StoreVerification("tampered", "run_certificate_invalid")
            for receipt in receipts:
                if (
                    certificate_schema_version
                    in _ROLLBACK_CERTIFICATE_SCHEMA_VERSIONS
                    and receipt.get("run_id") != certificate.get("run_id")
                ):
                    return StoreVerification("tampered", "receipt_run_id_mismatch")
                writer_role = receipt.get("writer_role")
                evidence_basis = receipt.get("evidence_basis")
                writer_finding = _writer_contract_finding(
                    writer_role,
                    evidence_basis,
                    receipt.get("transition"),
                    receipt.get("payload"),
                    legacy=(
                        certificate_schema_version
                        == _LEGACY_CERTIFICATE_SCHEMA_VERSION
                    ),
                )
                if writer_finding is not None:
                    return StoreVerification("tampered", writer_finding)
                writer = writers.get(writer_role)
                if not isinstance(writer, dict):
                    return StoreVerification("tampered", "receipt_writer_key_unknown")
                writer_public = bytes.fromhex(str(writer.get("public_key")))
                if (
                    writer.get("key_id") != _key_id(writer_public)
                    or receipt.get("writer_key_id") != writer.get("key_id")
                ):
                    return StoreVerification("tampered", "receipt_writer_key_unknown")
                writer_signature = bytes.fromhex(str(receipt.get("writer_signature")))
                writer_body = dict(receipt)
                writer_body.pop("writer_signature")
                try:
                    Ed25519PublicKey.from_public_bytes(writer_public).verify(
                        writer_signature,
                    _canonical_for_verification(writer_body),
                    )
                except InvalidSignature:
                    return StoreVerification("tampered", "receipt_writer_signature_invalid")
            lifecycle_finding = validate_v2_receipt_contract(
                receipts,
                sealed=bool(signed.get("sealed")),
                legacy=(
                    certificate_schema_version
                    == _LEGACY_CERTIFICATE_SCHEMA_VERSION
                ),
            )
            if lifecycle_finding is not None:
                return StoreVerification("tampered", lifecycle_finding)
        else:
            public_key = bytes.fromhex(str(signed.pop("public_key")))
            if not hmac.compare_digest(public_key, trusted_public_key):
                return StoreVerification("tampered", "manifest_signer_untrusted")
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                _canonical(signed),
            )
        return StoreVerification(
            "live_catching_up" if uncovered_tail else "verified",
            "manifest_coverage_lag" if uncovered_tail else None,
        )
    except InvalidSignature:
        return StoreVerification("tampered", "manifest_signature_invalid")
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return StoreVerification("incomplete", "evidence_unreadable")
