"""Ordered, immutable offline resolution."""

from __future__ import annotations

from typing import Any, Mapping

import yaml

from torq_cli.application.offline_status import offline_status_data
from torq_cli.domain.config_schema import ConfigSyntaxError, parse_config_bytes, parse_config_text, resolve_profile, validate_config_shape, validate_eligibility
from torq_cli.domain.drift_oracle import load_packaged_oracle, validate_oracle
from torq_cli.domain.findings import Finding, FindingCatalog
from torq_cli.domain.hermetic import LegacyConfigTooLarge, LegacyConfigUnreadable, ProtectedPathError, ReadOnlyConfigReader
from torq_cli.domain.models import ResolutionSnapshot, ResultEnvelope
from torq_cli.domain.registry_schema import (
    Registry,
    RegistryDocumentError,
    RegistryResourceMissing,
    RegistrySyntaxError,
    RegistryUnreadable,
    load_registry,
    validate_registry,
)


_STAGES = {
    "registry_read": 0, "registry_parse": 1, "registry_validate": 2,
    "config_read": 3, "config_parse": 4, "config_validate": 5,
    "profile_resolve": 6, "eligibility": 7, "oracle_validate": 8, "complete": 9,
}


def _snapshot(
    path: str | None,
    stage: str,
    registry: Registry | None = None,
    config: Mapping[str, Any] | None = None,
) -> ResolutionSnapshot:
    profile = config.get("profile", {}) if isinstance(config, Mapping) else {}
    return ResolutionSnapshot(
        registry_id=registry.registry_id if registry is not None else None,
        registry_version=registry.registry_version if registry is not None else None,
        registry_resource_sha256=registry.resource_sha256 if registry is not None else None,
        config_path=path,
        config_version=config.get("config_version") if isinstance(config, Mapping) and type(config.get("config_version")) is int else None,
        profile_id=profile.get("id") if isinstance(profile, Mapping) and type(profile.get("id")) is str else None,
        profile_version=profile.get("version") if isinstance(profile, Mapping) and type(profile.get("version")) is str else None,
        resolution_stage=stage,
    )


def _finding(finding_id: str, path: str = "/") -> Finding:
    return FindingCatalog.make(finding_id, path=path)


def _ordered(findings: list[Finding]) -> tuple[Finding, ...]:
    unique: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.id, finding.path)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return tuple(sorted(unique, key=lambda finding: (_STAGES.get(finding.stage, 99), finding.path, finding.id)))


def _status(findings: tuple[Finding, ...], default: str = "ok") -> str:
    classes = {finding.status_class for finding in findings}
    if "internal_error" in classes:
        return "internal_error"
    if "invalid" in classes:
        return "invalid"
    if "blocked" in classes:
        return "blocked"
    if "unattested" in classes:
        return "unattested"
    return default


def _failure(
    command: str,
    snapshot: ResolutionSnapshot | None,
    finding_id: str,
    status: str | None = None,
) -> ResultEnvelope:
    finding = _finding(finding_id)
    return ResultEnvelope(
        "1.0.0", command, status or finding.status_class, snapshot, (finding,), {}
    )


def config_read_failure(
    command: str,
    config_path: str,
    registry: Registry,
    finding_id: str = "config_unreadable",
) -> ResultEnvelope:
    """Return a config-read finding using the already validated registry."""
    snapshot = _snapshot(config_path, "config_read", registry)
    finding = _finding(finding_id)
    return ResultEnvelope("1.0.0", command, finding.status_class, snapshot, (finding,), {})


def _independence_findings(config: Mapping[str, Any], registry: Registry) -> list[Finding]:
    policy = config.get("policy")
    profile_raw = config.get("profile")
    if not isinstance(policy, Mapping) or policy.get("independence_mode") != "vendor_strict":
        return []
    if not isinstance(profile_raw, Mapping):
        return []
    profile_id = profile_raw.get("id")
    if type(profile_id) is not str:
        return []
    profile = registry.profiles.get(profile_id)
    if profile is None:
        return []
    findings: list[Finding] = []
    for rule in profile.independence_rules:
        reviewer = profile.bindings.get(rule.reviewer_role)
        reviewed = profile.bindings.get(rule.reviewed_role)
        if reviewer is not None and reviewed is not None and reviewer.provider_id == reviewed.provider_id:
            findings.append(FindingCatalog.make("independence_unsatisfied", path="/policy/independence_mode"))
    return findings


def _validated_registry(command: str, config_path: str) -> tuple[Registry | None, ResultEnvelope | None]:
    try:
        registry = load_registry()
    except RegistryResourceMissing:
        return None, _failure(command, _snapshot(config_path, "registry_read"), "registry_resource_missing", "invalid")
    except RegistryUnreadable:
        return None, _failure(command, _snapshot(config_path, "registry_read"), "registry_unreadable", "invalid")
    except RegistrySyntaxError:
        return None, _failure(command, _snapshot(config_path, "registry_parse"), "registry_syntax_invalid", "invalid")
    except RegistryDocumentError:
        return None, _failure(command, _snapshot(config_path, "registry_validate"), "registry_schema_invalid", "invalid")
    registry_findings = _ordered([_finding(item) for item in validate_registry(registry)])
    if registry_findings:
        snapshot = _snapshot(config_path, "registry_validate", registry)
        return None, ResultEnvelope("1.0.0", command, _status(registry_findings), snapshot, registry_findings, {})
    return registry, None


def _invalid_data(config: Mapping[str, Any]) -> dict[str, Any]:
    profile = config.get("profile")
    profile_id = profile.get("id") if isinstance(profile, Mapping) and type(profile.get("id")) is str else None
    profile_version = profile.get("version") if isinstance(profile, Mapping) and type(profile.get("version")) is str else None
    return {
        "schema_valid": False,
        "declaratively_eligible": False,
        "runtime_effective": False,
        "profile_id": profile_id,
        "profile_version": profile_version,
    }


def _resolve_config_mapping(command: str, config: Mapping[str, Any], config_path: str, registry: Registry, require_effective: bool) -> ResultEnvelope:
    try:
        config_findings = _ordered(list(validate_config_shape(config, registry)))
        if config_findings:
            snapshot = _snapshot(config_path, "config_validate", registry, config)
            return ResultEnvelope("1.0.0", command, _status(config_findings), snapshot, config_findings, _invalid_data(config))
        profile_findings = _ordered(list(resolve_profile(config, registry)))
        if profile_findings:
            snapshot = _snapshot(config_path, "profile_resolve", registry, config)
            return ResultEnvelope("1.0.0", command, _status(profile_findings), snapshot, profile_findings, _invalid_data(config))
        eligibility_findings = _ordered([
            *validate_eligibility(config, registry),
            *_independence_findings(config, registry),
        ])
        if eligibility_findings:
            snapshot = _snapshot(config_path, "eligibility", registry, config)
            return ResultEnvelope("1.0.0", command, _status(eligibility_findings), snapshot, eligibility_findings, {})
        profile_raw = config["profile"]
        profile_id = profile_raw["id"]
        profile_version = profile_raw["version"]
        snapshot = _snapshot(config_path, "profile_resolve", registry, config)
        oracle_findings: list[Finding] = []
        if profile_id == "torq-v5-6-live":
            oracle_findings.append(_finding("oracle_scope_not_applicable"))
        else:
            snapshot = _snapshot(config_path, "oracle_validate", registry, config)
            try:
                oracle = load_packaged_oracle()
            except (FileNotFoundError, OSError):
                oracle = None
            except (UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError):
                oracle = None
                oracle_findings.append(_finding("oracle_fixture_schema_invalid"))
            if oracle is None:
                if not oracle_findings:
                    oracle_findings.append(_finding("oracle_fixture_missing"))
            elif not oracle.manifest_bytes:
                oracle_findings.append(_finding("oracle_fixture_missing"))
            else:
                oracle_findings.extend(_finding(item) for item in validate_oracle(oracle))
        ordered = _ordered(oracle_findings)
        status = _status(ordered)
        if any(finding.status_class in {"invalid", "blocked"} for finding in ordered):
            failure_data = offline_status_data(profile_id, profile_version) if command == "status_offline" else {}
            return ResultEnvelope("1.0.0", command, status, snapshot, ordered, failure_data)
        data: Mapping[str, Any] = {
            "schema_valid": True,
            "declaratively_eligible": status in {"ok", "unattested"},
            "runtime_effective": False,
            "profile_id": profile_id,
            "profile_version": profile_version,
        }
        if command == "status_offline":
            data = offline_status_data(profile_id, profile_version)
            if not ordered or status == "ok":
                ordered = _ordered([*ordered, _finding("runtime_unattested")])
            status = _status(ordered, "unattested")
        else:
            status = "ok" if status == "ok" else status
        snapshot = _snapshot(config_path, "complete", registry, config)
        return ResultEnvelope("1.0.0", command, status, snapshot, ordered, data)
    except Exception:
        return _failure(command, _snapshot(config_path, "config_validate", registry), "internal_error", "internal_error")


def _resolve_config_text(command: str, text: str, config_path: str, registry: Registry, require_effective: bool) -> ResultEnvelope:
    try:
        config = parse_config_text(text)
    except ConfigSyntaxError:
        return _failure(command, _snapshot(None, "config_parse", registry), "config_syntax_invalid", "invalid")
    except Exception:
        return _failure(command, _snapshot(None, "config_parse", registry), "internal_error", "internal_error")
    return _resolve_config_mapping(command, config, config_path, registry, require_effective)


def _resolve_config_bytes(command: str, payload: bytes, config_path: str, registry: Registry, require_effective: bool) -> ResultEnvelope:
    try:
        config = parse_config_bytes(payload)
    except ConfigSyntaxError:
        return _failure(command, _snapshot(None, "config_parse", registry), "config_syntax_invalid", "invalid")
    except Exception:
        return _failure(command, _snapshot(None, "config_parse", registry), "internal_error", "internal_error")
    return _resolve_config_mapping(command, config, config_path, registry, require_effective)


def resolve_text(command: str, text: str, config_path: str, require_effective: bool = False) -> ResultEnvelope:
    registry, failure = _validated_registry(command, config_path)
    if failure is not None:
        return failure
    assert registry is not None
    return _resolve_config_text(command, text, config_path, registry, require_effective)


def resolve_path(command: str, config_path: str, require_effective: bool = False) -> ResultEnvelope:
    registry, failure = _validated_registry(command, config_path)
    if failure is not None:
        return failure
    assert registry is not None
    try:
        text = ReadOnlyConfigReader().read_utf8(config_path)
    except ProtectedPathError:
        return config_read_failure(command, config_path, registry, "protected_path_denied")
    except LegacyConfigTooLarge:
        return _failure(command, _snapshot(None, "config_parse", registry), "config_syntax_invalid", "invalid")
    except LegacyConfigUnreadable:
        return config_read_failure(command, config_path, registry)
    except UnicodeDecodeError:
        return _failure(command, _snapshot(None, "config_parse", registry), "config_syntax_invalid", "invalid")
    except Exception:
        return _failure(command, _snapshot(None, "config_read", registry), "internal_error", "internal_error")
    return _resolve_config_text(command, text, config_path, registry, require_effective)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def envelope_to_dict(envelope: ResultEnvelope) -> dict[str, Any]:
    def finding_dict(finding: Finding) -> dict[str, Any]:
        return {
            "id": finding.id, "message": finding.message, "severity": finding.severity.value,
            "bucket": finding.bucket, "status_class": finding.status_class, "stage": finding.stage,
            "path": finding.path, "context": _plain(finding.context),
        }
    snapshot = None if envelope.snapshot is None else {
        "registry_id": envelope.snapshot.registry_id,
        "registry_version": envelope.snapshot.registry_version,
        "registry_resource_sha256": envelope.snapshot.registry_resource_sha256,
        "config_path": envelope.snapshot.config_path,
        "config_version": envelope.snapshot.config_version,
        "profile_id": envelope.snapshot.profile_id,
        "profile_version": envelope.snapshot.profile_version,
        "resolution_stage": envelope.snapshot.resolution_stage,
    }
    return {
        "schema_version": envelope.schema_version, "command": envelope.command,
        "status": envelope.status, "snapshot": snapshot,
        "findings": [finding_dict(finding) for finding in envelope.findings],
        "data": _plain(envelope.data),
    }
