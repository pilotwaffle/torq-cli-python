from dataclasses import replace
import hashlib
from importlib import resources
import json
import yaml

import pytest

from torq_cli.domain.registry_schema import (
    EXPECTED_TRANSITIONS,
    load_registry,
    validate_registry,
    validate_transition,
)


def _load_registry_mutation(monkeypatch, mutate):
    from torq_cli.domain import registry_schema as registry_module

    source = resources.files("torq_cli").joinpath("data", "registry", "v1", "registry.yaml").read_bytes()
    raw = yaml.safe_load(source.decode("utf-8"))
    mutate(raw)
    mutated = yaml.safe_dump(raw, sort_keys=False).encode("utf-8")
    original = registry_module._resource_bytes
    monkeypatch.setattr(
        registry_module,
        "_resource_bytes",
        lambda relative: mutated if relative == "registry.yaml" else original(relative),
    )
    return load_registry()


def _assert_registry_mutation(monkeypatch, mutate, expected_id="registry_schema_invalid") -> None:
    from torq_cli.application import resolve as resolve_module
    from torq_cli.application.resolve import resolve_text

    invalid_registry = _load_registry_mutation(monkeypatch, mutate)
    monkeypatch.setattr(resolve_module, "load_registry", lambda: invalid_registry)
    result = resolve_text("profile_validate", "{}", "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "registry_validate"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == expected_id
    assert finding.path == "/"
    assert finding.severity.value == "high"
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    assert finding.exit_code == 2
    assert result.snapshot.registry_resource_sha256 == invalid_registry.resource_sha256
    assert result.snapshot.config_version is None
    assert result.snapshot.profile_id is None


def _assert_prompt_mutation(monkeypatch, mutate) -> None:
    from torq_cli.application import resolve as resolve_module
    from torq_cli.application.resolve import resolve_text
    from torq_cli.domain import registry_schema as registry_module

    source = resources.files("torq_cli").joinpath("data", "registry", "v1", "registry.yaml").read_bytes()
    raw = yaml.safe_load(source.decode("utf-8"))
    mutate(raw)
    mutated = yaml.safe_dump(raw, sort_keys=False).encode("utf-8")
    original_bytes = registry_module._resource_bytes
    reads: list[str] = []
    original_text = registry_module._resource_text
    monkeypatch.setattr(
        registry_module,
        "_resource_bytes",
        lambda relative: mutated if relative == "registry.yaml" else original_bytes(relative),
    )
    monkeypatch.setattr(
        registry_module,
        "_resource_text",
        lambda relative: (reads.append(relative), original_text(relative))[1],
    )
    invalid_registry = load_registry()
    monkeypatch.setattr(resolve_module, "load_registry", lambda: invalid_registry)
    result = resolve_text("profile_validate", "{}", "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "registry_validate"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.id == "registry_prompt_hash_mismatch"
    assert finding.severity.value == "high"
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    assert finding.stage == "registry_validate"
    assert finding.exit_code == 2
    assert result.snapshot.registry_resource_sha256 == invalid_registry.resource_sha256
    assert all(path.startswith("prompts/") for path in reads)
    assert all(".." not in path and "\\" not in path and "/./" not in path for path in reads)


def test_packaged_registry_prompt_identity_map_is_exact() -> None:
    registry = load_registry()
    assert tuple(
        (
            item["prompt_id"], item["prompt_version"],
            item["resource_path"], item["content_sha256"],
        )
        for item in registry.raw_document["prompts"]
    ) == (
        ("live.g1d.design", "1.0.0", "prompts/live.g1d.design.md", "f3bfbde6af7171290d1ee04edeb22ddbf3186f511e67a7351f138d40e1ef463a"),
        ("live.g1r.review", "1.0.0", "prompts/live.g1r.review.md", "a44ac9ecfe0812af59eed3a3dfd0b1fdaf5641b4bf0c7b237259b6b64b7f8bc3"),
        ("live.builder.execute", "1.0.0", "prompts/live.builder.execute.md", "faaa73cbb1e4990cee081907014f61893a979f67de347aae0909291ef07d1ece"),
        ("live.g2a.audit", "1.0.0", "prompts/live.g2a.audit.md", "05efe5ed6011fe4a1c0c11957768d40bb8921207bf6c4d05f42a62d891d16fac"),
        ("live.refine_bug.repair", "1.0.0", "prompts/live.refine_bug.repair.md", "3da3961a0b1781ce582eecc8d58ffd2aca9c3cfdfb0e5348e0d3f40cd9df8e9c"),
        ("live.refine_ui.repair", "1.0.0", "prompts/live.refine_ui.repair.md", "cae7eaf0b4dae71b56519843b4b171a570d3cc2f4b8e5755a835528f4e95dcf6"),
        ("compat.gate1_design", "1.0.0", "prompts/compat.gate1_design.md", "90c17eaac94c51cac25a862a463de3a4c93fb5cbf696c8c871da058e697a279c"),
        ("compat.gate1_review", "1.0.0", "prompts/compat.gate1_review.md", "f03bae8e49849b3979720df905bf7c83d1a1feada49d0e9f39ffddbefd09a867"),
        ("compat.builder", "1.0.0", "prompts/compat.builder.md", "ea78e683a4da1eb19c2ac3372309d0fc8abd957b01a363dda58ff135fa9c8314"),
        ("compat.g2a_adversarial", "1.0.0", "prompts/compat.g2a_adversarial.md", "13d029794c7f4951be3b0128b7ef34557f76e278fbf039d9e0307bf07c9e41a8"),
        ("compat.refine_bug", "1.0.0", "prompts/compat.refine_bug.md", "42c07e65fdb133515d9643be6917496398c0f52c11e07979022f9fa6267b6794"),
        ("compat.refine_ui", "1.0.0", "prompts/compat.refine_ui.md", "d4edeaaaa36b5301b5f3e62f4fe7215aeaba45d81d879c90ba41a0371eb385c5"),
    )


def test_prompt_dot_path_rejected_without_reading_mutated_path(monkeypatch) -> None:
    _assert_prompt_mutation(
        monkeypatch,
        lambda raw: raw["prompts"][0].__setitem__("resource_path", "prompts/./live.g1d.design.md"),
    )


def test_prompt_parent_path_rejected_without_reading_mutated_path(monkeypatch) -> None:
    _assert_prompt_mutation(
        monkeypatch,
        lambda raw: raw["prompts"][0].__setitem__("resource_path", "prompts/x/../live.g1d.design.md"),
    )


def test_prompt_backslash_path_rejected_without_reading_mutated_path(monkeypatch) -> None:
    _assert_prompt_mutation(
        monkeypatch,
        lambda raw: raw["prompts"][0].__setitem__("resource_path", r"prompts\live.g1d.design.md"),
    )


def test_prompt_coherent_repoint_rejected_without_reading_mutated_path(monkeypatch) -> None:
    def mutate(raw) -> None:
        prompt = raw["prompts"][0]
        prompt["resource_path"] = "prompts/live.g1r.review.md"
        prompt["content_sha256"] = hashlib.sha256(
            resources.files("torq_cli").joinpath(
                "data", "registry", "v1", "prompts", "live.g1r.review.md"
            ).read_bytes()
        ).hexdigest()

    _assert_prompt_mutation(monkeypatch, mutate)


def test_registry_mutation_two_policies_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["policies"].append(dict(raw["policies"][0])))


def test_registry_mutation_duplicate_severity_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["severity_buckets"].append(dict(raw["severity_buckets"][0])), "registry_duplicate_identity")


def test_registry_mutation_effort_provider_map_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["efforts"][0].__setitem__("provider_value", "wrong"))


def test_registry_mutation_light_roles_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["strategies"][0].__setitem__("stage_roles", ["builder"]))


def test_registry_mutation_live_profile_version_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0].__setitem__("profile_version", "9.9.9"))


def test_registry_mutation_live_default_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0].__setitem__("default", False))


@pytest.mark.parametrize("default", [1, 0, "true", [], {}, None])
def test_registry_mutation_non_bool_default_rejected_through_public_resolution(monkeypatch, default) -> None:
    _assert_registry_mutation(
        monkeypatch,
        lambda raw: raw["profiles"][0].__setitem__("default", default),
    )


def test_registry_mutation_live_strategy_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0].__setitem__("strategy_id", "light_v1"))


def test_registry_mutation_prompt_version_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["prompts"][0].__setitem__("prompt_version", "9.9.9"), "registry_prompt_hash_mismatch")


def test_registry_mutation_binding_prompt_version_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0]["bindings"][0].__setitem__("prompt_version", "9.9.9"))


def test_registry_mutation_escalation_provider_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0]["escalations"][0].__setitem__("provider_id", "wrong"))


def test_registry_mutation_escalation_model_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0]["escalations"][0].__setitem__("model_id", "wrong"))


def test_registry_mutation_escalation_triggers_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["profiles"][0]["escalations"][0]["triggers"].append({"kind": "task_label", "value": "wrong"}))


def test_registry_mutation_promotion_fixture_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["promotion_procedures"][0].__setitem__("benchmark_fixture_id", "wrong"))


def test_registry_mutation_promotion_fields_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["promotion_procedures"][0]["required_record_fields"].append("wrong"))


def test_registry_mutation_drift_oracle_id_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["drift_oracles"][0].__setitem__("oracle_id", "wrong"))


def test_registry_mutation_drift_source_commit_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["drift_oracles"][0].__setitem__("source_commit", "wrong"))


def test_registry_mutation_drift_fixture_directory_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["drift_oracles"][0].__setitem__("fixture_directory", "wrong"))


def test_registry_mutation_drift_refresh_mode_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["drift_oracles"][0].__setitem__("refresh_mode", "wrong"))


def test_registry_mutation_drift_runtime_access_rejected(monkeypatch) -> None:
    _assert_registry_mutation(monkeypatch, lambda raw: raw["drift_oracles"][0].__setitem__("runtime_access", "wrong"))


def test_duplicate_raw_profile_binding_emits_registry_duplicate_identity(monkeypatch) -> None:
    def mutate(raw) -> None:
        raw["profiles"][0]["bindings"].append(dict(raw["profiles"][0]["bindings"][0]))

    _assert_registry_mutation(monkeypatch, mutate, "registry_duplicate_identity")


def test_coherent_policy_repin_emits_policy_contract_hash_mismatch(monkeypatch) -> None:
    def mutate(raw) -> None:
        policy = raw["policies"][0]
        policy["policy_id"] = "repinned-policy"
        policy["policy_version"] = "9.9.9"
        preimage = {
            "policy_id": policy["policy_id"],
            "policy_version": policy["policy_version"],
            "routing_contract": "foundation-metadata-only",
        }
        policy["contract_sha256"] = hashlib.sha256(
            json.dumps(preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        for profile in raw["profiles"]:
            profile["policy_id"] = policy["policy_id"]

    _assert_registry_mutation(monkeypatch, mutate, "policy_contract_hash_mismatch")


def test_packaged_registry_has_exact_closed_contract() -> None:
    registry = load_registry()

    assert len(registry.prompts) == 12
    assert len(registry.efforts) == 3
    assert len(registry.roles) == 6
    assert len(registry.states) == 13
    assert len(registry.transitions) == 23
    assert len(registry.severity_buckets) == 4
    assert len(registry.strategies) == 2
    assert len(registry.profiles) == 2
    assert len(registry.promotion_procedures) == 1
    assert len(registry.drift_oracles) == 1
    assert registry.policy.contract_sha256 == (
        "fbeb3e1597a8dde9b4e5c1b5b049605bd3ed767c468a3f75b2e362c3e2f153ca"
    )
    assert tuple(registry.efforts) == ("effort.standard", "effort.high", "effort.maximum")
    assert tuple(registry.transitions) == EXPECTED_TRANSITIONS
    assert all(len(profile.independence_rules) == 4 for profile in registry.profiles.values())
    assert all(len(profile.escalations) == 3 for profile in registry.profiles.values())


def test_registry_validates_cleanly_and_rejects_invalid_transition() -> None:
    registry = load_registry()
    assert validate_registry(registry) == ()
    assert not validate_transition(("draft", "approve_build", "complete", "g2a"))


def test_unknown_profile_version_rejected() -> None:
    registry = load_registry()
    assert validate_registry(registry, profile_version="9.9.9")


def test_glm_builder_rejected() -> None:
    from torq_cli.domain.registry_schema import BindingSpec, validate_binding_eligibility

    assert not validate_binding_eligibility(
        BindingSpec("builder", "zai", "glm-5.2", "compat.builder", "1.0.0", "effort.high", "direct_api")
    )


def test_invalid_transition_rejected() -> None:
    assert not validate_transition(("draft", "approve_build", "complete", "g2a"))


def test_registry_retains_authority_and_raw_resource_hash() -> None:
    registry = load_registry()
    raw = resources.files("torq_cli").joinpath("data", "registry", "v1", "registry.yaml").read_bytes()

    assert registry.roles["g1d"].allowed_authority == ("draft_design", "revise_design")
    assert registry.roles["g1d"].prohibited_authority == (
        "approve_design", "approve_build", "apply_primary_diff"
    )
    assert registry.resource_sha256 == hashlib.sha256(raw).hexdigest()


def test_registry_unknown_root_key_is_rejected() -> None:
    registry = load_registry()
    invalid = replace(registry, raw_document={**registry.raw_document, "unexpected": True})

    assert "registry_schema_invalid" in validate_registry(invalid)


@pytest.mark.parametrize("field", ["strategies", "promotion_procedures", "drift_oracles"])
def test_registry_missing_required_collection_is_rejected(field: str) -> None:
    registry = load_registry()
    invalid = replace(registry, **{field: {} if field == "strategies" else ()})

    assert "registry_schema_invalid" in validate_registry(invalid)


def test_registry_profile_contents_and_references_are_closed() -> None:
    registry = load_registry()
    profile = registry.profiles["torq-v5-6-live"]
    invalid_profile = replace(
        profile,
        strategy_id="missing-strategy",
        bindings={**profile.bindings, "unknown": next(iter(profile.bindings.values()))},
        independence_rules=(),
        escalations=(),
        promotion_id="missing-promotion",
    )
    invalid = replace(registry, profiles={**registry.profiles, profile.profile_id: invalid_profile})

    findings = validate_registry(invalid)

    assert "registry_profile_invalid" in findings
    assert "registry_schema_invalid" in findings


def test_duplicate_raw_role_identity_emits_registry_duplicate_identity(monkeypatch) -> None:
    from torq_cli.application import resolve as resolve_module
    from torq_cli.application.resolve import resolve_text

    source = resources.files("torq_cli").joinpath("data", "registry", "v1", "registry.yaml").read_bytes()
    raw = yaml.safe_load(source.decode("utf-8"))
    raw["roles"].append(dict(raw["roles"][0]))
    duplicate_bytes = yaml.safe_dump(raw, sort_keys=False).encode("utf-8")
    original_resource_bytes = __import__("torq_cli.domain.registry_schema", fromlist=["_resource_bytes"])._resource_bytes
    monkeypatch.setattr(
        "torq_cli.domain.registry_schema._resource_bytes",
        lambda relative: duplicate_bytes if relative == "registry.yaml" else original_resource_bytes(relative),
    )
    duplicate_registry = load_registry()
    monkeypatch.setattr(resolve_module, "load_registry", lambda: duplicate_registry)

    result = resolve_text("profile_validate", "{}", "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "registry_validate"
    assert len(result.findings) == 1
    assert result.findings[0].id == "registry_duplicate_identity"
    assert result.findings[0].severity.value == "high"
    assert result.findings[0].bucket == "B"
    assert result.findings[0].status_class == "invalid"
    assert result.findings[0].exit_code == 2
