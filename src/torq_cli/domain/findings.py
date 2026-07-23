"""Stable finding contracts used by every Foundation resolution stage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .models import deep_freeze


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Finding:
    id: str
    message: str
    severity: FindingSeverity
    bucket: str
    status_class: str
    stage: str
    path: str
    context: Mapping[str, str | int | bool | None]
    exit_code: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", deep_freeze(self.context))


@dataclass(frozen=True)
class _FindingSpec:
    message: str
    severity: FindingSeverity
    bucket: str
    status_class: str
    stage: str
    exit_code: int


def _spec(
    message: str,
    severity: FindingSeverity,
    bucket: str,
    status_class: str,
    stage: str | int,
    exit_code: int | str,
) -> _FindingSpec:
    if isinstance(stage, int) and isinstance(exit_code, str):
        stage, exit_code = exit_code, stage
    assert isinstance(stage, str)
    assert isinstance(exit_code, int)
    return _FindingSpec(message, severity, bucket, status_class, stage, exit_code)


_HIGH_INVALID = (FindingSeverity.HIGH, "B", "invalid", 2)
_MEDIUM_BLOCKED = (FindingSeverity.MEDIUM, "C", "blocked", 3)


CATALOG: dict[str, _FindingSpec] = {
    "registry_resource_missing": _spec("Packaged registry resource is missing.", *_HIGH_INVALID, "registry_read"),
    "registry_unreadable": _spec("Packaged registry cannot be read.", *_HIGH_INVALID, "registry_read"),
    "registry_syntax_invalid": _spec("Packaged registry syntax is invalid.", *_HIGH_INVALID, "registry_parse"),
    "registry_schema_invalid": _spec("Packaged registry violates its closed schema.", *_HIGH_INVALID, "registry_validate"),
    "registry_version_unsupported": _spec("Registry version is unsupported.", *_HIGH_INVALID, "registry_validate"),
    "registry_duplicate_identity": _spec("Registry contains a duplicate identity.", *_HIGH_INVALID, "registry_validate"),
    "registry_prompt_hash_mismatch": _spec("Packaged prompt identity hash does not match resource bytes.", *_HIGH_INVALID, "registry_validate"),
    "registry_transition_invalid": _spec("Registry contains an invalid transition edge.", *_HIGH_INVALID, "registry_validate"),
    "registry_profile_invalid": _spec("Registry profile violates required cardinality or reference rules.", *_HIGH_INVALID, "registry_validate"),
    "config_unreadable": _spec("Config file cannot be read.", *_HIGH_INVALID, "config_read"),
    "config_syntax_invalid": _spec("Config syntax is invalid.", *_HIGH_INVALID, "config_parse"),
    "config_schema_invalid": _spec("Config violates its closed schema.", *_HIGH_INVALID, "config_validate"),
    "config_version_missing": _spec("Config version is required.", *_HIGH_INVALID, "config_validate"),
    "config_version_unsupported": _spec("Config version is unsupported.", *_HIGH_INVALID, "config_validate"),
    "migration_required": _spec("Config requires explicit migration.", *_HIGH_INVALID, "config_validate"),
    "raw_credential_field_forbidden": _spec("Raw credential field is forbidden.", FindingSeverity.CRITICAL, "A", "invalid", "config_validate", 2),
    "credential_ref_invalid": _spec("Credential reference has invalid opaque syntax.", *_HIGH_INVALID, "config_validate"),
    "profile_unknown": _spec("Selected profile is unknown.", *_HIGH_INVALID, "profile_resolve"),
    "profile_version_unknown": _spec("Selected profile version is unknown.", *_HIGH_INVALID, "profile_resolve"),
    "binding_override_forbidden": _spec("Binding override changes protected profile routing.", *_MEDIUM_BLOCKED, "eligibility"),
    "connector_unknown": _spec("Selected connector is unavailable or disabled.", *_MEDIUM_BLOCKED, "eligibility"),
    "connector_provider_mismatch": _spec("Selected connector provider differs from packaged binding.", *_MEDIUM_BLOCKED, "eligibility"),
    "connector_surface_mismatch": _spec("Selected connector surface differs from packaged binding.", *_MEDIUM_BLOCKED, "eligibility"),
    "required_role_disabled": _spec("A required strategy or independence role is disabled.", *_MEDIUM_BLOCKED, "eligibility"),
    "binding_ineligible": _spec("Binding violates role eligibility rules.", *_MEDIUM_BLOCKED, "eligibility"),
    "independence_unsatisfied": _spec("Declarative independence requirement is unsatisfied.", *_MEDIUM_BLOCKED, "eligibility"),
    "oracle_fixture_missing": _spec("Packaged oracle fixture is missing.", FindingSeverity.HIGH, "B", "blocked", "oracle_validate", 3),
    "oracle_fixture_hash_mismatch": _spec("Packaged oracle fixture hash does not match manifest.", FindingSeverity.HIGH, "B", "blocked", "oracle_validate", 3),
    "oracle_fixture_schema_invalid": _spec("Packaged oracle fixture schema is invalid.", FindingSeverity.HIGH, "B", "blocked", "oracle_validate", 3),
    "oracle_model_mismatch_g1d": _spec("Compatibility fixture differs on G1D model.", *_MEDIUM_BLOCKED, "oracle_validate"),
    "oracle_model_mismatch_g2a": _spec("Compatibility fixture differs on G2A model.", *_MEDIUM_BLOCKED, "oracle_validate"),
    "oracle_prompt_mismatch_g2a": _spec("Compatibility fixture differs on G2A prompt.", *_MEDIUM_BLOCKED, "oracle_validate"),
    "oracle_prompt_path_missing_g2a_adversarial": _spec("Compatibility fixture records missing G2A adversarial prompt.", *_MEDIUM_BLOCKED, "oracle_validate"),
    "oracle_scope_not_applicable": _spec("Compatibility oracle does not apply to selected profile.", FindingSeverity.LOW, "D", "ok", "oracle_validate", 0),
    "runtime_unattested": _spec("Runtime provider state is unattested offline.", FindingSeverity.LOW, "D", "unattested", "complete", 4),
    "protected_path_denied": _spec("Protected path access is denied.", FindingSeverity.CRITICAL, "A", "blocked", "config_read", 3),
    "internal_error": _spec("Internal failure occurred without exposing details.", FindingSeverity.CRITICAL, "A", "internal_error", "complete", 5),
    "policy_contract_hash_mismatch": _spec("Policy contract hash does not match canonical policy bytes.", *_HIGH_INVALID, "registry_validate"),
    "oracle_manifest_trusted_hash_mismatch": _spec("Oracle manifest does not match trusted manifest bytes.", FindingSeverity.HIGH, "B", "blocked", "oracle_validate", 3),
    "legacy_config_unreadable": _spec("Normalized legacy config cannot be read.", *_HIGH_INVALID, "legacy_config_read"),
    "legacy_config_syntax_invalid": _spec("Normalized legacy config syntax is invalid.", *_HIGH_INVALID, "legacy_config_parse"),
    "legacy_config_schema_invalid": _spec("Normalized legacy config violates its closed schema.", *_HIGH_INVALID, "legacy_config_validate"),
    "legacy_config_secret_field_forbidden": _spec("Raw credential field is forbidden in normalized legacy config.", FindingSeverity.CRITICAL, "A", "invalid", "legacy_config_validate", 2),
    "legacy_config_role_duplicate": _spec("Normalized legacy config contains a duplicate role.", *_HIGH_INVALID, "legacy_config_validate"),
    "legacy_config_role_missing": _spec("Normalized legacy config has missing or unknown roles.", *_HIGH_INVALID, "legacy_config_validate"),
    "legacy_config_mapping_unsupported": _spec("Normalized legacy config does not match the authenticated compatibility reference.", *_HIGH_INVALID, "legacy_config_map"),
    "legacy_config_projection_invalid": _spec("Canonical target config cannot satisfy the closed CLI config contract.", *_HIGH_INVALID, "legacy_config_project"),
}


class FindingCatalog:
    """Factory for the closed, non-secret finding catalog."""

    @staticmethod
    def make(
        finding_id: str,
        *,
        path: str,
        context: Mapping[str, str | int | bool | None] | None = None,
    ) -> Finding:
        spec = CATALOG[finding_id]
        safe_context = dict(context or {})
        return Finding(
            id=finding_id,
            message=spec.message,
            severity=spec.severity,
            bucket=spec.bucket,
            status_class=spec.status_class,
            stage=spec.stage,
            path=path,
            context=safe_context,
            exit_code=spec.exit_code,
        )

    @staticmethod
    def ids() -> tuple[str, ...]:
        return tuple(CATALOG)
