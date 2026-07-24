"""Idempotent guided-setup domain service."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from torq_cli.connectors.credential_sources import ExplicitEnvVault
from torq_cli.domain.config_schema import validate_credential_ref
from torq_cli.domain.registry_schema import load_registry


class SetupError(ValueError):
    pass


_REQUIRED_ROLES = {"g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"}
_ELIGIBILITY = {"builder": {"deepseek", "codex"}, "refine_bug": {"kimi"}, "refine_ui": {"zai", "codex"}}
_REGISTRY_PROVIDER = {
    "claude": "anthropic",
    "codex": "openai",
    "deepseek": "deepseek",
    "kimi": "moonshot",
    "zai": "zai",
}


class SetupService:
    def configure(self, target: Path, answers: Mapping[str, Any]) -> dict[str, Any]:
        bindings = answers.get("bindings")
        grants = answers.get("grants")
        if not isinstance(bindings, Mapping) or set(bindings) != _REQUIRED_ROLES:
            missing = sorted(_REQUIRED_ROLES - set(bindings or {}))
            raise SetupError("required_agent_unbound:" + ",".join(missing))
        if not isinstance(grants, Mapping):
            raise SetupError("provider_grants_missing")
        for role, raw in bindings.items():
            if not isinstance(raw, Mapping):
                raise SetupError(f"binding_invalid:{role}")
            provider, model = raw.get("provider"), raw.get("model")
            eligible = _ELIGIBILITY.get(str(role))
            if eligible is not None and provider not in eligible:
                constraint = "ru_only" if provider == "zai" else f"{role}_provider_constraint"
                raise SetupError(f"eligibility:{constraint}")
            provider_grants = grants.get(provider, ())
            if not isinstance(provider_grants, list) or model not in provider_grants:
                raise SetupError(f"model_not_granted:{model}")
        policy = answers.get("policy")
        if not isinstance(policy, Mapping) or policy.get("independence_mode") not in {"profile_minimum", "vendor_strict"}:
            raise SetupError("policy_invalid:independence_mode")
        credential_source: dict[str, str] | None = None
        raw_credential_file = answers.get("credential_file")
        raw_credential_refs = answers.get("credential_refs")
        if raw_credential_file is not None and raw_credential_refs is not None:
            raise SetupError("credential_source_ambiguous")
        if raw_credential_file is not None:
            source = Path(str(raw_credential_file))
            vault = ExplicitEnvVault(source)
            direct_providers = {
                str(raw.get("provider"))
                for raw in bindings.values()
                if isinstance(raw, Mapping) and raw.get("provider") in {"deepseek", "kimi", "zai"}
            }
            missing = sorted(provider for provider in direct_providers if vault.get(provider) is None)
            if missing:
                raise SetupError("provider_credential_missing:" + ",".join(missing))
            credential_source = {"kind": "external_env", "path": str(source)}
        credential_refs: dict[str, str] = {}
        if raw_credential_refs is not None:
            if not isinstance(raw_credential_refs, Mapping):
                raise SetupError("credential_refs_invalid")
            for provider, raw_ref in raw_credential_refs.items():
                if not isinstance(provider, str) or validate_credential_ref(
                    raw_ref, "/credential_refs"
                ) is not None:
                    raise SetupError("credential_refs_invalid")
                credential_refs[provider] = str(raw_ref)
            bound_providers = {
                str(raw.get("provider"))
                for raw in bindings.values()
                if isinstance(raw, Mapping)
            }
            if set(credential_refs) - bound_providers:
                raise SetupError("credential_refs_invalid")
            direct_providers = {
                str(raw.get("provider"))
                for raw in bindings.values()
                if isinstance(raw, Mapping) and raw.get("provider") in {"deepseek", "kimi", "zai"}
            }
            missing = sorted(direct_providers - set(credential_refs))
            if missing:
                raise SetupError("provider_credential_ref_missing:" + ",".join(missing))
            credential_source = {"kind": "platform_keychain"}
        registry = load_registry()
        profile_id = str(answers.get("profile"))
        profile_version = str(answers.get("profile_version"))
        profile = registry.profiles.get(profile_id)
        if profile is None or profile.profile_version != profile_version:
            raise SetupError("profile_unknown")
        overrides: dict[str, dict[str, object]] = {}
        connectors: dict[str, dict[str, object]] = {}
        for role, raw in sorted(bindings.items()):
            assert isinstance(raw, Mapping)
            expected = profile.bindings[str(role)]
            provider = str(raw.get("provider"))
            model = str(raw.get("model"))
            if _REGISTRY_PROVIDER.get(provider) != expected.provider_id or model != expected.model_id:
                raise SetupError(f"binding_profile_mismatch:{role}")
            connector_id = str(role).replace("_", "-") + "-main"
            overrides[str(role)] = {"connector_id": connector_id, "enabled": True}
            connectors[connector_id] = {
                "provider_id": expected.provider_id,
                "surface": expected.connector_surface,
                "enabled": True,
            }
            if provider in credential_refs:
                connectors[connector_id]["credential_ref"] = credential_refs[provider]
        ceilings = policy.get("ceilings")
        if not isinstance(ceilings, Mapping):
            raise SetupError("policy_invalid:ceilings")
        cost_usd = ceilings.get("cost_usd")
        if not isinstance(cost_usd, (int, float)) or isinstance(cost_usd, bool):
            raise SetupError("policy_invalid:cost_usd")
        document: dict[str, Any] = {
            "config_version": int(answers.get("config_version", 1)),
            "profile": {"id": profile_id, "version": profile_version},
            "binding_overrides": overrides,
            "connectors": connectors,
            "policy": {
                "independence_mode": policy["independence_mode"],
                "unattestable_action": "deny",
                "loop_budget": policy.get("loop_budget"),
                "resource_limits": {
                    "max_runtime_seconds": ceilings.get("runtime_seconds"),
                    "max_cost_cents": round(float(cost_usd) * 100),
                    "max_file_count": ceilings.get("file_count"),
                    "max_changed_lines": ceilings.get("changed_lines"),
                },
            },
        }
        if credential_source is not None:
            document["credential_source"] = credential_source
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(document, sort_keys=True)
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            target.write_text(rendered, encoding="utf-8")
        return document
