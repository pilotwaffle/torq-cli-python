"""Fixture-only compatibility oracle; it never reads upstream or Git objects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Mapping

import yaml

from .models import deep_freeze


ORACLE_ROOT = ("data", "oracles", "torq-console-3ae19610")
TRUSTED_MANIFEST_SHA256 = "f4255aadddfbe1d3de47d51145c2ea4e8e610dcc1284d017a9abd80d22f68e4b"
FIXTURE_NAMES = (
    "prompt_provenance.normalized.json",
    "role_map.normalized.json",
    "v5_config.normalized.json",
)


@dataclass(frozen=True)
class PackagedOracle:
    manifest_bytes: bytes
    fixture_bytes: Mapping[str, bytes]
    fixture_hashes: Mapping[str, str]
    prompt_provenance: tuple[Mapping[str, Any], ...]
    trusted_manifest_sha256: str = TRUSTED_MANIFEST_SHA256

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_bytes", deep_freeze(self.fixture_bytes))
        object.__setattr__(self, "fixture_hashes", deep_freeze(self.fixture_hashes))
        object.__setattr__(self, "prompt_provenance", tuple(deep_freeze(item) for item in self.prompt_provenance))


def _resource(name: str) -> Traversable:
    path = resources.files("torq_cli")
    for part in (*ORACLE_ROOT, name):
        path = path.joinpath(part)
    return path


def _read_resource(name: str) -> bytes:
    return _resource(name).read_bytes()


def _empty_oracle(manifest_bytes: bytes) -> PackagedOracle:
    return PackagedOracle(manifest_bytes, {}, {}, ())


def load_packaged_oracle() -> PackagedOracle:
    """Verify the trusted raw manifest before reading any fixture bytes."""
    try:
        manifest_bytes = _read_resource("manifest.yaml")
    except FileNotFoundError:
        return _empty_oracle(b"")
    except OSError:
        return _empty_oracle(b"")
    if hashlib.sha256(manifest_bytes).hexdigest() != TRUSTED_MANIFEST_SHA256:
        return _empty_oracle(manifest_bytes)
    try:
        manifest = yaml.safe_load(manifest_bytes.decode("ascii"))
        if not isinstance(manifest, Mapping) or not isinstance(manifest.get("fixtures"), Mapping):
            return _empty_oracle(manifest_bytes)
        fixture_hashes = {name: str(manifest["fixtures"][name]["sha256"]) for name in FIXTURE_NAMES}
    except (UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError):
        return _empty_oracle(manifest_bytes)
    fixture_bytes: dict[str, bytes] = {}
    for name in FIXTURE_NAMES:
        try:
            fixture_bytes[name] = _read_resource(name)
        except (FileNotFoundError, OSError):
            continue
    if (
        len(fixture_bytes) != len(FIXTURE_NAMES)
        or any(hashlib.sha256(fixture_bytes[name]).hexdigest() != fixture_hashes.get(name) for name in FIXTURE_NAMES)
    ):
        return PackagedOracle(manifest_bytes, fixture_bytes, fixture_hashes, ())
    prompt_provenance: tuple[Mapping[str, Any], ...] = ()
    raw_provenance = fixture_bytes.get("prompt_provenance.normalized.json")
    if raw_provenance is not None:
        try:
            parsed = json.loads(raw_provenance.decode("utf-8"))
            if isinstance(parsed, Mapping) and isinstance(parsed.get("entries"), list):
                prompt_provenance = tuple(parsed["entries"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return PackagedOracle(manifest_bytes, fixture_bytes, fixture_hashes, prompt_provenance)


def load_v5_config_reference() -> tuple[str | None, Mapping[str, Any] | None]:
    """Authenticate only the manifest and normalized V5 fixture before parsing either."""
    try:
        manifest_bytes = _read_resource("manifest.yaml")
    except (FileNotFoundError, OSError):
        return "oracle_fixture_missing", None
    if hashlib.sha256(manifest_bytes).hexdigest() != TRUSTED_MANIFEST_SHA256:
        return "oracle_manifest_trusted_hash_mismatch", None
    try:
        manifest = yaml.safe_load(manifest_bytes.decode("ascii"))
        if (
            not isinstance(manifest, Mapping)
            or set(manifest) != {"schema", "source_commit", "fixtures"}
            or manifest["schema"] != "torq-oracle-manifest-v1"
            or not isinstance(manifest["fixtures"], Mapping)
        ):
            return "oracle_fixture_schema_invalid", None
        entry = manifest["fixtures"].get("v5_config.normalized.json")
        if not isinstance(entry, Mapping) or set(entry) != {"sha256"} or not isinstance(entry["sha256"], str):
            return "oracle_fixture_schema_invalid", None
        expected_hash = entry["sha256"]
    except (UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError):
        return "oracle_fixture_schema_invalid", None
    try:
        fixture_bytes = _read_resource("v5_config.normalized.json")
    except (FileNotFoundError, OSError):
        return "oracle_fixture_missing", None
    if hashlib.sha256(fixture_bytes).hexdigest() != expected_hash:
        return "oracle_fixture_hash_mismatch", None
    try:
        parsed = json.loads(fixture_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "oracle_fixture_schema_invalid", None
    if not _schema_valid("v5_config.normalized.json", parsed):
        return "oracle_fixture_schema_invalid", None
    return None, parsed


def _schema_valid(name: str, value: Any) -> bool:
    if name == "role_map.normalized.json":
        return isinstance(value, dict) and set(value) == {"roles", "schema"} and value["schema"] == "torq-oracle-role-map-v1"
    if name == "v5_config.normalized.json":
        return isinstance(value, dict) and set(value) == {"agents", "schema"} and value["schema"] == "torq-oracle-v5-config-v1"
    if name == "prompt_provenance.normalized.json":
        return isinstance(value, dict) and set(value) == {"entries", "schema"} and value["schema"] == "torq-oracle-prompt-provenance-v1"
    return False


def _parsed_fixture(oracle: PackagedOracle, name: str) -> Any:
    raw = oracle.fixture_bytes[name]
    return json.loads(raw.decode("utf-8"))


def validate_oracle(oracle: PackagedOracle) -> tuple[str, ...]:
    """Validate trusted raw bytes first, then closed manifest and fixture data."""
    if hashlib.sha256(oracle.manifest_bytes).hexdigest() != oracle.trusted_manifest_sha256:
        return ("oracle_manifest_trusted_hash_mismatch",)
    findings: list[str] = []
    try:
        manifest = yaml.safe_load(oracle.manifest_bytes.decode("ascii"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return ("oracle_fixture_schema_invalid",)
    if not isinstance(manifest, Mapping) or set(manifest) != {"schema", "source_commit", "fixtures"} or manifest["schema"] != "torq-oracle-manifest-v1":
        return ("oracle_fixture_schema_invalid",)
    fixture_manifest = manifest.get("fixtures", {}) if isinstance(manifest, Mapping) else {}
    if not isinstance(fixture_manifest, Mapping) or set(fixture_manifest) != set(FIXTURE_NAMES):
        return ("oracle_fixture_schema_invalid",)
    for name in FIXTURE_NAMES:
        raw = oracle.fixture_bytes.get(name)
        if raw is None:
            findings.append("oracle_fixture_missing")
            continue
        expected_hash = fixture_manifest.get(name, {}).get("sha256") if isinstance(fixture_manifest.get(name), Mapping) else None
        if hashlib.sha256(raw).hexdigest() != expected_hash or oracle.fixture_hashes.get(name) != expected_hash:
            findings.append("oracle_fixture_hash_mismatch")
    if findings:
        return tuple(dict.fromkeys(findings))
    parsed_fixtures: dict[str, Any] = {}
    for name in FIXTURE_NAMES:
        raw = oracle.fixture_bytes[name]
        try:
            parsed_fixtures[name] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            findings.append("oracle_fixture_schema_invalid")
            continue
        if not _schema_valid(name, parsed_fixtures[name]):
            findings.append("oracle_fixture_schema_invalid")
    if not findings:
        try:
            role_map = parsed_fixtures["role_map.normalized.json"]
            v5_config = parsed_fixtures["v5_config.normalized.json"]
            provenance = parsed_fixtures["prompt_provenance.normalized.json"]
            roles = {item["role_id"]: item for item in role_map["roles"]}
            agents = {item["role_id"]: item for item in v5_config["agents"]}
            if roles.get("g1d", {}).get("model") != "claude-opus-4-8":
                findings.append("oracle_model_mismatch_g1d")
            if roles.get("g2a", {}).get("model") != "claude-opus-4-8":
                findings.append("oracle_model_mismatch_g2a")
            if agents.get("g2a", {}).get("prompt") != "prompts/gate2_audit.md":
                findings.append("oracle_prompt_mismatch_g2a")
            if not any(item.get("source_path") == ".torq/v5/prompts/g2a_adversarial.md" and item.get("present") is False for item in provenance["entries"]):
                findings.append("oracle_prompt_path_missing_g2a_adversarial")
        except (KeyError, TypeError):
            findings.append("oracle_fixture_schema_invalid")
    return tuple(dict.fromkeys(findings))


def validate_oracle_bytes(manifest_bytes: bytes, fixture_bytes: Mapping[str, bytes]) -> tuple[str, ...]:
    """Validate temporary copies without changing packaged resources."""
    if hashlib.sha256(manifest_bytes).hexdigest() != TRUSTED_MANIFEST_SHA256:
        return ("oracle_manifest_trusted_hash_mismatch",)
    try:
        parsed = yaml.safe_load(manifest_bytes.decode("ascii"))
        hashes = {name: str(parsed["fixtures"][name]["sha256"]) for name in FIXTURE_NAMES}
    except (UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError):
        return ("oracle_fixture_schema_invalid",)
    return validate_oracle(PackagedOracle(manifest_bytes, fixture_bytes, hashes, ()))
