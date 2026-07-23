import json
import pytest
import yaml
from copy import deepcopy
from dataclasses import replace

from torq_cli.application import resolve as resolve_module
from torq_cli.application.resolve import resolve_text
from torq_cli.application.resolve import envelope_to_dict
from torq_cli.interfaces.cli import exit_code_for
from torq_cli.domain import drift_oracle
from torq_cli.domain import config_schema as config_schema_module
from torq_cli.domain.findings import FindingCatalog
from torq_cli.domain.hermetic import ProtectedPathError


def valid_config() -> dict:
    return {
        "config_version": 1,
        "profile": {"id": "torq-v5-6-live", "version": "1.0.0"},
        "binding_overrides": {},
        "connectors": {},
        "policy": {
            "independence_mode": "profile_minimum",
            "unattestable_action": "deny",
            "loop_budget": 1,
            "resource_limits": {
                "max_runtime_seconds": 60,
                "max_cost_cents": 100,
                "max_file_count": 10,
                "max_changed_lines": 100,
            },
        },
    }


def test_offline_never_effective() -> None:
    result = resolve_text("status_offline", yaml.safe_dump(valid_config()), "explicit.yaml")

    assert result.status == "unattested"
    assert result.data["runtime_state"] == "offline_unattested"
    assert result.data["runtime_effective"] is False
    assert result.data["provider_available"] is False
    assert result.data["usage"] == "unreported"
    assert set(result.data) == {
        "runtime_state", "runtime_effective", "provider_available", "usage"
    }
    assert result.snapshot is not None
    assert result.snapshot.registry_resource_sha256 is not None


def test_invalid_config_returns_partial_immutable_snapshot() -> None:
    config = valid_config()
    del config["config_version"]

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_validate"
    assert result.snapshot.config_path == "explicit.yaml"
    assert [finding.id for finding in result.findings] == [
        "config_version_missing", "config_schema_invalid",
    ]


def test_connector_failure_snapshot_is_eligibility_stage() -> None:
    result = resolve_text(
        "profile_validate",
        yaml.safe_dump(_mismatched_config("openai", "direct_api")),
        "explicit.yaml",
    )

    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "eligibility"


def test_unknown_profile_failure_snapshot_is_profile_stage() -> None:
    config = valid_config()
    config["profile"] = {"id": "missing-profile", "version": "1.0.0"}

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "profile_resolve"
    assert result.findings[0].id == "profile_unknown"


def test_duplicate_yaml_mapping_is_rejected_before_schema_validation() -> None:
    text = """config_version: 1
profile:
  id: torq-v5-6-live
  version: 1.0.0
binding_overrides: {}
connectors: {}
policy:
  independence_mode: profile_minimum
  unattestable_action: deny
  loop_budget: 1
  resource_limits:
    max_runtime_seconds: 60
    max_cost_cents: 100
    max_file_count: 10
    max_changed_lines: 100
policy:
  independence_mode: profile_minimum
  unattestable_action: deny
  loop_budget: 1
  resource_limits:
    max_runtime_seconds: 60
    max_cost_cents: 100
    max_file_count: 10
    max_changed_lines: 100
"""

    result = resolve_text("profile_validate", text, "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_parse"
    assert len(result.findings) == 1
    assert result.findings[0].id == "config_syntax_invalid"


def test_parser_failure_has_fixed_snapshot_and_stops_before_later_stages(monkeypatch) -> None:
    def fail_later(*args, **kwargs):
        raise AssertionError("config parser failure must stop before schema validation")

    monkeypatch.setattr(resolve_module, "validate_config_shape", fail_later)
    result = resolve_text("profile_validate", "\ufeffconfig_version: 1\n", "secret-config.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.registry_id == "torq-cli-role-registry"
    assert result.snapshot.registry_version == "1.0.0"
    assert result.snapshot.registry_resource_sha256 is not None
    assert result.snapshot.config_path is None
    assert result.snapshot.config_version is None
    assert result.snapshot.profile_id is None
    assert result.snapshot.profile_version is None
    assert result.snapshot.resolution_stage == "config_parse"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == "config_syntax_invalid"
    assert finding.path == "/"
    assert finding.context == {}
    assert result.data == {}


@pytest.mark.parametrize("payload", [b"\xff", b"\xef\xbb\xbfconfig_version: 1\n"])
def test_file_parser_encoding_failures_are_config_parse(tmp_path, payload: bytes) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(payload)

    result = resolve_module.resolve_path("profile_validate", str(path))

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_parse"
    assert result.snapshot.config_path is None
    assert result.findings[0].id == "config_syntax_invalid"
    assert result.data == {}


def test_file_parser_size_bound_is_inclusive(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    prefix = b"config_version: 1\n"
    path.write_bytes(prefix + b"#" * (65_536 - len(prefix)))
    at_limit = resolve_module.resolve_path("profile_validate", str(path))
    assert at_limit.snapshot is not None
    assert at_limit.snapshot.resolution_stage == "config_validate"
    assert at_limit.findings[0].id == "config_schema_invalid"

    path.write_bytes(prefix + b"#" * (65_537 - len(prefix)))
    result = resolve_module.resolve_path("profile_validate", str(path))
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_parse"
    assert result.findings[0].id == "config_syntax_invalid"


def test_unexpected_failure_returns_internal_error_without_leaking_details(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("secret-internal-sentinel")

    monkeypatch.setattr(resolve_module, "validate_config_shape", explode)
    result = resolve_text("profile_validate", yaml.safe_dump(valid_config()), "explicit.yaml")

    assert result.status == "internal_error"
    assert result.findings[0].id == "internal_error"
    assert result.findings[0].exit_code == 5
    assert exit_code_for(result.status, False, result.findings) == 5
    assert "secret-internal-sentinel" not in repr(envelope_to_dict(result))


def _assert_parser_internal_error(result, sentinel: str) -> None:
    assert result.status == "internal_error"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_parse"
    assert result.snapshot.registry_id == "torq-cli-role-registry"
    assert result.snapshot.registry_version == "1.0.0"
    assert result.snapshot.registry_resource_sha256 is not None
    assert result.snapshot.config_path is None
    assert result.snapshot.config_version is None
    assert result.snapshot.profile_id is None
    assert result.snapshot.profile_version is None
    assert result.data == {}
    assert [finding.id for finding in result.findings] == ["internal_error"]
    assert result.findings[0].exit_code == 5
    assert exit_code_for(result.status, False, result.findings) == 5
    assert sentinel not in repr(envelope_to_dict(result))


@pytest.mark.parametrize("boundary", ["events", "compose", "node", "construct"])
def test_unexpected_parser_boundaries_are_opaque_through_public_resolution(monkeypatch, boundary: str) -> None:
    sentinel = f"parser-{boundary}-secret-sentinel"

    def explode(*args, **kwargs):
        raise RuntimeError(sentinel)

    if boundary == "events":
        monkeypatch.setattr(config_schema_module, "_preflight_events", explode)
    elif boundary == "compose":
        monkeypatch.setattr(config_schema_module.yaml, "compose_all", explode)
    elif boundary == "node":
        monkeypatch.setattr(config_schema_module, "_preflight_node", explode)
    else:
        monkeypatch.setattr(config_schema_module, "_construct_preflighted", explode)

    result = resolve_text("profile_validate", yaml.safe_dump(valid_config()), "secret.yaml")
    _assert_parser_internal_error(result, sentinel)


def test_unexpected_bytes_parser_failure_is_opaque(monkeypatch) -> None:
    sentinel = "bytes-parser-secret-sentinel"

    def explode(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(resolve_module, "parse_config_bytes", explode)
    result = resolve_module._resolve_config_bytes(
        "profile_validate", b"config_version: 1\n", "secret.yaml", resolve_module.load_registry(), False
    )
    _assert_parser_internal_error(result, sentinel)


def test_unexpected_reader_failure_is_opaque_at_config_read(monkeypatch) -> None:
    sentinel = "reader-secret-sentinel"

    def explode(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(resolve_module.ReadOnlyConfigReader, "read_utf8", explode)
    result = resolve_module.resolve_path("profile_validate", r"C:\safe\config.yaml")

    assert result.status == "internal_error"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_read"
    assert result.snapshot.registry_id == "torq-cli-role-registry"
    assert result.snapshot.registry_version == "1.0.0"
    assert result.snapshot.registry_resource_sha256 is not None
    assert result.snapshot.config_path is None
    assert result.snapshot.config_version is None
    assert result.snapshot.profile_id is None
    assert result.snapshot.profile_version is None
    assert result.data == {}
    assert [finding.id for finding in result.findings] == ["internal_error"]
    assert result.findings[0].exit_code == 5
    assert sentinel not in repr(envelope_to_dict(result))


def test_genuine_config_syntax_error_remains_invalid_not_internal() -> None:
    result = resolve_text("profile_validate", "a: 1\na: 2\n", "secret.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_parse"
    assert result.findings[0].id == "config_syntax_invalid"
    assert exit_code_for(result.status, False, result.findings) == 2


def test_validation_and_resolver_do_not_mutate_supplied_mappings() -> None:
    config = valid_config()
    before = deepcopy(config)
    registry = resolve_module.load_registry()
    resolve_module.validate_config_shape(config, registry)
    resolve_module.resolve_profile(config, registry)
    resolve_module.validate_eligibility(config, registry)
    assert config == before

    controlled = valid_config()
    controlled_before = deepcopy(controlled)
    resolve_module._resolve_config_mapping(
        "profile_validate", controlled, "explicit.yaml", registry, False
    )
    assert controlled == controlled_before


def test_hardlink_reader_failure_maps_to_protected_resolver_result(monkeypatch) -> None:
    def hardlink_failure(*args, **kwargs):
        raise ProtectedPathError("hardlink-secret-sentinel")

    monkeypatch.setattr(resolve_module.ReadOnlyConfigReader, "read_utf8", hardlink_failure)
    result = resolve_module.resolve_path("profile_validate", r"C:\safe\hardlink.yaml")

    assert result.status == "blocked"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_read"
    assert result.findings[0].id == "protected_path_denied"
    assert exit_code_for(result.status, False, result.findings) == 3
    assert "hardlink-secret-sentinel" not in repr(envelope_to_dict(result))


def test_missing_oracle_is_a_stable_finding(monkeypatch) -> None:
    config = valid_config()
    config["profile"] = {"id": "torq-v5-repo-compat", "version": "1.0.0"}

    def missing():
        raise FileNotFoundError("oracle-secret-sentinel")

    monkeypatch.setattr(resolve_module, "load_packaged_oracle", missing)
    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "blocked"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "oracle_validate"
    assert result.findings[0].id == "oracle_fixture_missing"
    assert "oracle-secret-sentinel" not in repr(envelope_to_dict(result))


def _mismatched_config(provider_id: str, surface: str) -> dict:
    config = valid_config()
    config["binding_overrides"] = {
        "builder": {"connector_id": "builder-main", "enabled": True},
    }
    config["connectors"] = {
        "builder-main": {
            "provider_id": provider_id,
            "surface": surface,
            "enabled": True,
            "credential_ref": "credref_0123456789abcdef0123456789abcdef",
        },
    }
    return config


def test_canonical_connector_id_provider_mismatch_blocks() -> None:
    result = resolve_text(
        "profile_validate",
        yaml.safe_dump(_mismatched_config("openai", "direct_api")),
        "explicit.yaml",
    )

    assert result.status == "blocked"
    assert exit_code_for(result.status, False, result.findings) == 3
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == "connector_provider_mismatch"
    assert finding.path == "/binding_overrides/builder/connector_id"
    assert finding.severity.value == "medium"
    assert finding.bucket == "C"
    assert finding.status_class == "blocked"
    assert finding.exit_code == 3
    serialized = repr(envelope_to_dict(result))
    assert "openai" not in serialized


def test_canonical_connector_id_surface_mismatch_blocks() -> None:
    result = resolve_text(
        "profile_validate",
        yaml.safe_dump(_mismatched_config("deepseek", "agent_sdk")),
        "explicit.yaml",
    )

    assert result.status == "blocked"
    assert exit_code_for(result.status, False, result.findings) == 3
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == "connector_surface_mismatch"
    assert finding.path == "/binding_overrides/builder/connector_id"
    assert finding.severity.value == "medium"
    assert finding.bucket == "C"
    assert finding.status_class == "blocked"
    assert finding.exit_code == 3
    serialized = repr(envelope_to_dict(result))
    assert "agent_sdk" not in serialized


def test_protected_config_read_failure_retains_partial_registry_snapshot() -> None:
    registry = resolve_module.load_registry()
    result = resolve_module.config_read_failure(
        "profile_validate", r"C:config.yaml", registry, finding_id="protected_path_denied"
    )

    assert result.status == "blocked"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_read"
    assert result.snapshot.registry_resource_sha256 is not None
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == "protected_path_denied"
    assert finding.severity.value == "critical"
    assert finding.bucket == "A"
    assert finding.status_class == "blocked"
    assert finding.exit_code == 3


def test_config_read_failure_uses_the_validated_registry_without_reloading(monkeypatch) -> None:
    registry = resolve_module.load_registry()

    def fail_reload():
        raise AssertionError("config read failure must not reload the registry")

    monkeypatch.setattr(resolve_module, "load_registry", fail_reload)
    result = resolve_module.config_read_failure(
        "profile_validate", "explicit.yaml", registry, finding_id="config_unreadable"
    )

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.registry_resource_sha256 == registry.resource_sha256
    assert result.snapshot.resolution_stage == "config_read"
    assert result.findings[0].id == "config_unreadable"


def test_unknown_profile_stops_before_forbidden_override() -> None:
    config = valid_config()
    config["profile"] = {"id": "missing-profile", "version": "1.0.0"}
    config["binding_overrides"] = {"builder": {"provider_id": "openai"}}

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert [finding.id for finding in result.findings] == ["profile_unknown"]
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "profile_resolve"


def test_config_schema_failure_stops_before_later_eligibility_finding() -> None:
    config = valid_config()
    del config["config_version"]
    config["binding_overrides"] = {
        "builder": {"connector_id": "builder-main", "enabled": True},
    }
    config["connectors"] = {
        "builder-main": {
            "provider_id": "openai",
            "surface": "direct_api",
            "enabled": True,
            "credential_ref": "credref_0123456789abcdef0123456789abcdef",
        },
    }

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert [finding.id for finding in result.findings] == [
        "config_version_missing", "config_schema_invalid",
    ]
    assert all(finding.stage == "config_validate" for finding in result.findings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", {"secret": "profile-secret-sentinel"}),
        ("id", ["profile-secret-sentinel"]),
        ("version", {"secret": "profile-secret-sentinel"}),
        ("version", ["profile-secret-sentinel"]),
    ],
)
def test_malformed_profile_identity_is_closed_and_non_disclosing(field: str, value) -> None:
    config = valid_config()
    config["profile"][field] = value

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")
    serialized = json.dumps(envelope_to_dict(result), sort_keys=True)

    assert result.status == "invalid"
    assert result.snapshot is not None
    if isinstance(value, list):
        assert result.snapshot.resolution_stage == "config_parse"
        assert result.snapshot.config_path is None
        assert result.snapshot.profile_id is None
        assert result.snapshot.profile_version is None
        assert result.data == {}
    else:
        assert result.snapshot.resolution_stage == "config_validate"
    if isinstance(value, list):
        assert result.findings[0].id == "config_syntax_invalid"
    else:
        assert any(finding.id == "config_schema_invalid" for finding in result.findings)
    assert not any(finding.id == "internal_error" for finding in result.findings)
    assert "profile-secret-sentinel" not in serialized
    if not isinstance(value, list):
        assert set(result.data) == {
            "schema_valid", "declaratively_eligible", "runtime_effective",
            "profile_id", "profile_version",
        }


def test_recursive_profile_identity_alias_is_invalid_without_internal_error() -> None:
    text = """config_version: 1
profile: &profile
  id: *profile
  version: 1.0.0
binding_overrides: {}
connectors: {}
policy:
  independence_mode: profile_minimum
  unattestable_action: deny
  loop_budget: 1
  resource_limits:
    max_runtime_seconds: 60
    max_cost_cents: 100
    max_file_count: 10
    max_changed_lines: 100
"""

    result = resolve_text("profile_validate", text, "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.profile_id is None
    assert result.snapshot.profile_version is None
    assert result.findings[0].id == "config_syntax_invalid"
    assert not any(finding.id == "internal_error" for finding in result.findings)


def test_boolean_config_version_is_invalid_and_not_snapshotted() -> None:
    config = valid_config()
    config["config_version"] = True

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.config_version is None
    assert result.findings[0].id == "config_schema_invalid"


def test_config_stage_failure_does_not_invoke_later_stages(monkeypatch) -> None:
    events: list[str] = []

    def config_stage(config, registry):
        events.append("config_validate")
        return (FindingCatalog.make("config_schema_invalid", path="/policy"),)

    def later_stage(*args, **kwargs):
        events.append("later_stage")
        raise RuntimeError("later-stage-sentinel")

    monkeypatch.setattr(resolve_module, "validate_config_shape", config_stage, raising=False)
    monkeypatch.setattr(resolve_module, "resolve_profile", later_stage, raising=False)
    monkeypatch.setattr(resolve_module, "validate_eligibility", later_stage, raising=False)
    monkeypatch.setattr(resolve_module, "load_packaged_oracle", later_stage)

    result = resolve_text("profile_validate", yaml.safe_dump(valid_config()), "explicit.yaml")

    assert events == ["config_validate"]
    assert result.status == "invalid"
    assert [finding.id for finding in result.findings] == ["config_schema_invalid"]
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_validate"


def test_profile_stage_failure_does_not_invoke_eligibility_or_oracle(monkeypatch) -> None:
    events: list[str] = []

    def config_stage(config, registry):
        events.append("config_validate")
        return ()

    def profile_stage(config, registry):
        events.append("profile_resolve")
        return (FindingCatalog.make("profile_unknown", path="/profile/id"),)

    def later_stage(*args, **kwargs):
        events.append("later_stage")
        raise RuntimeError("later-stage-sentinel")

    monkeypatch.setattr(resolve_module, "validate_config_shape", config_stage, raising=False)
    monkeypatch.setattr(resolve_module, "resolve_profile", profile_stage, raising=False)
    monkeypatch.setattr(resolve_module, "validate_eligibility", later_stage, raising=False)
    monkeypatch.setattr(resolve_module, "load_packaged_oracle", later_stage)

    result = resolve_text("profile_validate", yaml.safe_dump(valid_config()), "explicit.yaml")

    assert events == ["config_validate", "profile_resolve"]
    assert result.status == "invalid"
    assert [finding.id for finding in result.findings] == ["profile_unknown"]
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "profile_resolve"


def test_same_stage_findings_are_returned_deterministically_without_later_calls(monkeypatch) -> None:
    events: list[str] = []

    def config_stage(config, registry):
        events.append("config_validate")
        return (
            FindingCatalog.make("config_schema_invalid", path="/policy"),
            FindingCatalog.make("config_schema_invalid", path="/profile"),
        )

    def later_stage(*args, **kwargs):
        events.append("later_stage")
        raise RuntimeError("later-stage-sentinel")

    monkeypatch.setattr(resolve_module, "validate_config_shape", config_stage, raising=False)
    monkeypatch.setattr(resolve_module, "resolve_profile", later_stage, raising=False)

    result = resolve_text("profile_validate", yaml.safe_dump(valid_config()), "explicit.yaml")

    assert events == ["config_validate"]
    assert [finding.path for finding in result.findings] == ["/policy", "/profile"]
    assert result.status == "invalid"


def test_wrong_hash_prompt_provenance_is_blocked_before_json_parse(monkeypatch) -> None:
    oracle = drift_oracle.load_packaged_oracle()
    fixture_bytes = dict(oracle.fixture_bytes)
    fixture_bytes["prompt_provenance.normalized.json"] = b" " + fixture_bytes["prompt_provenance.normalized.json"]
    changed = replace(oracle, fixture_bytes=fixture_bytes)
    parse_calls: list[object] = []
    original_load = drift_oracle.json.loads

    def spy_load(value, *args, **kwargs):
        parse_calls.append(value)
        return original_load(value, *args, **kwargs)

    monkeypatch.setattr(resolve_module, "load_packaged_oracle", lambda: changed)
    monkeypatch.setattr(drift_oracle.json, "loads", spy_load)

    config = valid_config()
    config["profile"] = {"id": "torq-v5-repo-compat", "version": "1.0.0"}
    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "blocked"
    assert len(result.findings) == 1
    assert result.findings[0].id == "oracle_fixture_hash_mismatch"
    assert result.findings[0].stage == "oracle_validate"
    assert result.findings[0].exit_code == 3
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "oracle_validate"
    assert result.data == {}
    assert parse_calls == []
    assert "prompt_provenance" not in json.dumps(envelope_to_dict(result), sort_keys=True)
