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
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from torq_cli.core.redaction import PatternRegistry


_PRIVATE_KEY_NAME = ".torq-receipt-signing-key"
_PUBLIC_KEY_NAME = ".torq-receipt-signing-key.pub"


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


def _set_windows_owner_only_acl(path: Path) -> None:
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
    sddl = f"D:P(A;;FA;;;{_windows_current_user_sid()})"
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
        dacl = ctypes.c_void_p()
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
            0x80000004,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(result, "private_key_acl_failed")
    finally:
        kernel32.LocalFree(descriptor)


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


def private_key_permissions_are_restricted(path: Path) -> bool:
    """Return whether the private key is limited to the current OS user."""
    if os.name == "nt":
        sid = _windows_current_user_sid()
        rendered = _windows_acl_sddl(path)
        ace_start = rendered.find("(")
        if ace_start < 0:
            return False
        control = rendered[2:ace_start] if rendered.startswith("D:") else ""
        # P is the protected-DACL flag. Other control metadata (for example AI)
        # varies across Windows versions and filesystems, but the ACE list must
        # still be exactly one allow/full-control entry for the current SID.
        owner_ace = rf"\(A;;(?:FA|GA|0x1f01ff);;;{re.escape(sid)}\)"
        return "P" in control and re.fullmatch(owner_ace, rendered[ace_start:]) is not None
    return path.stat(follow_symlinks=False).st_mode & 0o077 == 0


def _restrict_private_key(path: Path) -> None:
    if os.name == "nt":
        _set_windows_owner_only_acl(path)
    else:
        os.chmod(path, 0o600)
    if not private_key_permissions_are_restricted(path):
        raise PermissionError("receipt_signing_key_permissions_unsafe")


class RunKeyStore(Protocol):
    def get_or_create(self, run_id: str) -> bytes: ...


class MemoryRunKeyStore:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def get_or_create(self, run_id: str) -> bytes:
        del run_id
        return self._key


class FileRunKeyStore:
    """Persistent run-root signing identity protected by local filesystem ACLs."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root
        self.private_key_path = evidence_root / _PRIVATE_KEY_NAME

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
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
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
        return {"verified": 0, "tampered": 3, "incomplete": 4}[self.status]


class ReceiptChain:
    schema_version = "1.0.0"

    def __init__(self, evidence_root: Path, run_id: str, keys: RunKeyStore, *, profile_version: str, policy_version: str) -> None:
        self.run_id = run_id
        self.root = evidence_root / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.receipts_path = self.root / "receipts.jsonl"
        self.key = keys.get_or_create(run_id)
        self._pin_signing_identity(evidence_root)
        self.profile_version = profile_version
        self.policy_version = policy_version
        self.registry = PatternRegistry.default()
        self._sequence = 0
        self._previous: str | None = None

    def _pin_signing_identity(self, evidence_root: Path) -> None:
        public_key = Ed25519PrivateKey.from_private_bytes(self.key).public_key().public_bytes_raw()
        pin_path = evidence_root / _PUBLIC_KEY_NAME
        encoded = public_key.hex().encode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(pin_path, flags, 0o600)
        except FileExistsError:
            metadata = pin_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("receipt_trust_anchor_unsafe")
            if not hmac.compare_digest(pin_path.read_bytes().strip(), encoded):
                raise ValueError("receipt_signing_key_mismatch")
            return
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("receipt_trust_anchor_short_write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(pin_path, 0o600)

    @staticmethod
    def hash_file(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _hash(payload: Mapping[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def _sanitize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(payload, sort_keys=True)
        clean, _ = self.registry.scan(serialized)
        value = json.loads(clean)
        if not isinstance(value, dict):
            raise TypeError("receipt payload must be an object")
        return value

    def append(self, transition: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        clean = self._sanitize(payload)
        self._sequence += 1
        receipt = {
            "schema_version": self.schema_version,
            "profile_version": self.profile_version,
            "policy_version": self.policy_version,
            "sequence": self._sequence,
            "previous_receipt_hash": self._previous,
            "transition": transition,
            "payload": clean,
        }
        receipt_hash = self._hash(receipt)
        envelope = {**receipt, "receipt_hash": receipt_hash}
        with self.receipts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(envelope, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(self.receipts_path, 0o600)
        self._previous = receipt_hash
        return envelope

    def write_artifact(self, name: str, content: str) -> Path:
        clean, _ = self.registry.scan(content)
        nonce = secrets.token_bytes(16)
        plain = clean.encode()
        stream = bytearray()
        counter = 0
        while len(stream) < len(plain):
            stream.extend(hmac.new(self.key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
            counter += 1
        cipher = bytes(a ^ b for a, b in zip(plain, stream, strict=False))
        target = self.root / "artifacts" / (name + ".enc")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(nonce + cipher)
        os.chmod(target, 0o600)
        return target

    def seal(self) -> Path:
        manifest = {"run_id": self.run_id, "terminal_receipt_hash": self._previous, "receipt_count": self._sequence}
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        private = Ed25519PrivateKey.from_private_bytes(self.key)
        signed = {
            **manifest,
            "public_key": private.public_key().public_bytes_raw().hex(),
            "signature": private.sign(body).hex(),
        }
        target = self.root / "terminal-manifest.json"
        target.write_text(json.dumps(signed, sort_keys=True), encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    def verify(self, manifest_path: Path) -> Verification:
        result = verify_receipt_store(manifest_path.parent)
        return Verification(result.status == "verified", result.finding)


def verify_receipt_store(root: Path) -> StoreVerification:
    receipts_path = root / "receipts.jsonl"
    manifest_path = root / "terminal-manifest.json"
    if not receipts_path.exists() or not manifest_path.exists():
        return StoreVerification("incomplete", "evidence_missing")
    try:
        lines = receipts_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return StoreVerification("incomplete", "receipt_chain_truncated")
        previous: str | None = None
        versions: tuple[str, str, str] | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            envelope = json.loads(line)
            receipt_hash = envelope.pop("receipt_hash")
            if envelope.get("sequence") != expected_sequence:
                return StoreVerification("tampered", "sequence_discontinuity")
            if envelope.get("previous_receipt_hash") != previous:
                return StoreVerification("tampered", "receipt_chain_broken")
            if ReceiptChain._hash(envelope) != receipt_hash:
                return StoreVerification("tampered", "receipt_hash_mismatch")
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
        signed = json.loads(manifest_path.read_text(encoding="utf-8"))
        signature = bytes.fromhex(str(signed.pop("signature")))
        public_key = bytes.fromhex(str(signed.pop("public_key")))
        pin_path = root.parent / _PUBLIC_KEY_NAME
        try:
            pin_metadata = pin_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return StoreVerification("incomplete", "trust_anchor_missing")
        if not stat.S_ISREG(pin_metadata.st_mode) or pin_metadata.st_nlink != 1:
            return StoreVerification("tampered", "trust_anchor_unsafe")
        pinned_public_key = bytes.fromhex(pin_path.read_text(encoding="ascii").strip())
        if not hmac.compare_digest(public_key, pinned_public_key):
            return StoreVerification("tampered", "manifest_signer_untrusted")
        if signed.get("terminal_receipt_hash") != previous or signed.get("receipt_count") != len(lines):
            return StoreVerification("incomplete", "manifest_coverage_mismatch")
        body = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, body)
        return StoreVerification("verified", None)
    except InvalidSignature:
        return StoreVerification("tampered", "manifest_signature_invalid")
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return StoreVerification("incomplete", "evidence_unreadable")
