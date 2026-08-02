"""Validated production wiring for installed governed live runs."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from torq_cli.adapters.live_provider import (
    LiveStageDispatcher,
    current_environment,
    provider_binary_path,
)
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.connectors.credential_sources import (
    CredentialSourceError,
    CredentialVault,
    claude_compatible_environment,
    credential_vault_from_config,
    openai_compatible_environment,
)
from torq_cli.core.policy import G2APolicy
from torq_cli.domain.config_schema import parse_config_bytes, validate_config
from torq_cli.domain.registry_schema import ProfileSpec, load_registry
from torq_cli.safety.accounting_registry import PersistentEntitlementLedger
from torq_cli.safety.entitlements import InMemoryEntitlementLedger
from torq_cli.safety.receipts import restrict_owner_only_directory

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


def _isolated_runtime_directory(forbidden_roots: tuple[Path, ...]) -> Path:
    """Create an empty, owner-only cwd outside the operator's target tree."""
    directory = Path(tempfile.mkdtemp(prefix="torq-live-"))
    try:
        resolved = directory.resolve(strict=True)
        if any(resolved.is_relative_to(root.resolve()) for root in forbidden_roots):
            raise ValueError("live_runtime_directory_unsafe")
        restrict_owner_only_directory(directory)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, ValueError) and str(exc) == "live_runtime_directory_unsafe":
            raise
        raise ValueError("live_runtime_directory_unsafe") from exc
    atexit.register(shutil.rmtree, directory, True)
    return directory


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
    expected_profile_version: str | None = None,
    expected_policy_version: str | None = None,
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
    if not isinstance(config_version, int) or isinstance(config_version, bool):
        raise ValueError("live_config_version_invalid")
    if expected_config_version is not None and config_version != expected_config_version:
        raise ValueError("config_version_mismatch")
    profile_value = config.get("profile")
    if not isinstance(profile_value, Mapping):
        raise ValueError("live_profile_invalid")
    profile_id = profile_value.get("id")
    if not isinstance(profile_id, str):
        raise ValueError("live_profile_invalid")
    profile = registry.profiles[profile_id]
    if (
        expected_profile_version is not None
        and profile.profile_version != expected_profile_version
    ):
        raise ValueError("profile_version_mismatch")
    if (
        expected_policy_version is not None
        and G2APolicy.version != expected_policy_version
    ):
        raise ValueError("policy_version_mismatch")

    environment = dict(current_environment() if base_environment is None else base_environment)
    vault = credential_vault_from_config(config)
    _required_credentials(profile, vault, environment)
    claude_binary = provider_binary_path(environment)
    if claude_binary is None:
        raise ValueError("live_provider_binary_missing:claude")

    entitlements = config.get("entitlement_accounts")
    if not isinstance(entitlements, Mapping):
        raise ValueError("entitlement_accounts_required")
    # Validate every window in memory first. Persistent construction creates the
    # accounting signing identity, so no fallible config parsing may follow it.
    validated_entitlements = InMemoryEntitlementLedger.from_config(entitlements)
    for window in validated_entitlements.windows.values():
        if window.used != 0 or window.reserved != 0:
            raise ValueError("entitlement_baseline_unsupported")
    for provider in sorted({binding.provider_id for binding in profile.bindings.values()}):
        expected_settlement = "metered" if provider == "openai" else "plan_covered"
        if validated_entitlements.window(provider).settlement != expected_settlement:
            raise ValueError(f"entitlement_settlement_mismatch:{provider}")
    policy = config.get("policy")
    if not isinstance(policy, Mapping):
        raise ValueError("live_policy_invalid")
    limits = policy.get("resource_limits")
    if not isinstance(limits, Mapping):
        raise ValueError("live_policy_invalid")
    max_cost_cents = limits.get("max_cost_cents")
    loop_budget = policy.get("loop_budget")
    if not isinstance(max_cost_cents, int) or isinstance(max_cost_cents, bool):
        raise ValueError("live_policy_invalid")
    if not isinstance(loop_budget, int) or isinstance(loop_budget, bool):
        raise ValueError("live_policy_invalid")
    budget_usd = max_cost_cents / 100
    runtime_directory = _isolated_runtime_directory(
        (config_path.parent, run_root, Path.cwd())
    )
    # Construct every remaining fallible runtime dependency before the durable
    # ledger creates its signing identity under ``run_root``.
    orchestrator = GovernedOrchestrator(
        LiveStageDispatcher(
            vault,
            environment,
            claude_binary=claude_binary,
            runtime_directory=str(runtime_directory),
        ),
        loop_budget=loop_budget,
        budget_usd=budget_usd,
        # OpenAI Responses is the only metered lane in the packaged profile.
        cost_ceiling_usd_by_role={"g2a": budget_usd},
        entitlement_ledger=validated_entitlements,
    )
    ledger = PersistentEntitlementLedger(run_root, validated_entitlements.windows)
    orchestrator.entitlement_ledger = ledger
    return LiveRuntime(orchestrator, profile, config_version)


__all__ = ["LiveRuntime", "build_live_runtime"]
