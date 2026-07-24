"""Hash-chained, redacted receipts and signed terminal manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
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
                return self._read_regular_key(self.private_key_path)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("receipt_signing_key_short_write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self.private_key_path, 0o600)
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
