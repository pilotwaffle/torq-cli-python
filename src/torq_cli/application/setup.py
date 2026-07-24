"""Idempotent guided-setup domain service."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from torq_cli.connectors.credential_sources import ExplicitEnvVault


class SetupError(ValueError):
    pass


_REQUIRED_ROLES = {"g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"}
_ELIGIBILITY = {"builder": {"deepseek", "codex"}, "refine_bug": {"kimi"}, "refine_ui": {"zai", "codex"}}


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
        document = {
            "config_version": int(answers.get("config_version", 1)),
            "profile": {"id": str(answers.get("profile")), "version": str(answers.get("profile_version"))},
            "policy_version": str(answers.get("policy_version")),
            "bindings": {str(key): dict(value) for key, value in sorted(bindings.items())},
            "policy": dict(policy),
        }
        if credential_source is not None:
            document["credential_source"] = credential_source
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(document, sort_keys=True)
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            target.write_text(rendered, encoding="utf-8")
        return document
