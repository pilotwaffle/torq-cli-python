from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from test_phase5_cli_experience import _answers
from torq_cli.application.setup import SetupError, SetupService
from torq_cli.connectors import headless_credentials as vault_module
from torq_cli.connectors.headless_credentials import (
    HeadlessCredentialError,
    HeadlessEncryptedFileStore,
    OpaqueUnlockError,
    attended_passphrase,
)
from torq_cli.domain.config_schema import validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces import cli as cli_module
from torq_cli.interfaces.cli import main
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


def _structural_record() -> dict[str, Any]:
    return {
        "aead": {
            "algorithm": "xchacha20poly1305-ietf",
            "nonce_b64": vault_module._b64(b"n" * 24),
        },
        "ciphertext_b64": vault_module._b64(b"c" * 17),
        "format": "torq-credential-vault",
        "kdf": {
            "algorithm": "argon2id",
            "memory_kib": 65_536,
            "parallelism": 1,
            "salt_b64": vault_module._b64(b"s" * 16),
            "time_cost": 3,
            "version": 19,
        },
        "metadata": {
            "backend": "headless_encrypted_file",
            "credential_ref": REF,
            "generation": 1,
            "provider_id": "deepseek",
        },
        "version": 1,
    }


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


def test_boolean_version_is_not_accepted_as_integer_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "test-secret")
    path = _path(store)
    record = json.loads(path.read_bytes())
    record["version"] = True
    path.write_bytes(vault_module._canonical(record))
    vault_module._restrict(path)

    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.resolve("deepseek", REF)


def test_parser_pins_raw_and_ciphertext_size_boundaries() -> None:
    with pytest.raises(ValueError):
        vault_module._parse(b"x" * 98_305)

    for size in (17, 16_400):
        record = _structural_record()
        record["ciphertext_b64"] = vault_module._b64(b"c" * size)
        assert vault_module._parse(vault_module._canonical(record))["version"] == 1

    for size in (16, 16_401):
        record = _structural_record()
        record["ciphertext_b64"] = vault_module._b64(b"c" * size)
        with pytest.raises(ValueError):
            vault_module._parse(vault_module._canonical(record))


def test_parser_rejects_noncanonical_base64_classes() -> None:
    whitespace = _structural_record()
    whitespace["aead"]["nonce_b64"] += "\n"

    urlsafe = _structural_record()
    urlsafe["aead"]["nonce_b64"] = vault_module._b64(b"\xfb" * 24).replace(
        "+", "-"
    )

    unpadded = _structural_record()
    unpadded["kdf"]["salt_b64"] = str(unpadded["kdf"]["salt_b64"]).rstrip("=")

    extra_padding = _structural_record()
    extra_padding["aead"]["nonce_b64"] += "="

    for record in (whitespace, urlsafe, unpadded, extra_padding):
        with pytest.raises(ValueError):
            vault_module._parse(vault_module._canonical(record))


def test_parser_rejects_generation_and_nested_schema_mutations() -> None:
    maximum = _structural_record()
    maximum["metadata"]["generation"] = 9_007_199_254_740_991
    assert vault_module._parse(vault_module._canonical(maximum))["metadata"] == maximum["metadata"]

    cases: list[dict[str, object]] = []
    for generation in (0, 9_007_199_254_740_992, True):
        record = _structural_record()
        record["metadata"]["generation"] = generation
        cases.append(record)
    for key, value in (
        ("backend", "platform_keychain"),
        ("credential_ref", "credref_invalid"),
        ("provider_id", "unknown"),
    ):
        record = _structural_record()
        record["metadata"][key] = value
        cases.append(record)
    for container, key, value in (
        ("aead", "algorithm", "xchacha20poly1305"),
        ("aead", "unknown", "x"),
        ("kdf", "algorithm", "argon2i"),
        ("kdf", "memory_kib", True),
        ("kdf", "parallelism", True),
        ("kdf", "time_cost", True),
        ("kdf", "version", True),
        ("kdf", "unknown", 1),
        ("metadata", "unknown", "x"),
    ):
        record = _structural_record()
        record[container][key] = value
        cases.append(record)

    for record in cases:
        with pytest.raises(ValueError):
            vault_module._parse(vault_module._canonical(record))

    valid = vault_module._canonical(_structural_record())
    nested_duplicate = valid.replace(
        b'"algorithm":"xchacha20poly1305-ietf",',
        b'"algorithm":"xchacha20poly1305-ietf","algorithm":"xchacha20poly1305-ietf",',
        1,
    )
    with pytest.raises(ValueError):
        vault_module._parse(nested_duplicate)


@pytest.mark.parametrize(
    ("container", "field", "sizes"),
    [
        ("aead", "nonce_b64", (23, 25)),
        ("kdf", "salt_b64", (15, 17)),
    ],
)
def test_parser_rejects_nonce_and_salt_size_bounds(
    container: str, field: str, sizes: tuple[int, int]
) -> None:
    for size in sizes:
        record = _structural_record()
        record[container][field] = vault_module._b64(b"x" * size)
        with pytest.raises(ValueError):
            vault_module._parse(vault_module._canonical(record))


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


def test_revocation_authenticates_provider_passphrase_and_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store("deepseek", REF, "secret")
    path = _path(store)
    original = path.read_bytes()

    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.revoke("qwen", REF)
    assert path.read_bytes() == original

    wrong = HeadlessEncryptedFileStore(store.root, passphrase_reader=_reader("wrong"))
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        wrong.revoke("deepseek", REF)
    assert path.read_bytes() == original

    record = json.loads(original)
    record["metadata"]["provider_id"] = "qwen"
    path.write_bytes(vault_module._canonical(record))
    vault_module._restrict(path)
    tampered = path.read_bytes()
    with pytest.raises(OpaqueUnlockError, match="^credential_unlock_failed$"):
        store.revoke("deepseek", REF)
    assert path.read_bytes() == tampered


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


def test_filesystem_failures_are_secret_free_backend_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_file = tmp_path / "sensitive-root-name"
    root_file.write_text("not a directory", encoding="utf-8")
    invalid = HeadlessEncryptedFileStore(root_file.resolve(), passphrase_reader=_reader())
    with pytest.raises(HeadlessCredentialError) as caught:
        invalid.store("deepseek", REF, "secret")
    assert str(caught.value) == "credential_store_failed"
    assert str(root_file) not in str(caught.value)

    store = _store(tmp_path)
    store.store("deepseek", REF, "prior")
    prior = _path(store).read_bytes()

    def deny_replace(_source: object, _target: object) -> None:
        raise PermissionError(f"denied: {tmp_path / 'private-location'}")

    monkeypatch.setattr(vault_module.os, "replace", deny_replace)
    with pytest.raises(HeadlessCredentialError) as replacement:
        store.rotate("deepseek", REF, "replacement")
    assert str(replacement.value) == "credential_store_failed"
    assert str(tmp_path) not in str(replacement.value)
    assert _path(store).read_bytes() == prior
    assert not list(store.root.glob("*.tmp"))


def test_cli_rejects_relative_store_root_before_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main([
        "auth", "verify-access", "--provider", "deepseek", "--credential-ref", REF,
        "--backend", "headless_encrypted_file", "--store-root", "relative-vault",
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 3
    assert report == {
        "finding": "credential_store_absolute_required", "status": "blocked",
    }
    assert not (tmp_path / "relative-vault").exists()


def test_cli_filesystem_failure_discloses_neither_traceback_nor_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root_file = tmp_path / "private-vault-location"
    root_file.write_text("not a directory", encoding="utf-8")
    real_store = HeadlessEncryptedFileStore
    monkeypatch.setattr(
        cli_module,
        "HeadlessEncryptedFileStore",
        lambda root: real_store(root, passphrase_reader=_reader()),
    )
    monkeypatch.setattr(cli_module, "_read_attended_secret", lambda: "secret")

    code = main([
        "auth", "store", "--provider", "deepseek", "--credential-ref", REF,
        "--backend", "headless_encrypted_file", "--store-root", str(root_file.resolve()),
    ])
    output = capsys.readouterr()
    assert code == 3
    assert json.loads(output.out) == {"finding": "credential_store_failed", "status": "blocked"}
    assert str(root_file) not in output.out
    assert output.err == ""


def test_cli_headless_verify_access_reports_presence_not_validity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = (tmp_path / "vault").resolve()
    store = HeadlessEncryptedFileStore(root, passphrase_reader=_reader())
    store.store("deepseek", REF, "secret")

    code = main([
        "auth", "verify-access", "--provider", "deepseek", "--credential-ref", REF,
        "--backend", "headless_encrypted_file", "--store-root", str(root),
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report == {"backend": "headless_encrypted_file", "status": "present"}


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


def test_maximum_plaintext_and_passphrase_bounds_are_accepted(tmp_path: Path) -> None:
    store = HeadlessEncryptedFileStore(
        (tmp_path / "vault").resolve(), passphrase_reader=_reader("p" * 1024),
    )
    secret = "s" * 16_384
    store.store("deepseek", REF, secret)
    assert store.resolve("deepseek", REF) == secret


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
    nul_path = {
        **base,
        "credential_source": {"kind": "headless_encrypted_file", "path": "/v\x00ault"},
    }
    assert [
        f for f in validate_config(nul_path, load_registry())
        if f.path == "/credential_source/path"
    ]


def test_store_root_nul_is_rejected_before_filesystem_access(tmp_path: Path) -> None:
    root = str(tmp_path.resolve()) + "\x00vault"
    with pytest.raises(HeadlessCredentialError, match="^credential_store_path_invalid$"):
        HeadlessEncryptedFileStore(Path(root), passphrase_reader=_reader())

    answers = _answers()
    answers["credential_refs"] = {
        "codex": "credref_33333333333333333333333333333333",
        "deepseek": REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
    }
    answers["credential_backend"] = "headless_encrypted_file"
    answers["credential_store_root"] = root
    with pytest.raises(SetupError, match="^credential_store_root_invalid$"):
        SetupService().configure(tmp_path / "config.yaml", answers)


def test_setup_persists_explicit_headless_backend_without_secret(tmp_path: Path) -> None:
    answers = _answers()
    answers["credential_refs"] = {
        "codex": "credref_33333333333333333333333333333333",
        "deepseek": REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
    }
    answers["credential_backend"] = "headless_encrypted_file"
    answers["credential_store_root"] = str((tmp_path / "vault").resolve())
    target = tmp_path / "config.yaml"
    document = SetupService().configure(target, answers)
    assert document["connectors"]["g2a-main"]["credential_ref"] == (
        "credref_33333333333333333333333333333333"
    )
    assert document["credential_source"] == {
        "kind": "headless_encrypted_file", "path": str((tmp_path / "vault").resolve()),
    }
    rendered = target.read_text(encoding="utf-8")
    assert PASSPHRASE not in rendered
    assert "headless_encrypted_file" in rendered


def test_setup_headless_backend_requires_codex_ref_before_store_creation(
    tmp_path: Path,
) -> None:
    answers = _answers()
    answers["credential_refs"] = {
        "deepseek": REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
    }
    answers["credential_backend"] = "headless_encrypted_file"
    store_root = tmp_path / "vault"
    answers["credential_store_root"] = str(store_root.resolve())
    target = tmp_path / "config.yaml"

    with pytest.raises(SetupError, match="^provider_credential_ref_missing:codex$"):
        SetupService().configure(target, answers)
    assert not target.exists()
    assert not store_root.exists()
