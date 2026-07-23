"""Application boundary for bounded raw Console V5 YAML import."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from torq_cli.domain.drift_oracle import load_v5_config_reference
from torq_cli.domain.findings import Finding, FindingCatalog
from torq_cli.domain.hermetic import (
    LegacyConfigTooLarge,
    LegacyConfigUnreadable,
    ProtectedPathError,
    read_bounded_legacy_config,
)
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
from torq_cli.domain.v5_config_import import (
    TARGET_CONFIG_UTF8,
    target_config,
    validate_legacy_mapping,
    validate_projection,
)
from torq_cli.domain.v5_console_import import parse_console_config


COMMAND = "config_import_v5_console"
TARGET_SHA256 = "63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e"


def _snapshot(registry: Registry | None, stage: str) -> ResolutionSnapshot:
    return ResolutionSnapshot(
        registry_id=registry.registry_id if registry is not None else None,
        registry_version=registry.registry_version if registry is not None else None,
        registry_resource_sha256=registry.resource_sha256 if registry is not None else None,
        config_path=None,
        config_version=1 if stage == "complete" else None,
        profile_id="torq-v5-repo-compat" if stage == "complete" else None,
        profile_version="1.0.0" if stage == "complete" else None,
        resolution_stage=stage,
    )


def _finding(finding_id: str, path: str) -> Finding:
    return FindingCatalog.make(finding_id, path=path)


def _failure(
    finding_id: str,
    stage: str,
    registry: Registry | None,
    path: str,
    status: str | None = None,
) -> ResultEnvelope:
    finding = _finding(finding_id, path)
    return ResultEnvelope("1.0.0", COMMAND, status or finding.status_class, _snapshot(registry, stage), (finding,), {})


def internal_error() -> ResultEnvelope:
    finding = _finding("internal_error", "/")
    return ResultEnvelope("1.0.0", COMMAND, "internal_error", None, (finding,), {})


def output_rejected() -> ResultEnvelope:
    finding = _finding("legacy_config_projection_invalid", "/target_config")
    return ResultEnvelope("1.0.0", COMMAND, "invalid", None, (finding,), {})


def _registry() -> tuple[Registry | None, ResultEnvelope | None]:
    try:
        registry = load_registry()
    except RegistryResourceMissing:
        return None, _failure("registry_resource_missing", "registry_read", None, "/registry")
    except RegistryUnreadable:
        return None, _failure("registry_unreadable", "registry_read", None, "/registry")
    except RegistrySyntaxError:
        return None, _failure("registry_syntax_invalid", "registry_parse", None, "/registry")
    except RegistryDocumentError:
        return None, _failure("registry_schema_invalid", "registry_validate", None, "/registry")
    findings = tuple(_finding(item, "/registry") for item in validate_registry(registry))
    if findings:
        status = "invalid" if any(item.status_class == "invalid" for item in findings) else "blocked"
        return None, ResultEnvelope("1.0.0", COMMAND, status, _snapshot(registry, "registry_validate"), findings, {})
    return registry, None


def import_v5_console_path(config_path: str) -> ResultEnvelope:
    """Import raw Console V5 YAML into the fixed repo-compat projection."""
    try:
        registry, failure = _registry()
        if failure is not None:
            return failure
        assert registry is not None

        reference_finding, reference = load_v5_config_reference()
        if reference_finding is not None:
            return _failure(reference_finding, "oracle_validate", registry, "/packaged_reference", "blocked")
        assert reference is not None

        try:
            raw = read_bounded_legacy_config(config_path)
        except ProtectedPathError:
            return _failure("protected_path_denied", "legacy_config_read", registry, "/legacy_config", "blocked")
        except LegacyConfigTooLarge:
            return _failure("legacy_config_schema_invalid", "legacy_config_validate", registry, "/legacy_config")
        except LegacyConfigUnreadable:
            return _failure("legacy_config_unreadable", "legacy_config_read", registry, "/legacy_config")

        parse_finding, normalized = parse_console_config(raw)
        if parse_finding is not None:
            stage = "legacy_config_parse" if parse_finding == "legacy_config_syntax_invalid" else "legacy_config_validate"
            return _failure(parse_finding, stage, registry, "/legacy_config")
        assert normalized is not None

        mapping_finding = validate_legacy_mapping(normalized, reference)
        if mapping_finding is not None:
            return _failure(mapping_finding, "legacy_config_map", registry, "/legacy_config/agents")

        projection = target_config()
        if not validate_projection(projection, registry):
            return _failure("legacy_config_projection_invalid", "legacy_config_project", registry, "/target_config")
        if len(TARGET_CONFIG_UTF8) != 1029 or hashlib.sha256(TARGET_CONFIG_UTF8).hexdigest() != TARGET_SHA256:
            return _failure("legacy_config_projection_invalid", "legacy_config_project", registry, "/target_config")
        data: Mapping[str, Any] = {
            "schema_valid": True,
            "declaratively_eligible": True,
            "runtime_effective": False,
            "runtime_state": "offline_unattested",
            "source_schema": "torq-console-v5-config-v5",
            "target_config_version": 1,
            "target_profile": {"id": "torq-v5-repo-compat", "version": "1.0.0"},
            "canonicalization": "registry_authoritative_lossy",
            "canonical_target_config_utf8": TARGET_CONFIG_UTF8.decode("utf-8"),
            "canonical_target_config_sha256": TARGET_SHA256,
        }
        return ResultEnvelope("1.0.0", COMMAND, "ok", _snapshot(registry, "complete"), (), data)
    except Exception:
        return internal_error()
