from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest
import yaml

from torq_cli.application import resolve as resolve_module
from torq_cli.application.resolve import resolve_text
from torq_cli.domain import drift_oracle
from torq_cli.domain.findings import CATALOG, FindingCatalog, FindingSeverity
from torq_cli.domain.models import ResultEnvelope, ResolutionSnapshot
from torq_cli.domain.registry_schema import (
    RegistryResourceMissing,
    RegistrySyntaxError,
    RegistryUnreadable,
    load_registry,
)
from torq_cli.interfaces.cli import main


def test_finding_catalog_exposes_fixed_contract() -> None:
    finding = FindingCatalog.make("config_version_missing", path="/")

    assert finding.id == "config_version_missing"
    assert finding.message == "Config version is required."
    assert finding.severity is FindingSeverity.HIGH
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    assert finding.stage == "config_validate"
    assert finding.exit_code == 2


def test_result_envelope_snapshot_is_immutable() -> None:
    snapshot = ResolutionSnapshot(
        registry_id=None,
        registry_version=None,
        registry_resource_sha256=None,
        config_path=None,
        config_version=None,
        profile_id=None,
        profile_version=None,
        resolution_stage="registry_read",
    )
    envelope = ResultEnvelope(
        schema_version="1.0.0",
        command="profile_validate",
        status="invalid",
        snapshot=snapshot,
        findings=(FindingCatalog.make("config_version_missing", path="/"),),
        data={"schema_valid": False},
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.resolution_stage = "complete"  # type: ignore[misc]
    assert envelope.findings[0].id == "config_version_missing"


def test_finding_catalog_is_exhaustive_and_context_is_deeply_immutable() -> None:
    expected = {
        "registry_resource_missing", "registry_unreadable", "registry_syntax_invalid",
        "registry_schema_invalid", "registry_version_unsupported", "registry_duplicate_identity",
        "registry_prompt_hash_mismatch", "registry_transition_invalid", "registry_profile_invalid",
        "config_unreadable", "config_syntax_invalid", "config_schema_invalid",
        "config_version_missing", "config_version_unsupported", "migration_required",
        "raw_credential_field_forbidden", "credential_ref_invalid", "profile_unknown",
        "profile_version_unknown", "binding_override_forbidden", "connector_unknown",
        "connector_provider_mismatch", "connector_surface_mismatch", "required_role_disabled",
        "binding_ineligible", "independence_unsatisfied", "oracle_fixture_missing",
        "oracle_fixture_hash_mismatch", "oracle_fixture_schema_invalid", "oracle_model_mismatch_g1d",
        "oracle_model_mismatch_g2a", "oracle_prompt_mismatch_g2a",
        "oracle_prompt_path_missing_g2a_adversarial", "oracle_scope_not_applicable",
        "runtime_unattested", "protected_path_denied", "internal_error",
        "policy_contract_hash_mismatch", "oracle_manifest_trusted_hash_mismatch",
        "legacy_config_unreadable", "legacy_config_syntax_invalid",
        "legacy_config_schema_invalid", "legacy_config_secret_field_forbidden",
        "legacy_config_role_duplicate", "legacy_config_role_missing",
        "legacy_config_mapping_unsupported", "legacy_config_projection_invalid",
    }
    assert set(CATALOG) == expected

    finding = FindingCatalog.make("config_schema_invalid", path="/", context={"nested": {"secret": "value"}})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        finding.context["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        finding.context["nested"]["secret"] = "changed"  # type: ignore[index]


def _valid_config() -> dict:
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
                "max_runtime_seconds": 60, "max_cost_cents": 100,
                "max_file_count": 10, "max_changed_lines": 100,
            },
        },
    }


def _registry_result(monkeypatch, mutate):
    registry = mutate(load_registry())
    monkeypatch.setattr(resolve_module, "load_registry", lambda: registry)
    return resolve_text("profile_validate", yaml.safe_dump(_valid_config()), "explicit.yaml")


def _registry_exception(monkeypatch, exception):
    monkeypatch.setattr(resolve_module, "load_registry", lambda: (_ for _ in ()).throw(exception))
    return resolve_text("profile_validate", yaml.safe_dump(_valid_config()), "explicit.yaml")


def _connector_result(monkeypatch, provider="deepseek", surface="direct_api", missing=False):
    config = _valid_config()
    config["binding_overrides"] = {"builder": {"connector_id": "builder-main", "enabled": True}}
    config["connectors"] = {} if missing else {"builder-main": {
        "provider_id": provider, "surface": surface, "enabled": True,
        "credential_ref": "credref_0123456789abcdef0123456789abcdef",
    }}
    return resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")


def _oracle_result(name: str):
    oracle = drift_oracle.load_packaged_oracle()
    fixture_bytes = dict(oracle.fixture_bytes)
    fixture_hashes = dict(oracle.fixture_hashes)
    manifest = yaml.safe_load(oracle.manifest_bytes.decode("ascii"))
    if name == "oracle_manifest_trusted_hash_mismatch":
        return drift_oracle.validate_oracle(replace(oracle, manifest_bytes=b"tampered"))
    if name == "oracle_fixture_missing":
        del fixture_bytes["role_map.normalized.json"]
        return drift_oracle.validate_oracle(replace(oracle, fixture_bytes=fixture_bytes))
    if name == "oracle_fixture_hash_mismatch":
        fixture_bytes["role_map.normalized.json"] = b"{}"
        return drift_oracle.validate_oracle(replace(oracle, fixture_bytes=fixture_bytes))
    fixture_name = "role_map.normalized.json"
    value = json.loads(fixture_bytes[fixture_name].decode("utf-8"))
    if name == "oracle_fixture_schema_invalid":
        value = {}
        fixture_name = "role_map.normalized.json"
    elif name == "oracle_model_mismatch_g1d":
        value["roles"][0]["model"] = "wrong-model"
    elif name == "oracle_model_mismatch_g2a":
        value["roles"][3]["model"] = "wrong-model"
    elif name == "oracle_prompt_mismatch_g2a":
        fixture_name = "v5_config.normalized.json"
        value = json.loads(fixture_bytes[fixture_name].decode("utf-8"))
        value["agents"][3]["prompt"] = "wrong-prompt"
    elif name == "oracle_prompt_path_missing_g2a_adversarial":
        fixture_name = "prompt_provenance.normalized.json"
        value = json.loads(fixture_bytes[fixture_name].decode("utf-8"))
        value["entries"][3]["present"] = True
    changed = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fixture_bytes[fixture_name] = changed
    digest = hashlib.sha256(changed).hexdigest()
    fixture_hashes[fixture_name] = digest
    manifest["fixtures"][fixture_name]["sha256"] = digest
    manifest_bytes = yaml.safe_dump(manifest, sort_keys=False).encode("ascii")
    changed_oracle = replace(
        oracle, manifest_bytes=manifest_bytes, fixture_bytes=fixture_bytes,
        fixture_hashes=fixture_hashes,
        trusted_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return drift_oracle.validate_oracle(changed_oracle)


def _cli_output(main_args, capsys):
    main(main_args)
    return json.loads(capsys.readouterr().out)


def _reachable_registry_schema(monkeypatch):
    return _registry_result(monkeypatch, lambda registry: replace(registry, raw_document={}))


def _reachable_duplicate(monkeypatch):
    registry = load_registry()
    raw = dict(registry.raw_document)
    roles = list(raw["roles"])
    roles.append(dict(roles[0]))
    raw["roles"] = roles
    return _registry_result(monkeypatch, lambda _: replace(registry, raw_document=raw))


def _reachable_prompt_hash(monkeypatch):
    registry = load_registry()
    prompt = registry.prompts["live.g1d.design"]
    return _registry_result(monkeypatch, lambda _: replace(registry, prompts={**registry.prompts, prompt.prompt_id: replace(prompt, content_sha256="bad")}))


def _reachable_transition(monkeypatch):
    registry = load_registry()
    return _registry_result(monkeypatch, lambda _: replace(registry, transitions=()))


def _reachable_profile(monkeypatch):
    registry = load_registry()
    return _registry_result(monkeypatch, lambda _: replace(registry, profiles={}))


def _reachable_binding_ineligible(monkeypatch):
    registry = load_registry()
    profile = registry.profiles["torq-v5-6-live"]
    binding = replace(profile.bindings["builder"], model_id="glm-5.2")
    changed = replace(profile, bindings={**profile.bindings, "builder": binding})
    return _registry_result(monkeypatch, lambda _: replace(registry, profiles={**registry.profiles, profile.profile_id: changed}))


def _reachable_policy_hash(monkeypatch):
    registry = load_registry()
    return _registry_result(monkeypatch, lambda _: replace(registry, policy=replace(registry.policy, contract_sha256="bad")))


def _reachable_config(config):
    return lambda monkeypatch, capsys: resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")


def _reachable_config_read(monkeypatch, capsys):
    return _cli_output(["profile", "validate", "--config", r"C:\missing\config.yaml"], capsys)


def _reachable_protected(monkeypatch, capsys):
    return _cli_output(["profile", "validate", "--config", r"E:\TORQ-CONSOLE\config.yaml"], capsys)


def _reachable_internal(monkeypatch, capsys):
    monkeypatch.setattr(resolve_module, "validate_config_shape", lambda *args: (_ for _ in ()).throw(RuntimeError("sentinel")))
    return resolve_text("profile_validate", yaml.safe_dump(_valid_config()), "explicit.yaml")


def _reachable_oracle(name):
    return lambda monkeypatch, capsys: _oracle_result(name)


def _reachable_independence(monkeypatch, capsys):
    config = _valid_config()
    config["policy"]["independence_mode"] = "vendor_strict"
    return resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")


def _reachable_legacy(finding_id):
    def invoke(monkeypatch, tmp_path, capsys):
        from torq_cli.application.import_v5_config import reachability_case

        return reachability_case(finding_id)

    return invoke


REACHABILITY_CASES = {
    "registry_resource_missing": lambda m, c, cap: _registry_exception(m, RegistryResourceMissing()),
    "registry_unreadable": lambda m, c, cap: _registry_exception(m, RegistryUnreadable()),
    "registry_syntax_invalid": lambda m, c, cap: _registry_exception(m, RegistrySyntaxError()),
    "registry_schema_invalid": lambda m, c, cap: _reachable_registry_schema(m),
    "registry_version_unsupported": lambda m, c, cap: _registry_result(m, lambda r: replace(r, registry_version="9.0.0")),
    "registry_duplicate_identity": lambda m, c, cap: _reachable_duplicate(m),
    "registry_prompt_hash_mismatch": lambda m, c, cap: _reachable_prompt_hash(m),
    "registry_transition_invalid": lambda m, c, cap: _reachable_transition(m),
    "registry_profile_invalid": lambda m, c, cap: _reachable_profile(m),
    "config_unreadable": lambda m, c, cap: _reachable_config_read(m, cap),
    "config_syntax_invalid": lambda m, c, cap: resolve_text("profile_validate", ": [", "explicit.yaml"),
    "config_schema_invalid": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({"unknown": True}), "explicit.yaml"),
    "config_version_missing": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({}), "explicit.yaml"),
    "config_version_unsupported": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "config_version": 2}), "explicit.yaml"),
    "migration_required": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "config_version": 0}), "explicit.yaml"),
    "raw_credential_field_forbidden": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "api-key": "secret"}), "explicit.yaml"),
    "credential_ref_invalid": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "connectors": {"x": {"provider_id": "deepseek", "surface": "direct_api", "enabled": True, "credential_ref": "bad"}}}), "explicit.yaml"),
    "profile_unknown": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "profile": {"id": "missing", "version": "1.0.0"}}), "explicit.yaml"),
    "profile_version_unknown": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "profile": {"id": "torq-v5-6-live", "version": "9.9.9"}}), "explicit.yaml"),
    "binding_override_forbidden": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "binding_overrides": {"builder": {"provider_id": "openai"}}}), "explicit.yaml"),
    "connector_unknown": lambda m, c, cap: _connector_result(m, missing=True),
    "connector_provider_mismatch": lambda m, c, cap: _connector_result(m, provider="openai"),
    "connector_surface_mismatch": lambda m, c, cap: _connector_result(m, surface="agent_sdk"),
    "required_role_disabled": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump({**_valid_config(), "binding_overrides": {"builder": {"enabled": False}}}), "explicit.yaml"),
    "binding_ineligible": lambda m, c, cap: _reachable_binding_ineligible(m),
    "independence_unsatisfied": lambda m, c, cap: _reachable_independence(m, cap),
    "oracle_fixture_missing": lambda m, c, cap: _reachable_oracle("oracle_fixture_missing")(m, cap),
    "oracle_fixture_hash_mismatch": lambda m, c, cap: _reachable_oracle("oracle_fixture_hash_mismatch")(m, cap),
    "oracle_fixture_schema_invalid": lambda m, c, cap: _reachable_oracle("oracle_fixture_schema_invalid")(m, cap),
    "oracle_model_mismatch_g1d": lambda m, c, cap: _reachable_oracle("oracle_model_mismatch_g1d")(m, cap),
    "oracle_model_mismatch_g2a": lambda m, c, cap: _reachable_oracle("oracle_model_mismatch_g2a")(m, cap),
    "oracle_prompt_mismatch_g2a": lambda m, c, cap: _reachable_oracle("oracle_prompt_mismatch_g2a")(m, cap),
    "oracle_prompt_path_missing_g2a_adversarial": lambda m, c, cap: _reachable_oracle("oracle_prompt_path_missing_g2a_adversarial")(m, cap),
    "oracle_scope_not_applicable": lambda m, c, cap: resolve_text("profile_validate", yaml.safe_dump(_valid_config()), "explicit.yaml"),
    "runtime_unattested": lambda m, c, cap: resolve_text("status_offline", yaml.safe_dump(_valid_config()), "explicit.yaml"),
    "protected_path_denied": lambda m, c, cap: _reachable_protected(m, cap),
    "internal_error": lambda m, c, cap: _reachable_internal(m, cap),
    "policy_contract_hash_mismatch": lambda m, c, cap: _reachable_policy_hash(m),
    "oracle_manifest_trusted_hash_mismatch": lambda m, c, cap: _reachable_oracle("oracle_manifest_trusted_hash_mismatch")(m, cap),
    "legacy_config_unreadable": _reachable_legacy("legacy_config_unreadable"),
    "legacy_config_syntax_invalid": _reachable_legacy("legacy_config_syntax_invalid"),
    "legacy_config_schema_invalid": _reachable_legacy("legacy_config_schema_invalid"),
    "legacy_config_secret_field_forbidden": _reachable_legacy("legacy_config_secret_field_forbidden"),
    "legacy_config_role_duplicate": _reachable_legacy("legacy_config_role_duplicate"),
    "legacy_config_role_missing": _reachable_legacy("legacy_config_role_missing"),
    "legacy_config_mapping_unsupported": _reachable_legacy("legacy_config_mapping_unsupported"),
    "legacy_config_projection_invalid": _reachable_legacy("legacy_config_projection_invalid"),
}


def _finding_ids(result):
    if isinstance(result, tuple):
        return set(result)
    if isinstance(result, dict):
        return {item["id"] for item in result["findings"]}
    return {finding.id for finding in result.findings}


def test_reachability_case_keys_exactly_equal_finding_catalog_keys() -> None:
    assert set(REACHABILITY_CASES) == set(CATALOG)


@pytest.mark.parametrize("finding_id", sorted(REACHABILITY_CASES))
def test_all_foundation_catalog_findings_are_behaviorally_reachable(finding_id, monkeypatch, tmp_path, capsys) -> None:
    result = REACHABILITY_CASES[finding_id](monkeypatch, tmp_path, capsys)

    assert finding_id in _finding_ids(result)
