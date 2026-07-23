import hashlib
import inspect
import json
from dataclasses import replace

from torq_cli.domain import drift_oracle
from torq_cli.domain.drift_oracle import load_packaged_oracle, validate_oracle


EXPECTED_MANIFEST = (
    "schema: torq-oracle-manifest-v1\n"
    "source_commit: 3ae196102a84aed24f7daa9dc3fed037522e1f20\n"
    "fixtures:\n"
    "  prompt_provenance.normalized.json:\n"
    "    sha256: 798c9ba82859da69eb6d38c5b16a47dd9891be48f4310136421c36e6b8e6382b\n"
    "  role_map.normalized.json:\n"
    "    sha256: 7348b7c15bfece61ecacdeb71a860ad4221425044d39e705cf0ec7b28b4d2cf0\n"
    "  v5_config.normalized.json:\n"
    "    sha256: 52110d4522b883ee16b313a612441871e802710e75bb754bb5d9707492f27698\n"
)


def test_packaged_oracle_has_exact_manifest_and_fixture_hashes() -> None:
    oracle = load_packaged_oracle()

    assert oracle.manifest_bytes == EXPECTED_MANIFEST.encode("ascii")
    assert len(oracle.manifest_bytes) == 423
    assert hashlib.sha256(oracle.manifest_bytes).hexdigest() == (
        "f4255aadddfbe1d3de47d51145c2ea4e8e610dcc1284d017a9abd80d22f68e4b"
    )
    assert oracle.fixture_hashes == {
        "prompt_provenance.normalized.json": "798c9ba82859da69eb6d38c5b16a47dd9891be48f4310136421c36e6b8e6382b",
        "role_map.normalized.json": "7348b7c15bfece61ecacdeb71a860ad4221425044d39e705cf0ec7b28b4d2cf0",
        "v5_config.normalized.json": "52110d4522b883ee16b313a612441871e802710e75bb754bb5d9707492f27698",
    }
    assert oracle.prompt_provenance[3] == {
        "present": False,
        "source_blob_oid": None,
        "source_path": ".torq/v5/prompts/g2a_adversarial.md",
    }
    assert validate_oracle(oracle) == ()


def test_retired_manifest_digest_is_not_trusted() -> None:
    oracle = load_packaged_oracle()
    assert oracle.trusted_manifest_sha256 != (
        "c88694f6da38f26373e3cae84f7ee47d8ff943be2c1e81c022be4c53abe6af9e"
    )


def test_oracle_has_no_upstream_worktree_read() -> None:
    source = inspect.getsource(drift_oracle)
    assert "TORQ-CONSOLE" not in source
    assert "subprocess" not in source


def test_trusted_manifest_hash_is_checked_before_fixture_processing() -> None:
    oracle = load_packaged_oracle()
    invalid = replace(oracle, manifest_bytes=b"tampered", fixture_bytes={})

    assert validate_oracle(invalid) == ("oracle_manifest_trusted_hash_mismatch",)


def test_oracle_missing_fixture_is_stable() -> None:
    oracle = load_packaged_oracle()
    missing = dict(oracle.fixture_bytes)
    del missing["role_map.normalized.json"]
    invalid = replace(oracle, fixture_bytes=missing)

    assert "oracle_fixture_missing" in validate_oracle(invalid)


def test_wrong_hash_fixture_skips_all_json_parsing(monkeypatch) -> None:
    oracle = load_packaged_oracle()
    fixture_bytes = dict(oracle.fixture_bytes)
    fixture_bytes["prompt_provenance.normalized.json"] = b" " + fixture_bytes["prompt_provenance.normalized.json"]
    invalid = replace(oracle, fixture_bytes=fixture_bytes)
    calls: list[object] = []
    original_loads = drift_oracle.json.loads

    def spy_loads(value, *args, **kwargs):
        calls.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(drift_oracle.json, "loads", spy_loads)

    assert validate_oracle(invalid) == ("oracle_fixture_hash_mismatch",)
    assert calls == []


def test_valid_hash_fixtures_are_parsed_after_integrity_pass(monkeypatch) -> None:
    oracle = load_packaged_oracle()
    calls: list[object] = []
    original_loads = drift_oracle.json.loads

    def spy_loads(value, *args, **kwargs):
        calls.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(drift_oracle.json, "loads", spy_loads)

    assert validate_oracle(oracle) == ()
    assert len(calls) >= 3


def test_narrow_v5_accessor_hashes_before_json_parse(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(drift_oracle.json, "loads", lambda value, *args, **kwargs: calls.append("json") or {})
    monkeypatch.setattr(drift_oracle, "_read_resource", lambda name: calls.append(name) or b"tampered")

    result = drift_oracle.load_v5_config_reference()

    assert result[0] == "oracle_manifest_trusted_hash_mismatch"
    assert calls == ["manifest.yaml"]


def test_narrow_v5_accessor_never_reads_other_fixtures_or_creates_drift(monkeypatch) -> None:
    oracle = load_packaged_oracle()
    calls: list[str] = []
    original = drift_oracle._read_resource

    def read(name: str) -> bytes:
        calls.append(name)
        return original(name)

    monkeypatch.setattr(drift_oracle, "_read_resource", read)
    finding, value = drift_oracle.load_v5_config_reference()

    assert finding is None
    assert value == json.loads(oracle.fixture_bytes["v5_config.normalized.json"])
    assert calls == ["manifest.yaml", "v5_config.normalized.json"]
