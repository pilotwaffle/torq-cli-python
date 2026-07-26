"""Explicit live stage dispatcher for governed, operator-authorized runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.connectors.credential_sources import (
    CredentialVault,
    claude_compatible_environment,
    safe_child_environment,
)
from torq_cli.core.engine import NormalizedResponse, Provenance


_PROVIDER_NAMES = {
    "anthropic": "claude",
    "deepseek": "deepseek",
    "moonshot": "kimi",
    "qwen": "qwen",
    "zai": "zai",
}
_STRING = {"type": "string", "maxLength": 120}
_CONTRACTS: Mapping[str, Mapping[str, object]] = {
    "g1d": {
        "type": "object",
        "properties": {"status": {"type": "string", "const": "design_complete"}, "proposal": _STRING},
        "required": ["status", "proposal"],
        "additionalProperties": False,
    },
    "g1r": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "rationale": _STRING,
        },
        "required": ["verdict", "rationale"],
        "additionalProperties": False,
    },
    "builder": {
        "type": "object",
        "properties": {"status": {"type": "string", "const": "build_complete"}, "proposal": _STRING},
        "required": ["status", "proposal"],
        "additionalProperties": False,
    },
    "g2a": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "defects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "defect_id": _STRING,
                        "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                        "class": {"type": "string", "enum": ["bug", "ui", "security", "other"]},
                        "status": {"type": "string", "const": "open"},
                    },
                    "required": ["defect_id", "severity", "class", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verdict", "defects"],
        "additionalProperties": False,
    },
    "refine_bug": {
        "type": "object",
        "properties": {"status": {"type": "string", "const": "repair_complete"}, "proposal": _STRING},
        "required": ["status", "proposal"],
        "additionalProperties": False,
    },
    "refine_ui": {
        "type": "object",
        "properties": {"status": {"type": "string", "const": "repair_complete"}, "proposal": _STRING},
        "required": ["status", "proposal"],
        "additionalProperties": False,
    },
}


def current_environment() -> Mapping[str, str]:
    """Read ambient process state only at the isolated production adapter."""
    return dict(os.environ)


def provider_binary_path(base_environment: Mapping[str, str]) -> str | None:
    """Resolve the governed transport once so dispatch cannot re-search PATH."""
    candidate = shutil.which("claude", path=base_environment.get("PATH"))
    if candidate is None:
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_absolute() or not resolved.is_file():
        return None
    return str(resolved)


def _single_json_object(value: object, provider: str) -> str:
    """Canonicalize one provider object while rejecting ambiguous output."""
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if not isinstance(value, str):
        raise OrchestrationBlocked(f"live_provider_response_invalid:{provider}")
    text = value
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1:-3].strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise OrchestrationBlocked(f"live_provider_response_invalid:{provider}") from exc
    if not isinstance(value, Mapping):
        raise OrchestrationBlocked(f"live_provider_response_invalid:{provider}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class LiveStageDispatcher:
    """Dispatch profile-bound stages without exposing ambient credentials."""

    def __init__(
        self,
        vault: CredentialVault,
        base_environment: Mapping[str, str],
        *,
        timeout_seconds: int = 90,
        claude_binary: str = "claude",
        runtime_directory: str | None = None,
    ) -> None:
        self.vault = vault
        self.base_environment = dict(base_environment)
        self.timeout_seconds = timeout_seconds
        self.claude_binary = claude_binary
        self.runtime_directory = runtime_directory

    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        schema = _CONTRACTS.get(role)
        if schema is None:
            raise OrchestrationBlocked(f"live_role_unsupported:{role}")
        governed_prompt = (
            f"{prompt}\nMake an independent evidence-based decision and populate the requested "
            "structured output. Do not use tools, access files, "
            "execute commands, or claim to have changed the target."
        )
        if provider == "openai":
            return self._openai(role, model, governed_prompt, schema)
        provider_name = _PROVIDER_NAMES.get(provider)
        if provider_name is None:
            raise OrchestrationBlocked(f"live_provider_unsupported:{provider}")
        return self._claude_compatible(
            provider_name, provider, role, model, governed_prompt, schema
        )

    def _claude_compatible(
        self,
        provider_name: str,
        attested_provider: str,
        role: str,
        model: str,
        prompt: str,
        schema: Mapping[str, object],
    ) -> NormalizedResponse:
        environment = (
            safe_child_environment(self.base_environment)
            if provider_name == "claude"
            else claude_compatible_environment(
                provider_name, self.vault, self.base_environment
            )
        )
        completed = subprocess.run(
            (
                self.claude_binary, "-p", prompt, "--output-format", "json", "--model", model,
                "--json-schema", json.dumps(schema, separators=(",", ":")),
                "--tools", "", "--no-session-persistence", "--permission-mode", "plan",
                "--disable-slash-commands", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            ),
            env=environment,
            cwd=self.runtime_directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise OrchestrationBlocked(f"live_provider_command_failed:{provider_name}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OrchestrationBlocked(f"live_provider_response_invalid:{provider_name}") from exc
        result = payload.get("structured_output", payload.get("result"))
        model_usage = payload.get("modelUsage")
        resolved = set(model_usage) if isinstance(model_usage, Mapping) else set()
        if not isinstance(result, (str, Mapping)) or resolved != {model}:
            raise OrchestrationBlocked(f"live_model_unattested:{provider_name}")
        usage = payload.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        return NormalizedResponse(
            _single_json_object(result, provider_name),
            "",
            {
                "prompt_tokens": int(usage_map.get("input_tokens", 0)),
                "completion_tokens": int(usage_map.get("output_tokens", 0)),
                "reasoning_tokens": 0,
            },
            Provenance(attested_provider, model, False),
        )

    def _openai(
        self,
        role: str,
        model: str,
        prompt: str,
        schema: Mapping[str, object],
    ) -> NormalizedResponse:
        credential = self.vault.get("codex")
        if credential is None:
            raise OrchestrationBlocked("live_credential_missing:openai")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps({
                "model": model,
                "input": prompt,
                "max_output_tokens": 256,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": f"torq_{role}",
                        "strict": True,
                        "schema": schema,
                    }
                },
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise OrchestrationBlocked("live_provider_command_failed:openai") from exc
        resolved = payload.get("model")
        if resolved != model:
            raise OrchestrationBlocked("live_model_unattested:openai")
        text = "".join(
            str(content.get("text", ""))
            for item in payload.get("output", ())
            if isinstance(item, Mapping)
            for content in item.get("content", ())
            if isinstance(content, Mapping)
        ).strip()
        usage = payload.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        details = usage_map.get("output_tokens_details")
        detail_map = details if isinstance(details, Mapping) else {}
        if not text:
            raise OrchestrationBlocked(f"live_provider_response_invalid:{role}")
        return NormalizedResponse(
            _single_json_object(text, role),
            "",
            {
                "prompt_tokens": int(usage_map.get("input_tokens", 0)),
                "completion_tokens": int(usage_map.get("output_tokens", 0)),
                "reasoning_tokens": int(detail_map.get("reasoning_tokens", 0)),
            },
            Provenance("openai", model, False),
        )


__all__ = ["LiveStageDispatcher", "current_environment", "provider_binary_path"]
