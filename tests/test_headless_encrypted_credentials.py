from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from test_phase5_cli_experience import _answers
from torq_cli.application.setup import SetupService
from torq_cli.connectors import headless_credentials as vault_module
from torq_cli.connectors.headless_credentials import (
    HeadlessCredentialError,
    HeadlessEncryptedFileStore,
    OpaqueUnlockError,
    attended_passphrase,
)
from torq_cli.domain.config_schema import validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.safety.receipts import signing_file_permissions_are_restricted


REF = "credref_0123456789abcdef0123456789abcdef"
OTHER_REF = "credref_11111111111111111111111111111111"
PASSPHRASE = "test-only-passphrase"


def _reader(value: str = PASSPHRASE):
    return lambda: value


def _store(tmp_path: Path, **kwargs: object) -> HeadlessEncryptedFileStore:
    return HeadlessEncryptedFileStore(
        (tmp_path / "vault").resolve(), passphrase_reader=_reader(), **kwargs
    )


def _path(store: HeadlessEncryptedFileStore) -> Path:
    return store.root / f"{REF}.tqcv"


def test_create_resolve_and_canonical_envelope_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "test-secret")

    path = _path(store)
    payload = path.read_bytes()
    record = json.loads(payload)
    assert payload == json.dumps(
        record, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    assert list(record) == ["aead", "ciphertext_b64", "format", "kdf", "metadata", "version"]
    assert record["aead"]["algorithm"] == "xchacha20poly1305-ietf"
    assert record["kdf"] == {
        "algorithm": "argon2id",
        "memory_kib": 65536,
        "parallelism": 1,
        "salt_b64": record["kdf"]["salt_b64"],
        "time_cost": 3,
        "version": 19,
    }
    assert record["metadata"] == {
        "backend": "headless_encrypted_file",
        "credential_ref": REF,
        "generation": 1,
        "provider_id": "deepseek",
    }
    assert store.resolve("deepseek", REF) == "test-secret"
    assert "test-secret" not in payload.decode("ascii")
    assert signing_file_permissions_are_restricted(path)
    assert signing_file_permissions_are_restricted(store.root)


def test_wrong_passphrase_tamper_and_metadata_mismatch_are_opaque(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "test-secret")
    path = _path(store)

    wrong = HeadlessEncryptedFileStore(store.root, passphrase_reader=_reader("wrong"))
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        wrong.resolve("deepseek", REF)

    original = path.read_bytes()
    record = json.loads(original)
    ciphertext = record["ciphertext_b64"]
    record["ciphertext_b64"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    path.write_bytes(vault_module._canonical(record))
    vault_module._restrict(path)
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.resolve("deepseek", REF)

    path.write_bytes(original)
    vault_module._restrict(path)
    record = json.loads(original)
    record["metadata"]["provider_id"] = "qwen"
    path.write_bytes(vault_module._canonical(record))
    vault_module._restrict(path)
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.resolve("deepseek", REF)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}\n",
        b'\xef\xbb\xbf{}',
        b'{"a":1,"a":1}',
        b'{"a":1.0}',
        b'{"a":true}',
        b'{"a":null}',
        b'{"unknown":1}',
        b"[1]",
        b'{"version":1 }',
    ],
)
def test_malformed_noncanonical_records_fail_with_one_outcome(
    tmp_path: Path, payload: bytes
) -> None:
    store = _store(tmp_path)
    store.root.mkdir()
    vault_module._restrict(store.root, directory=True)
    path = _path(store)
    path.write_bytes(payload)
    vault_module._restrict(path)
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.resolve("deepseek", REF)


def test_rotation_increments_generation_and_revocation_is_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.contains("deepseek", REF) is False
    store.store("deepseek", REF, "first")
    first = _path(store).read_bytes()
    store.rotate("deepseek", REF, "second")
    second = _path(store).read_bytes()
    assert second != first
    assert store.generation("deepseek", REF) == 2
    assert store.resolve("deepseek", REF) == "second"
    assert store.revoke("deepseek", REF) is True
    assert not _path(store).exists()
    assert store.revoke("deepseek", REF) is False
    with pytest.raises(HeadlessCredentialError, match="credential_absent"):
        store.rotate("deepseek", OTHER_REF, "value")


def test_rotation_rechecks_existence_inside_its_exclusive_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "first")
    outcome: list[str] = []

    def rotate() -> None:
        try:
            store.rotate("deepseek", REF, "replacement")
        except HeadlessCredentialError as exc:
            outcome.append(str(exc))

    with store._lock(REF):
        worker = threading.Thread(target=rotate)
        worker.start()
        _path(store).unlink()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome == ["credential_absent"]
    assert not _path(store).exists()


def test_interrupted_rotation_preserves_prior_authoritative_record(tmp_path: Path) -> None:
    initial = _store(tmp_path)
    initial.store("deepseek", REF, "prior")
    prior = _path(initial).read_bytes()

    def interrupt(step: str) -> None:
        if step == "temporary_fsynced":
            raise RuntimeError("simulated interruption")

    interrupted = _store(tmp_path, commit_observer=interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        interrupted.rotate("deepseek", REF, "replacement")

    assert _path(initial).read_bytes() == prior
    assert initial.resolve("deepseek", REF) == "prior"
    assert not list(initial.root.glob("*.tmp"))


def test_existing_lock_times_out_without_reading_passphrase(tmp_path: Path) -> None:
    calls = 0

    def reader() -> str:
        nonlocal calls
        calls += 1
        return PASSPHRASE

    root = (tmp_path / "vault").resolve()
    root.mkdir()
    vault_module._restrict(root, directory=True)
    lock = root / f".{REF}.lock"
    lock.write_bytes(b"")
    vault_module._restrict(lock)
    store = HeadlessEncryptedFileStore(root, passphrase_reader=reader, lock_seconds=0.01)
    with pytest.raises(HeadlessCredentialError, match="credential_store_locked"):
        store.store("deepseek", REF, "secret")
    assert calls == 0


def test_bounds_and_invalid_inputs_write_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for secret in ("", "x" * 16_385, "has\x00nul"):
        with pytest.raises(HeadlessCredentialError, match="credential_value_invalid"):
            store.store("deepseek", REF, secret)
    with pytest.raises(HeadlessCredentialError, match="credential_ref_invalid"):
        store.store("deepseek", "../escape", "secret")
    with pytest.raises(HeadlessCredentialError, match="credential_provider_unsupported"):
        store.store("other", REF, "secret")
    assert not _path(store).exists()


def test_passphrase_normalization_and_bounds(tmp_path: Path) -> None:
    root = (tmp_path / "vault").resolve()
    composed = HeadlessEncryptedFileStore(root, passphrase_reader=_reader("café"))
    composed.store("deepseek", REF, "secret")
    decomposed = HeadlessEncryptedFileStore(root, passphrase_reader=_reader("café"))
    assert decomposed.resolve("deepseek", REF) == "secret"
    for index, invalid in enumerate(("", "x" * 1025, "bad\x00value")):
        rejected = HeadlessEncryptedFileStore(
            (tmp_path / f"invalid-{index}" / "vault").resolve(),
            passphrase_reader=_reader(invalid),
        )
        with pytest.raises(HeadlessCredentialError, match="attended_unlock_failed"):
            rejected.store("deepseek", REF, "secret")


def test_attended_reader_rejects_redirected_input_and_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault_module.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(vault_module.sys.stderr, "isatty", lambda: True)
    with pytest.raises(HeadlessCredentialError, match="attended_unlock_required"):
        attended_passphrase()
    monkeypatch.setattr(vault_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(HeadlessCredentialError, match="attended_unlock_required"):
        attended_passphrase()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_posix_permissions_are_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "secret")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(_path(store).stat().st_mode) == 0o600


def test_config_schema_requires_explicit_absolute_headless_path() -> None:
    base = {
        "config_version": 1,
        "profile": {"id": "torq-v5-6-live", "version": "1.0.0"},
        "binding_overrides": {},
        "connectors": {},
        "policy": {
            "independence_mode": "profile_minimum", "unattestable_action": "deny",
            "loop_budget": 1, "resource_limits": {"max_runtime_seconds": 60,
            "max_cost_cents": 100, "max_file_count": 10, "max_changed_lines": 100},
        },
    }
    # This assertion only checks the new source field; unrelated minimal-config
    # findings are deliberately ignored.
    valid = {**base, "credential_source": {"kind": "headless_encrypted_file", "path": "/v"}}
    assert not [
        f for f in validate_config(valid, load_registry())
        if f.path.startswith("/credential_source")
    ]
    invalid = {**base, "credential_source": {"kind": "headless_encrypted_file", "path": "v"}}
    assert [
        f for f in validate_config(invalid, load_registry())
        if f.path == "/credential_source/path"
    ]


def test_setup_persists_explicit_headless_backend_without_secret(tmp_path: Path) -> None:
    answers = _answers()
    answers["credential_refs"] = {
        "deepseek": REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
    }
    answers["credential_backend"] = "headless_encrypted_file"
    answers["credential_store_root"] = str((tmp_path / "vault").resolve())
    target = tmp_path / "config.yaml"
    document = SetupService().configure(target, answers)
    assert document["credential_source"] == {
        "kind": "headless_encrypted_file", "path": str((tmp_path / "vault").resolve()),
    }
    rendered = target.read_text(encoding="utf-8")
    assert PASSPHRASE not in rendered
    assert "headless_encrypted_file" in rendered
