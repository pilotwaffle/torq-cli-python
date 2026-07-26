"""Attended-only encrypted credential files for headless Linux operators."""

from __future__ import annotations

import base64
import binascii
import errno
import getpass
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)

from torq_cli.safety.receipts import (
    _set_windows_owner_only_acl,
    signing_file_permissions_are_restricted,
)


_REFERENCE: Final = re.compile(r"credref_[0-9a-f]{32}\Z")
_PROVIDERS: Final = frozenset({"anthropic", "openai", "qwen", "moonshot", "zai", "deepseek"})
_ALIASES: Final = {
    "claude": "anthropic", "anthropic": "anthropic", "codex": "openai", "openai": "openai",
    "qwen": "qwen", "alibaba": "qwen", "kimi": "moonshot", "moonshot": "moonshot",
    "zai": "zai", "deepseek": "deepseek",
}
_MAX_RAW: Final = 98_304
_MAX_SECRET: Final = 16_384
_MAX_GENERATION: Final = 9_007_199_254_740_991
_LOCK_SECONDS: Final = 5.0
_BINARY: Final = getattr(os, "O_BINARY", 0)


class HeadlessCredentialError(RuntimeError):
    """Secret-free failure from the encrypted-file backend."""


class OpaqueUnlockError(HeadlessCredentialError):
    """Non-disclosing unlock failure shared by malformed, mismatched, and tampered records."""


PassphraseReader = Callable[[], str]
CommitObserver = Callable[[str], None]


def attended_passphrase() -> str:
    """Read a passphrase only from a local, interactive, no-echo terminal."""
    if os.environ.get("CI") or not sys.stdin.isatty() or not sys.stderr.isatty():
        raise HeadlessCredentialError("attended_unlock_required")
    return getpass.getpass("TORQ credential vault passphrase: ")


def _passphrase_bytes(reader: PassphraseReader) -> bytes:
    try:
        value = unicodedata.normalize("NFC", reader())
        encoded = value.encode("utf-8", errors="strict")
    except (UnicodeError, EOFError, KeyboardInterrupt) as exc:
        raise HeadlessCredentialError("attended_unlock_failed") from exc
    if not encoded or len(encoded) > 1024 or "\x00" in value:
        raise HeadlessCredentialError("attended_unlock_failed")
    return encoded


def _provider(provider: str) -> str:
    normalized = _ALIASES.get(provider.casefold())
    if normalized not in _PROVIDERS:
        raise HeadlessCredentialError("credential_provider_unsupported")
    return normalized


def _reference(value: str) -> str:
    if _REFERENCE.fullmatch(value) is None:
        raise HeadlessCredentialError("credential_ref_invalid")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_b64(value: object, length: int | tuple[int, int]) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", value) is None:
        raise ValueError
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError from exc
    if _b64(decoded) != value:
        raise ValueError
    minimum, maximum = (length, length) if isinstance(length, int) else length
    if not minimum <= len(decoded) <= maximum:
        raise ValueError
    return decoded


def _derive(passphrase: bytes, salt: bytes) -> bytes:
    return Argon2id(
        salt=salt, length=32, iterations=3, lanes=1, memory_cost=65_536,
    ).derive(passphrase)


def _metadata(provider: str, credential_ref: str, generation: int) -> dict[str, object]:
    return {
        "backend": "headless_encrypted_file", "credential_ref": credential_ref,
        "generation": generation, "provider_id": provider,
    }


def _aad(record: Mapping[str, Any]) -> bytes:
    return _canonical({key: value for key, value in record.items() if key != "ciphertext_b64"})


def _record(provider: str, credential_ref: str, generation: int, secret: bytes,
            passphrase: bytes) -> bytes:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(24)
    record: dict[str, Any] = {
        "aead": {"algorithm": "xchacha20poly1305-ietf", "nonce_b64": _b64(nonce)},
        "ciphertext_b64": "",
        "format": "torq-credential-vault",
        "kdf": {"algorithm": "argon2id", "memory_kib": 65_536, "parallelism": 1,
                "salt_b64": _b64(salt), "time_cost": 3, "version": 19},
        "metadata": _metadata(provider, credential_ref, generation),
        "version": 1,
    }
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
        secret, _aad(record), nonce, _derive(passphrase, salt),
    )
    record["ciphertext_b64"] = _b64(ciphertext)
    payload = _canonical(record)
    if not 1 <= len(payload) <= _MAX_RAW:
        raise HeadlessCredentialError("credential_store_failed")
    return payload


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _parse(payload: bytes) -> dict[str, Any]:
    if not 1 <= len(payload) <= _MAX_RAW or payload.startswith(b"\xef\xbb\xbf"):
        raise ValueError
    try:
        record = json.loads(
            payload.decode("ascii"), object_pairs_hook=_strict_object,
            parse_float=lambda _: (_ for _ in ()).throw(ValueError()),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValueError from exc
    if not isinstance(record, dict) or _canonical(record) != payload:
        raise ValueError
    if set(record) != {"aead", "ciphertext_b64", "format", "kdf", "metadata", "version"}:
        raise ValueError
    if (
        record["format"] != "torq-credential-vault"
        or type(record["version"]) is not int
        or record["version"] != 1
    ):
        raise ValueError
    aead, kdf, metadata = record["aead"], record["kdf"], record["metadata"]
    if not isinstance(aead, dict) or set(aead) != {"algorithm", "nonce_b64"}:
        raise ValueError
    if aead["algorithm"] != "xchacha20poly1305-ietf":
        raise ValueError
    _decode_b64(aead["nonce_b64"], 24)
    if not isinstance(kdf, dict) or set(kdf) != {
        "algorithm", "memory_kib", "parallelism", "salt_b64", "time_cost", "version",
    }:
        raise ValueError
    expected_kdf = {"algorithm": "argon2id", "memory_kib": 65_536, "parallelism": 1,
                    "time_cost": 3, "version": 19}
    if any(kdf.get(key) != value or type(kdf.get(key)) is not type(value)
           for key, value in expected_kdf.items()):
        raise ValueError
    _decode_b64(kdf["salt_b64"], 16)
    _decode_b64(record["ciphertext_b64"], (17, 16_400))
    if not isinstance(metadata, dict) or set(metadata) != {
        "backend", "credential_ref", "generation", "provider_id",
    }:
        raise ValueError
    generation = metadata.get("generation")
    if metadata.get("backend") != "headless_encrypted_file" or type(generation) is not int:
        raise ValueError
    if not 1 <= generation <= _MAX_GENERATION:
        raise ValueError
    if not isinstance(metadata.get("credential_ref"), str) or _REFERENCE.fullmatch(
        str(metadata["credential_ref"])
    ) is None or metadata.get("provider_id") not in _PROVIDERS:
        raise ValueError
    return record


def _restrict(path: Path, directory: bool = False) -> None:
    if os.name == "nt":
        _set_windows_owner_only_acl(path)
    else:
        os.chmod(path, 0o700 if directory else 0o600)
    if not signing_file_permissions_are_restricted(path):
        raise PermissionError("credential_store_permissions_unsafe")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class HeadlessEncryptedFileStore:
    """One-record-per-reference attended encrypted vault with bounded locking."""

    backend = "headless_encrypted_file"

    def __init__(self, root: Path, *, passphrase_reader: PassphraseReader = attended_passphrase,
                 lock_seconds: float = _LOCK_SECONDS,
                 commit_observer: CommitObserver | None = None) -> None:
        if "\x00" in str(root):
            raise HeadlessCredentialError("credential_store_path_invalid")
        if not root.is_absolute():
            raise HeadlessCredentialError("credential_store_absolute_required")
        self.root = root
        self._reader = passphrase_reader
        self._lock_seconds = lock_seconds
        self._observer = commit_observer or (lambda _step: None)

    def _path(self, credential_ref: str) -> Path:
        return self.root / f"{_reference(credential_ref)}.tqcv"

    @contextmanager
    def _lock(self, credential_ref: str) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise HeadlessCredentialError("credential_store_unsafe")
        _restrict(self.root, directory=True)
        lock = self.root / f".{credential_ref}.lock"
        deadline = time.monotonic() + self._lock_seconds
        descriptor = -1
        while descriptor < 0:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY, 0o600)
            except OSError as exc:
                # Windows reports sharing/access denial rather than EEXIST for
                # an owner-only lock currently held by another thread/process.
                if exc.errno not in {errno.EACCES, errno.EEXIST} or not lock.exists():
                    raise
                if time.monotonic() >= deadline:
                    raise HeadlessCredentialError("credential_store_locked") from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            _restrict(lock)
            yield
        finally:
            os.close(descriptor)
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def store(self, provider: str, credential_ref: str, secret: str) -> None:
        try:
            self._store(provider, credential_ref, secret, require_existing=False)
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_store_failed") from exc

    def _store(self, provider: str, credential_ref: str, secret: str, *,
               require_existing: bool) -> None:
        provider_id = _provider(provider)
        path = self._path(credential_ref)
        try:
            raw_secret = secret.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HeadlessCredentialError("credential_value_invalid") from exc
        if not raw_secret or len(raw_secret) > _MAX_SECRET or b"\x00" in raw_secret:
            raise HeadlessCredentialError("credential_value_invalid")
        with self._lock(credential_ref):
            exists = path.exists()
            if require_existing and not exists:
                raise HeadlessCredentialError("credential_absent")
            generation = 1
            if exists:
                try:
                    passphrase = _passphrase_bytes(self._reader)
                except HeadlessCredentialError as exc:
                    raise OpaqueUnlockError("credential_unlock_failed") from exc
                current = self._unlock(path, provider_id, credential_ref, passphrase)
                generation = int(current[1]) + 1
                if generation > _MAX_GENERATION:
                    raise HeadlessCredentialError("credential_generation_exhausted")
            else:
                passphrase = _passphrase_bytes(self._reader)
            payload = _record(provider_id, credential_ref, generation, raw_secret, passphrase)
            self._atomic_write(path, payload)

    def rotate(self, provider: str, credential_ref: str, secret: str) -> None:
        try:
            self._store(provider, credential_ref, secret, require_existing=True)
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_store_failed") from exc

    def resolve(self, provider: str, credential_ref: str) -> str | None:
        try:
            provider_id = _provider(provider)
            path = self._path(credential_ref)
            if not path.exists():
                return None
            with self._lock(credential_ref):
                try:
                    passphrase = _passphrase_bytes(self._reader)
                except HeadlessCredentialError as exc:
                    raise OpaqueUnlockError("credential_unlock_failed") from exc
                secret, _generation = self._unlock(
                    path, provider_id, credential_ref, passphrase,
                )
            try:
                return secret.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise OpaqueUnlockError("credential_unlock_failed") from exc
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_store_failed") from exc

    def contains(self, provider: str, credential_ref: str) -> bool:
        # Presence is intentionally coarse and never decrypts or implies validity.
        try:
            _provider(provider)
            return self._path(credential_ref).is_file()
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_store_failed") from exc

    def revoke(self, provider: str, credential_ref: str) -> bool:
        try:
            _provider(provider)
            path = self._path(credential_ref)
            with self._lock(credential_ref):
                if not path.exists():
                    return False
                try:
                    passphrase = _passphrase_bytes(self._reader)
                except HeadlessCredentialError as exc:
                    raise OpaqueUnlockError("credential_unlock_failed") from exc
                self._unlock(path, _provider(provider), credential_ref, passphrase)
                path.unlink()
                _fsync_directory(self.root)
                return True
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_revoke_failed") from exc

    def generation(self, provider: str, credential_ref: str) -> int | None:
        try:
            provider_id = _provider(provider)
            path = self._path(credential_ref)
            if not path.exists():
                return None
            with self._lock(credential_ref):
                try:
                    passphrase = _passphrase_bytes(self._reader)
                except HeadlessCredentialError as exc:
                    raise OpaqueUnlockError("credential_unlock_failed") from exc
                _secret, generation = self._unlock(
                    path, provider_id, credential_ref, passphrase,
                )
            return generation
        except HeadlessCredentialError:
            raise
        except OSError as exc:
            raise HeadlessCredentialError("credential_store_failed") from exc

    def _unlock(self, path: Path, provider: str, credential_ref: str,
                passphrase: bytes) -> tuple[bytes, int]:
        try:
            if path.is_symlink() or not path.is_file() or not signing_file_permissions_are_restricted(path):
                raise ValueError
            payload = path.read_bytes()
            record = _parse(payload)
            metadata = record["metadata"]
            if metadata != _metadata(provider, credential_ref, int(metadata["generation"])):
                raise ValueError
            aead, kdf = record["aead"], record["kdf"]
            nonce = _decode_b64(aead["nonce_b64"], 24)
            salt = _decode_b64(kdf["salt_b64"], 16)
            ciphertext = _decode_b64(record["ciphertext_b64"], (17, 16_400))
            secret = crypto_aead_xchacha20poly1305_ietf_decrypt(
                ciphertext, _aad(record), nonce, _derive(passphrase, salt),
            )
            if not 1 <= len(secret) <= _MAX_SECRET or b"\x00" in secret:
                raise ValueError
            return secret, int(metadata["generation"])
        except Exception as exc:
            if isinstance(exc, HeadlessCredentialError):
                raise
            raise OpaqueUnlockError("credential_unlock_failed") from exc

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary = self.root / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY, 0o600,
            )
            _restrict(temporary)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
            self._observer("temporary_fsynced")
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            _restrict(path)
            self._observer("replaced")
            _fsync_directory(self.root)
            self._observer("directory_fsynced")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class ConfiguredHeadlessVault:
    """Resolve only configured provider/reference bindings from a headless store."""

    def __init__(self, store: HeadlessEncryptedFileStore, references: Mapping[str, str]) -> None:
        self._store = store
        self._references = {_provider(provider): _reference(ref) for provider, ref in references.items()}

    def get(self, provider: str) -> str | None:
        provider_id = _provider(provider)
        reference = self._references.get(provider_id)
        return None if reference is None else self._store.resolve(provider_id, reference)

    def base_url(self, provider: str) -> None:
        """Headless stores contain secrets only; provider regions use defaults."""
        _provider(provider)
        return None

    def base_url(self, provider: str) -> None:
        """Encrypted stores contain secrets only, never routing metadata."""
        _provider(provider)
        return None

    def __repr__(self) -> str:
        return "ConfiguredHeadlessVault()"


__all__ = [
    "ConfiguredHeadlessVault", "HeadlessCredentialError", "HeadlessEncryptedFileStore",
    "OpaqueUnlockError", "attended_passphrase",
]
