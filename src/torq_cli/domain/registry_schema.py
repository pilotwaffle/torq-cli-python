"""Closed, immutable packaged registry contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Mapping

import yaml

from .models import deep_freeze


POLICY_HASH = "fbeb3e1597a8dde9b4e5c1b5b049605bd3ed767c468a3f75b2e362c3e2f153ca"
POLICY_ID = "g2a-routing-3.1.3"
POLICY_VERSION = "3.1.3"
_EXPECTED_PROMPTS = (
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
PROFILE_IDS = ("torq-v5-6-live", "torq-v5-repo-compat")
ROLE_IDS = ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
EFFORT_IDS = ("effort.standard", "effort.high", "effort.maximum")
STATE_IDS = (
    "draft", "design_submitted", "design_rejected", "design_approved", "building",
    "build_submitted", "audit_rejected", "refine_bug", "refine_ui", "targeted_reaudit",
    "awaiting_human_approval", "complete", "blocked",
)
EXPECTED_TRANSITIONS = (
    ("draft", "submit_design", "design_submitted", "g1d"),
    ("design_submitted", "reject_design", "design_rejected", "g1r"),
    ("design_submitted", "approve_design", "design_approved", "g1r"),
    ("design_rejected", "revise_design", "draft", "g1d"),
    ("design_approved", "start_build", "building", "builder"),
    ("building", "submit_build", "build_submitted", "builder"),
    ("build_submitted", "reject_build", "audit_rejected", "g2a"),
    ("build_submitted", "approve_build", "awaiting_human_approval", "g2a"),
    ("audit_rejected", "route_bug", "refine_bug", "g2a"),
    ("audit_rejected", "route_ui", "refine_ui", "g2a"),
    ("audit_rejected", "route_builder", "building", "g2a"),
    ("refine_bug", "submit_repair", "targeted_reaudit", "refine_bug"),
    ("refine_ui", "submit_repair", "targeted_reaudit", "refine_ui"),
    ("targeted_reaudit", "reject_build", "audit_rejected", "g2a"),
    ("targeted_reaudit", "approve_build", "awaiting_human_approval", "g2a"),
    ("awaiting_human_approval", "approve_apply", "complete", "operator"),
    ("awaiting_human_approval", "deny_apply", "blocked", "operator"),
    ("draft", "policy_block", "blocked", "system"),
    ("design_submitted", "policy_block", "blocked", "system"),
    ("design_approved", "policy_block", "blocked", "system"),
    ("building", "policy_block", "blocked", "system"),
    ("build_submitted", "policy_block", "blocked", "system"),
    ("targeted_reaudit", "policy_block", "blocked", "system"),
)

_ROOT_KEYS = {
    "registry_schema_version", "registry_id", "registry_version", "policies", "prompts",
    "efforts", "roles", "states", "transitions", "severity_buckets", "strategies",
    "profiles", "promotion_procedures", "drift_oracles",
}
_ROLE_AUTHORITY = {
    "g1d": (("draft_design", "revise_design"), ("approve_design", "approve_build", "apply_primary_diff")),
    "g1r": (("approve_design", "reject_design"), ("draft_design", "produce_sandbox_diff", "apply_primary_diff")),
    "builder": (("produce_sandbox_diff", "run_allowed_tests"), ("approve_design", "approve_build", "apply_primary_diff")),
    "g2a": (("approve_build", "reject_build", "route_defect", "request_human_review"), ("produce_sandbox_diff", "apply_primary_diff")),
    "refine_bug": (("produce_bug_repair", "run_allowed_tests"), ("approve_design", "approve_build", "apply_primary_diff")),
    "refine_ui": (("produce_ui_repair", "run_allowed_tests"), ("approve_design", "approve_build", "apply_primary_diff")),
}
_EXPECTED_BINDINGS = {
    "torq-v5-6-live": {
        "g1d": ("anthropic", "claude-fable-5", "live.g1d.design", "effort.high", "agent_sdk"),
        "g1r": ("anthropic", "claude-opus-4-8", "live.g1r.review", "effort.high", "agent_sdk"),
        "builder": ("deepseek", "deepseek-v4-pro", "live.builder.execute", "effort.high", "direct_api"),
        "g2a": ("openai", "gpt-5.5-thinking", "live.g2a.audit", "effort.high", "codex_sdk"),
        "refine_bug": ("moonshot", "kimi-k3", "live.refine_bug.repair", "effort.standard", "direct_api"),
        "refine_ui": ("zai", "glm-5.2", "live.refine_ui.repair", "effort.high", "direct_api"),
    },
    "torq-v5-repo-compat": {
        "g1d": ("anthropic", "claude-opus-4-8", "compat.gate1_design", "effort.high", "agent_sdk"),
        "g1r": ("anthropic", "claude-opus-4-7", "compat.gate1_review", "effort.high", "agent_sdk"),
        "builder": ("deepseek", "deepseek-v4-pro", "compat.builder", "effort.high", "direct_api"),
        "g2a": ("anthropic", "claude-opus-4-8", "compat.g2a_adversarial", "effort.high", "agent_sdk"),
        "refine_bug": ("moonshot", "kimi-k2.7-code", "compat.refine_bug", "effort.standard", "direct_api"),
        "refine_ui": ("zai", "glm-5.2", "compat.refine_ui", "effort.high", "direct_api"),
    },
}
_EXPECTED_RULES = (
    ("g1r", "g1d", "model"), ("g2a", "builder", "vendor"),
    ("g2a", "refine_bug", "vendor"), ("g2a", "refine_ui", "vendor"),
)
_EXPECTED_EFFORTS = (
    ("effort.standard", "standard"),
    ("effort.high", "high"),
    ("effort.maximum", "maximum"),
)
_EXPECTED_STRATEGIES = {
    "light_v1": ("g1d", "g1r", "builder", "g2a"),
    "standard_v1": ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"),
}
_EXPECTED_PROFILE_METADATA = {
    "torq-v5-6-live": ("1.0.0", True, "standard_v1"),
    "torq-v5-repo-compat": ("1.0.0", False, "standard_v1"),
}
_EXPECTED_ESCALATIONS = (
    (
        "builder.sol-high", "builder", "openai", "gpt-5.6-sol-high",
        (("operator_flag", "builder_escalation"), ("task_label", "cross_stack"),
         ("task_label", "product_critical"), ("task_label", "major_workflow")),
    ),
    (
        "refine_ui.terra-high", "refine_ui", "openai", "gpt-5.6-terra-high",
        (("operator_flag", "refine_ui_escalation"), ("task_label", "customer_facing_ui"),
         ("task_label", "design_system")),
    ),
    (
        "g1d.fable-5b", "g1d", "anthropic", "claude-fable-5b",
        (("operator_flag", "g1d_promotion"), ("task_label", "promoted_g1d")),
    ),
)
_EXPECTED_PROMOTION_FIELDS = (
    "incumbent_model", "candidate_model", "fixture_hash", "score", "decision_id", "decided_at_utc",
)
_EXPECTED_DRIFT_ORACLE = {
    "oracle_id": "torq-console-compat@3ae196102a84aed24f7daa9dc3fed037522e1f20",
    "source_commit": "3ae196102a84aed24f7daa9dc3fed037522e1f20",
    "fixture_directory": "oracles/torq-console-3ae19610",
    "refresh_mode": "operator_gated_external_capture",
    "runtime_access": "fixture_only",
    "manifest_schema": "torq-oracle-manifest-v1",
}


class RegistryResourceMissing(FileNotFoundError):
    """The packaged registry resource is absent."""


class RegistryUnreadable(OSError):
    """The packaged registry resource cannot be read."""


class RegistrySyntaxError(ValueError):
    """The packaged registry is not valid YAML."""


class RegistryDocumentError(ValueError):
    """The packaged registry document has invalid top-level shape."""


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    policy_version: str
    contract_sha256: str


@dataclass(frozen=True)
class BindingSpec:
    role_id: str
    provider_id: str
    model_id: str
    prompt_id: str
    prompt_version: str
    effort_id: str
    connector_surface: str


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    allowed_authority: tuple[str, ...]
    prohibited_authority: tuple[str, ...]


@dataclass(frozen=True)
class IndependenceRuleSpec:
    reviewer_role: str
    reviewed_role: str
    minimum: str


@dataclass(frozen=True)
class EscalationSpec:
    escalation_id: str
    target_role: str
    provider_id: str
    model_id: str
    triggers: tuple[tuple[str, str], ...]
    promotion_id: str


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    profile_version: str
    default: bool
    strategy_id: str
    policy_id: str
    bindings: Mapping[str, BindingSpec]
    independence_rules: tuple[IndependenceRuleSpec, ...]
    escalations: tuple[EscalationSpec, ...]
    promotion_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bindings", deep_freeze(self.bindings))
        object.__setattr__(self, "independence_rules", tuple(self.independence_rules))
        object.__setattr__(self, "escalations", tuple(self.escalations))


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    prompt_version: str
    resource_path: str
    content_sha256: str


@dataclass(frozen=True)
class Registry:
    registry_schema_version: str
    registry_id: str
    registry_version: str
    policy: PolicySpec
    prompts: Mapping[str, PromptSpec]
    efforts: tuple[str, ...]
    roles: Mapping[str, RoleSpec]
    states: tuple[str, ...]
    transitions: tuple[tuple[str, str, str, str], ...]
    severity_buckets: Mapping[str, str]
    strategies: Mapping[str, tuple[str, ...]]
    profiles: Mapping[str, ProfileSpec]
    promotion_procedures: tuple[Mapping[str, Any], ...]
    drift_oracles: tuple[Mapping[str, Any], ...]
    resource_sha256: str
    raw_document: Mapping[str, Any] = field(default_factory=dict)
    duplicate_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("prompts", "roles", "severity_buckets", "strategies", "profiles", "raw_document"):
            object.__setattr__(self, name, deep_freeze(getattr(self, name)))
        object.__setattr__(self, "efforts", tuple(self.efforts))
        object.__setattr__(self, "states", tuple(self.states))
        object.__setattr__(self, "transitions", tuple(tuple(item) for item in self.transitions))
        object.__setattr__(self, "promotion_procedures", tuple(deep_freeze(item) for item in self.promotion_procedures))
        object.__setattr__(self, "drift_oracles", tuple(deep_freeze(item) for item in self.drift_oracles))
        object.__setattr__(self, "duplicate_identities", tuple(self.duplicate_identities))


def _resource_bytes(relative: str) -> bytes:
    return resources.files("torq_cli").joinpath("data", "registry", "v1", relative).read_bytes()


def _resource_text(relative: str) -> str:
    return _resource_bytes(relative).decode("utf-8")


def _binding(raw: Mapping[str, Any]) -> BindingSpec:
    return BindingSpec(
        role_id=str(raw["role_id"]), provider_id=str(raw["provider_id"]),
        model_id=str(raw["model_id"]), prompt_id=str(raw["prompt_id"]),
        prompt_version=str(raw["prompt_version"]), effort_id=str(raw["effort_id"]),
        connector_surface=str(raw["connector_surface"]),
    )


def _profile(raw: Mapping[str, Any]) -> ProfileSpec:
    bindings = {_b["role_id"]: _binding(_b) for _b in raw["bindings"]}
    rules = tuple(IndependenceRuleSpec(**rule) for rule in raw["independence_rules"])
    escalations = tuple(
        EscalationSpec(
            escalation_id=str(item["escalation_id"]), target_role=str(item["target_role"]),
            provider_id=str(item["provider_id"]), model_id=str(item["model_id"]),
            triggers=tuple((str(t["kind"]), str(t["value"])) for t in item["triggers"]),
            promotion_id=str(item["promotion_id"]),
        )
        for item in raw["escalations"]
    )
    return ProfileSpec(
        profile_id=str(raw["profile_id"]), profile_version=str(raw["profile_version"]),
        default=raw["default"], strategy_id=str(raw["strategy_id"]),
        policy_id=str(raw["policy_id"]), bindings=bindings,
        independence_rules=rules, escalations=escalations,
        promotion_id=str(raw["promotion_id"]),
    )


def load_registry() -> Registry:
    """Load only the packaged registry resource and retain its raw evidence."""
    try:
        raw_bytes = _resource_bytes("registry.yaml")
    except FileNotFoundError as exc:
        raise RegistryResourceMissing() from exc
    except OSError as exc:
        raise RegistryUnreadable() from exc
    try:
        raw = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RegistrySyntaxError() from exc
    if not isinstance(raw, Mapping):
        raise RegistryDocumentError()
    try:
        policies = raw["policies"]
        prompts_raw = raw["prompts"]
        roles_raw = raw["roles"]
        profiles_raw = raw["profiles"]
        if (
            not isinstance(policies, list)
            or not isinstance(prompts_raw, list)
            or not isinstance(roles_raw, list)
            or not isinstance(profiles_raw, list)
        ):
            raise TypeError
        policy = PolicySpec(**policies[0])
        prompts = {item["prompt_id"]: PromptSpec(**item) for item in prompts_raw}
        role_ids = [item["role_id"] for item in roles_raw]
        duplicate_role_ids = tuple(dict.fromkeys(str(role_id) for role_id in role_ids if role_ids.count(role_id) > 1))
        duplicate_binding_ids: list[str] = []
        for profile_raw in profiles_raw:
            binding_rows = profile_raw["bindings"]
            binding_ids = [item["role_id"] for item in binding_rows]
            duplicate_binding_ids.extend(
                f"{profile_raw['profile_id']}:{role_id}"
                for role_id in dict.fromkeys(str(role_id) for role_id in binding_ids)
                if binding_ids.count(role_id) > 1
            )
        roles = {
            item["role_id"]: RoleSpec(
                role_id=str(item["role_id"]),
                allowed_authority=tuple(item["allowed_authority"]),
                prohibited_authority=tuple(item["prohibited_authority"]),
            )
            for item in roles_raw
        }
        registry = Registry(
            registry_schema_version=str(raw["registry_schema_version"]),
            registry_id=str(raw["registry_id"]), registry_version=str(raw["registry_version"]),
            policy=policy, prompts=prompts,
            efforts=tuple(item["effort_id"] for item in raw["efforts"]), roles=roles,
            states=tuple(item["state_id"] for item in raw["states"]),
            transitions=tuple((item["from_state"], item["event"], item["to_state"], item["actor_id"]) for item in raw["transitions"]),
            severity_buckets={item["severity"]: item["bucket_id"] for item in raw["severity_buckets"]},
            strategies={item["strategy_id"]: tuple(item["stage_roles"]) for item in raw["strategies"]},
            profiles={item["profile_id"]: _profile(item) for item in profiles_raw},
            promotion_procedures=tuple(raw["promotion_procedures"]),
            drift_oracles=tuple(raw["drift_oracles"]),
            resource_sha256=hashlib.sha256(raw_bytes).hexdigest(), raw_document=raw,
            duplicate_identities=(*duplicate_role_ids, *duplicate_binding_ids),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RegistryDocumentError() from exc
    return registry


def validate_transition(edge: tuple[str, str, str, str]) -> bool:
    return edge in EXPECTED_TRANSITIONS


def validate_profile_version(registry: Registry, profile_version: str) -> bool:
    return any(p.profile_version == profile_version for p in registry.profiles.values())


def validate_binding_eligibility(binding: BindingSpec) -> bool:
    if binding.model_id.startswith("glm") and binding.role_id != "refine_ui":
        return False
    if binding.role_id == "builder" and binding.provider_id == "moonshot":
        return False
    return True


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _add(findings: list[str], finding: str) -> None:
    if finding not in findings:
        findings.append(finding)


def _closed_document(document: Mapping[str, Any]) -> bool:
    if set(document) != _ROOT_KEYS:
        return False
    nested = {
        "policies": {"policy_id", "policy_version", "contract_sha256"},
        "prompts": {"prompt_id", "prompt_version", "resource_path", "content_sha256"},
        "efforts": {"effort_id", "provider_value"},
        "roles": {"role_id", "allowed_authority", "prohibited_authority"},
        "states": {"state_id"},
        "transitions": {"from_state", "event", "to_state", "actor_id"},
        "severity_buckets": {"severity", "bucket_id"},
        "strategies": {"strategy_id", "stage_roles"},
        "profiles": {"profile_id", "profile_version", "default", "strategy_id", "policy_id", "bindings", "independence_rules", "escalations", "promotion_id"},
        "promotion_procedures": {"promotion_id", "benchmark_fixture_id", "required_record_fields"},
        "drift_oracles": {"oracle_id", "source_commit", "fixture_directory", "refresh_mode", "runtime_access", "manifest_schema"},
    }
    for name, keys in nested.items():
        value = document.get(name)
        if not isinstance(value, (list, tuple)):
            return False
        for item in value:
            if not isinstance(item, Mapping) or set(item) != keys:
                return False
    for profile in document.get("profiles", []):
        for binding in profile.get("bindings", []):
            if not isinstance(binding, Mapping) or set(binding) != {"role_id", "provider_id", "model_id", "prompt_id", "prompt_version", "effort_id", "connector_surface"}:
                return False
        for rule in profile.get("independence_rules", []):
            if not isinstance(rule, Mapping) or set(rule) != {"reviewer_role", "reviewed_role", "minimum"}:
                return False
        for escalation in profile.get("escalations", []):
            if not isinstance(escalation, Mapping) or set(escalation) != {"escalation_id", "target_role", "provider_id", "model_id", "promotion_id", "triggers"}:
                return False
            if any(not isinstance(trigger, Mapping) or set(trigger) != {"kind", "value"} for trigger in escalation.get("triggers", [])):
                return False
    return True


def _duplicates(values: list[Any]) -> bool:
    return len(values) != len({str(value) for value in values})


def validate_registry(registry: Registry, profile_version: str | None = None) -> tuple[str, ...]:
    findings: list[str] = []
    if not _closed_document(registry.raw_document):
        _add(findings, "registry_schema_invalid")
    if registry.duplicate_identities:
        _add(findings, "registry_duplicate_identity")
    raw_policies = registry.raw_document.get("policies", ())
    if not isinstance(raw_policies, (list, tuple)) or len(raw_policies) != 1:
        _add(findings, "registry_schema_invalid")
    raw_collections = (
        ("prompts", "prompt_id"), ("efforts", "effort_id"), ("roles", "role_id"),
        ("states", "state_id"), ("profiles", "profile_id"),
        ("strategies", "strategy_id"), ("severity_buckets", "severity"),
    )
    for collection, identity_key in raw_collections:
        raw_values = registry.raw_document.get(collection, ())
        if isinstance(raw_values, (list, tuple)) and _duplicates([item.get(identity_key) for item in raw_values if isinstance(item, Mapping)]):
            _add(findings, "registry_duplicate_identity")
    raw_profiles = registry.raw_document.get("profiles", ())
    if isinstance(raw_profiles, (list, tuple)):
        for raw_profile in raw_profiles:
            if not isinstance(raw_profile, Mapping) or type(raw_profile.get("default")) is not bool:
                _add(findings, "registry_schema_invalid")
            raw_bindings = raw_profile.get("bindings", ()) if isinstance(raw_profile, Mapping) else ()
            binding_ids = [
                item.get("role_id") for item in raw_bindings
                if isinstance(item, Mapping)
            ] if isinstance(raw_bindings, (list, tuple)) else []
            if _duplicates(binding_ids):
                _add(findings, "registry_duplicate_identity")
            elif len(binding_ids) != len(ROLE_IDS) or set(str(item) for item in binding_ids) != set(ROLE_IDS):
                _add(findings, "registry_profile_invalid")
    if registry.registry_schema_version != "1.0.0" or registry.registry_id != "torq-cli-role-registry" or registry.registry_version != "1.0.0":
        _add(findings, "registry_version_unsupported")
    raw_efforts = registry.raw_document.get("efforts", ())
    effort_values = (
        tuple((str(item.get("effort_id")), str(item.get("provider_value"))) for item in raw_efforts)
        if isinstance(raw_efforts, (list, tuple)) and all(isinstance(item, Mapping) for item in raw_efforts)
        else ()
    )
    if (
        tuple(registry.efforts) != EFFORT_IDS
        or effort_values != _EXPECTED_EFFORTS
        or len(registry.prompts) != 12
        or set(registry.roles) != set(ROLE_IDS)
    ):
        _add(findings, "registry_schema_invalid")
    if registry.states != STATE_IDS or registry.transitions != EXPECTED_TRANSITIONS:
        _add(findings, "registry_transition_invalid")
    if len(registry.profiles) != 2 or set(registry.profiles) != set(PROFILE_IDS):
        _add(findings, "registry_profile_invalid")
    if not registry.strategies or not registry.promotion_procedures or not registry.drift_oracles:
        _add(findings, "registry_schema_invalid")
    if _duplicates(list(registry.prompts)) or _duplicates(list(registry.roles)) or _duplicates(list(registry.profiles)):
        _add(findings, "registry_duplicate_identity")
    for role_id, role in registry.roles.items():
        expected = _ROLE_AUTHORITY.get(role_id)
        if expected is None or role.allowed_authority != expected[0] or role.prohibited_authority != expected[1]:
            _add(findings, "registry_schema_invalid")
    if set(registry.severity_buckets.items()) != {("critical", "A"), ("high", "B"), ("medium", "C"), ("low", "D")}:
        _add(findings, "registry_schema_invalid")
    if any(not validate_binding_eligibility(binding) for profile in registry.profiles.values() for binding in profile.bindings.values()):
        _add(findings, "binding_ineligible")
    if profile_version is not None and not validate_profile_version(registry, profile_version):
        findings.append("profile_version_unknown")
    policy_value = {
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "routing_contract": "foundation-metadata-only",
    }
    if (
        registry.policy.policy_id != POLICY_ID
        or registry.policy.policy_version != POLICY_VERSION
        or registry.policy.contract_sha256 != POLICY_HASH
        or hashlib.sha256(_canonical_json(policy_value)).hexdigest() != POLICY_HASH
    ):
        _add(findings, "policy_contract_hash_mismatch")
    raw_prompt_identities = tuple(
        (
            str(item.get("prompt_id")), str(item.get("prompt_version")),
            str(item.get("resource_path")), str(item.get("content_sha256")),
        )
        for item in registry.raw_document.get("prompts", ())
        if isinstance(item, Mapping)
    )
    if raw_prompt_identities != _EXPECTED_PROMPTS:
        _add(findings, "registry_prompt_hash_mismatch")
    for prompt_id, prompt_version, resource_path, content_sha256 in _EXPECTED_PROMPTS:
        prompt = registry.prompts.get(prompt_id)
        if prompt is None or (
            prompt.prompt_version, prompt.resource_path, prompt.content_sha256
        ) != (prompt_version, resource_path, content_sha256):
            _add(findings, "registry_prompt_hash_mismatch")
        try:
            prompt_hash = hashlib.sha256(_resource_text(resource_path).encode("utf-8")).hexdigest()
        except (FileNotFoundError, OSError):
            prompt_hash = ""
        if prompt_hash != content_sha256:
            _add(findings, "registry_prompt_hash_mismatch")
    if dict(registry.strategies) != _EXPECTED_STRATEGIES:
        _add(findings, "registry_schema_invalid")
    for profile_id, profile in registry.profiles.items():
        expected_bindings = _EXPECTED_BINDINGS.get(profile_id, {})
        expected_metadata = _EXPECTED_PROFILE_METADATA.get(profile_id)
        if set(profile.bindings) != set(ROLE_IDS) or len(profile.independence_rules) != 4 or len(profile.escalations) != 3:
            _add(findings, "registry_profile_invalid")
        if expected_metadata is None or (
            profile.profile_version, profile.default, profile.strategy_id
        ) != expected_metadata:
            _add(findings, "registry_schema_invalid")
        if profile.strategy_id not in registry.strategies or profile.policy_id != registry.policy.policy_id or profile.promotion_id != "promotion-v1":
            _add(findings, "registry_schema_invalid")
        for role_id, binding in profile.bindings.items():
            binding_expected = expected_bindings.get(role_id)
            if binding_expected is None or (
                binding.provider_id, binding.model_id, binding.prompt_id,
                binding.effort_id, binding.connector_surface,
            ) != binding_expected:
                _add(findings, "registry_profile_invalid")
            if binding.prompt_id not in registry.prompts or binding.effort_id not in registry.efforts or binding.role_id not in registry.roles:
                _add(findings, "registry_schema_invalid")
            elif (
                registry.prompts[binding.prompt_id].prompt_version == "1.0.0"
                and binding.prompt_version != registry.prompts[binding.prompt_id].prompt_version
            ):
                _add(findings, "registry_schema_invalid")
        if tuple((rule.reviewer_role, rule.reviewed_role, rule.minimum) for rule in profile.independence_rules) != _EXPECTED_RULES:
            _add(findings, "registry_profile_invalid")
        if any(rule.reviewer_role not in registry.roles or rule.reviewed_role not in registry.roles for rule in profile.independence_rules):
            _add(findings, "registry_schema_invalid")
        if any(item.target_role not in registry.roles or item.promotion_id != "promotion-v1" for item in profile.escalations):
            _add(findings, "registry_schema_invalid")
        escalation_values = tuple(
            (
                item.escalation_id, item.target_role, item.provider_id, item.model_id,
                item.triggers,
            )
            for item in profile.escalations
        )
        expected_escalations = tuple(
            (item[0], item[1], item[2], item[3], item[4]) for item in _EXPECTED_ESCALATIONS
        )
        if escalation_values != expected_escalations:
            _add(findings, "registry_schema_invalid")
    if (
        len(registry.promotion_procedures) != 1
        or registry.promotion_procedures[0].get("promotion_id") != "promotion-v1"
        or registry.promotion_procedures[0].get("benchmark_fixture_id") != "model-promotion-fixture-v1"
        or tuple(registry.promotion_procedures[0].get("required_record_fields", ())) != _EXPECTED_PROMOTION_FIELDS
    ):
        _add(findings, "registry_schema_invalid")
    if len(registry.drift_oracles) != 1 or any(
        registry.drift_oracles[0].get(key) != value for key, value in _EXPECTED_DRIFT_ORACLE.items()
    ):
        _add(findings, "registry_schema_invalid")
    return tuple(findings)
