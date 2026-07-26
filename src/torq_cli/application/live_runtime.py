"""Validated production wiring for installed governed live runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from torq_cli.adapters.live_provider import (
    LiveStageDispatcher,
    current_environment,
    provider_binary_available,
)
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.connectors.credential_sources import (
    CredentialSourceError,
    CredentialVault,
    claude_compatible_environment,
    credential_vault_from_config,
    openai_compatible_environment,
)
from torq_cli.domain.config_schema import parse_config_bytes, validate_config
from torq_cli.domain.registry_schema import ProfileSpec, load_registry
from torq_cli.safety.accounting_registry import PersistentEntitlementLedger
from torq_cli.safety.entitlements import InMemoryEntitlementLedger


_DIRECT_CREDENTIAL_NAMES = {
    "deepseek": "deepseek",
    "openai": "codex",
    "moonshot": "kimi",
    "zai": "zai",
}


@dataclass(frozen=True)
class LiveRuntime:
    """Fully preflighted objects needed by ``torq run --live``."""

    orchestrator: GovernedOrchestrator
    profile: ProfileSpec
    config_version: int


def _required_credentials(
    profile: ProfileSpec,
    vault: CredentialVault,
    base_environment: Mapping[str, str],
) -> None:
    missing = sorted(
        provider
        for provider in {binding.provider_id for binding in profile.bindings.values()}
        if provider in _DIRECT_CREDENTIAL_NAMES
        and vault.get(_DIRECT_CREDENTIAL_NAMES[provider]) is None
    )
    if missing:
        raise CredentialSourceError("provider_credential_missing:" + ",".join(missing))
    # Exercise route construction before the evidence root exists. This catches
    # an invalid Token Plan regional override and inaccessible native secrets
    # at preflight rather than after ``run_attested`` has been written.
    for provider in sorted({binding.provider_id for binding in profile.bindings.values()}):
        credential_name = _DIRECT_CREDENTIAL_NAMES.get(provider)
        if credential_name == "codex":
            openai_compatible_environment(credential_name, vault, base_environment)
        elif credential_name is not None:
            claude_compatible_environment(credential_name, vault, base_environment)


def build_live_runtime(
    config_path: Path,
    run_root: Path,
    *,
    base_environment: Mapping[str, str] | None = None,
    expected_config_version: int | None = None,
) -> LiveRuntime:
    """Build a live runtime only after all non-mutating preflight checks pass."""
    try:
        config = parse_config_bytes(config_path.read_bytes())
    except OSError as exc:
        raise ValueError("live_config_unreadable") from exc
    registry = load_registry()
    findings = validate_config(config, registry)
    if findings:
        raise ValueError("live_config_invalid:" + findings[0].id)
    config_version = config.get("config_version")
    assert isinstance(config_version, int) and not isinstance(config_version, bool)
    if expected_config_version is not None and config_version != expected_config_version:
        raise ValueError("config_version_mismatch")
    profile_value = config.get("profile")
    assert isinstance(profile_value, Mapping)
    profile_id = profile_value.get("id")
    assert isinstance(profile_id, str)
    profile = registry.profiles[profile_id]

    environment = dict(current_environment() if base_environment is None else base_environment)
    vault = credential_vault_from_config(config)
    _required_credentials(profile, vault, environment)
    if not provider_binary_available(environment):
        raise ValueError("live_provider_binary_missing:claude")

    entitlements = config.get("entitlement_accounts")
    if not isinstance(entitlements, Mapping):
        raise ValueError("entitlement_accounts_required")
    # Validate every window in memory first. Persistent construction creates the
    # accounting signing identity, so no fallible config parsing may follow it.
    validated_entitlements = InMemoryEntitlementLedger.from_config(entitlements)
    ledger = PersistentEntitlementLedger(run_root, validated_entitlements.windows)
    policy = config.get("policy")
    assert isinstance(policy, Mapping)
    limits = policy.get("resource_limits")
    assert isinstance(limits, Mapping)
    max_cost_cents = limits.get("max_cost_cents")
    loop_budget = policy.get("loop_budget")
    assert isinstance(max_cost_cents, int) and not isinstance(max_cost_cents, bool)
    assert isinstance(loop_budget, int) and not isinstance(loop_budget, bool)
    budget_usd = max_cost_cents / 100
    orchestrator = GovernedOrchestrator(
        LiveStageDispatcher(vault, environment),
        loop_budget=loop_budget,
        budget_usd=budget_usd,
        # OpenAI Responses is the only metered lane in the packaged profile.
        cost_ceiling_usd_by_role={"g2a": budget_usd},
        entitlement_ledger=ledger,
    )
    return LiveRuntime(orchestrator, profile, config_version)


__all__ = ["LiveRuntime", "build_live_runtime"]
