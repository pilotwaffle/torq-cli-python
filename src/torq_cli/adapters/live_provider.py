"""Explicit live stage dispatcher for governed, operator-authorized runs."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from torq_cli.adapters.owned_stream import ProcessEvent
from torq_cli.adapters.process import ExitObservation, OwnedProcess
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
_MAX_PROVIDER_OUTPUT_BYTES = 1_048_576
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
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
                "maxItems": 50,
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


class _ProcessOwner(Protocol):
    @property
    def output_closed(self) -> bool: ...

    @property
    def background_error(self) -> BaseException | None: ...

    def next_event(self, *, timeout: float) -> ProcessEvent | None: ...

    def poll(self) -> int | None: ...

    def wait(self, *, timeout: float = 5.0) -> ExitObservation: ...

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation: ...

    def close(self) -> ExitObservation: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _direct_https_opener() -> urllib.request.OpenerDirector:
    """Build a transport that ignores ambient proxy routing and redirects."""

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


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


def _usage_count(value: object, provider: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
        raise OrchestrationBlocked(f"live_provider_response_invalid:{provider}")
    return value


def _matches_contract(value: object, schema: Mapping[str, object]) -> bool:
    expected_type = schema.get("type")
    if expected_type == "string":
        if not isinstance(value, str):
            return False
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return False
        if "const" in schema and value != schema["const"]:
            return False
        allowed = schema.get("enum")
        return not isinstance(allowed, list) or value in allowed
    if expected_type == "array":
        if not isinstance(value, list):
            return False
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            return False
        item_schema = schema.get("items")
        return isinstance(item_schema, Mapping) and all(
            _matches_contract(item, item_schema) for item in value
        )
    if expected_type == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            return False
        if not set(required).issubset(value):
            return False
        if schema.get("additionalProperties") is False and not set(value).issubset(properties):
            return False
        return all(
            key in properties
            and isinstance(properties[key], Mapping)
            and _matches_contract(item, properties[key])
            for key, item in value.items()
        )
    return False


def _validated_role_object(value: object, provider: str, role: str) -> str:
    """Validate the provider result locally; remote schema compliance is not trusted."""

    canonical = _single_json_object(value, provider)
    decoded = json.loads(canonical)
    schema = _CONTRACTS.get(role)
    if schema is None or not _matches_contract(decoded, schema):
        raise OrchestrationBlocked(f"live_provider_response_invalid:{provider}")
    return canonical


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
        owner_factory: Callable[..., _ProcessOwner] = OwnedProcess,
        https_opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.vault = vault
        self.base_environment = dict(base_environment)
        self.timeout_seconds = timeout_seconds
        self.claude_binary = claude_binary
        self.runtime_directory = runtime_directory
        self.owner_factory = owner_factory
        self.https_opener = https_opener or _direct_https_opener()

    def _owned_provider_command(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        prompt: str,
        provider: str,
    ) -> tuple[int, str]:
        """Run one CLI inside the platform owner and retain bounded stdout only."""

        try:
            owner = self.owner_factory(
                tuple(command),
                env=dict(environment),
                cwd=self.runtime_directory or str(Path.cwd()),
                input_data=prompt.encode("utf-8"),
                event_capacity_bytes=_MAX_PROVIDER_OUTPUT_BYTES,
            )
        except Exception as exc:
            raise OrchestrationBlocked(f"live_provider_command_failed:{provider}") from exc
        stdout = bytearray()
        total = 0
        deadline = time.monotonic() + self.timeout_seconds
        failure: OrchestrationBlocked | None = None
        primary_error: BaseException | None = None
        returncode: int | None = None

        def stop_observation() -> ExitObservation | None:
            try:
                return owner.force_stop(timeout=5.0)
            except BaseException:
                return None

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stopped = stop_observation()
                    if stopped is None or not stopped.confirmed:
                        failure = OrchestrationBlocked(
                            f"live_provider_termination_unconfirmed:{provider}"
                        )
                    else:
                        failure = OrchestrationBlocked(
                            f"live_provider_command_failed:{provider}"
                        )
                    break
                event = owner.next_event(timeout=min(0.05, remaining))
                if event is not None:
                    if event.channel == "system" or total + len(event.data) > (
                        _MAX_PROVIDER_OUTPUT_BYTES
                    ):
                        stopped = stop_observation()
                        if stopped is None or not stopped.confirmed:
                            failure = OrchestrationBlocked(
                                f"live_provider_termination_unconfirmed:{provider}"
                            )
                        else:
                            failure = OrchestrationBlocked(
                                f"live_provider_response_too_large:{provider}"
                            )
                        break
                    total += len(event.data)
                    if event.channel == "stdout":
                        stdout.extend(event.data)
                if owner.poll() is not None and owner.output_closed:
                    observation = owner.wait(timeout=max(0.001, remaining))
                    if not observation.confirmed:
                        stopped = stop_observation()
                        if stopped is not None:
                            observation = stopped
                    if not observation.confirmed:
                        failure = OrchestrationBlocked(
                            f"live_provider_termination_unconfirmed:{provider}"
                        )
                    elif owner.background_error is not None:
                        failure = OrchestrationBlocked(
                            f"live_provider_command_failed:{provider}"
                        )
                    returncode = observation.returncode
                    if returncode is None:
                        failure = OrchestrationBlocked(
                            f"live_provider_command_failed:{provider}"
                        )
                    break
        except BaseException as exc:
            primary_error = exc
            stop_observation()
        try:
            close_observation = owner.close()
        except BaseException:
            close_observation = None
        if close_observation is None or not close_observation.confirmed:
            raise OrchestrationBlocked(f"live_provider_termination_unconfirmed:{provider}")
        if primary_error is not None:
            if isinstance(primary_error, OrchestrationBlocked):
                raise primary_error
            if not isinstance(primary_error, Exception):
                raise primary_error
            raise OrchestrationBlocked(
                f"live_provider_command_failed:{provider}"
            ) from primary_error
        if failure is not None:
            raise failure
        final_returncode = (
            returncode if returncode is not None else close_observation.returncode
        )
        if final_returncode is None:
            raise OrchestrationBlocked(f"live_provider_command_failed:{provider}")
        return final_returncode, stdout.decode("utf-8", errors="replace")

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
        returncode, stdout = self._owned_provider_command(
            (
                self.claude_binary, "-p", "--output-format", "json", "--model", model,
                "--json-schema", json.dumps(schema, separators=(",", ":")),
                "--tools", "", "--no-session-persistence", "--permission-mode", "plan",
                "--disable-slash-commands", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            ),
            environment=environment,
            prompt=prompt,
            provider=provider_name,
        )
        if returncode != 0:
            raise OrchestrationBlocked(f"live_provider_command_failed:{provider_name}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OrchestrationBlocked(f"live_provider_response_invalid:{provider_name}") from exc
        if not isinstance(payload, Mapping):
            raise OrchestrationBlocked(f"live_provider_response_invalid:{provider_name}")
        result = payload.get("structured_output", payload.get("result"))
        model_usage = payload.get("modelUsage")
        resolved = set(model_usage) if isinstance(model_usage, Mapping) else set()
        if not isinstance(result, (str, Mapping)) or resolved != {model}:
            raise OrchestrationBlocked(f"live_model_unattested:{provider_name}")
        usage = payload.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        return NormalizedResponse(
            _validated_role_object(result, provider_name, role),
            "",
            {
                "prompt_tokens": _usage_count(usage_map.get("input_tokens", 0), provider_name),
                "completion_tokens": _usage_count(
                    usage_map.get("output_tokens", 0), provider_name
                ),
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
            _OPENAI_RESPONSES_URL,
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
            with self.https_opener.open(request, timeout=self.timeout_seconds) as response:
                if response.geturl() != _OPENAI_RESPONSES_URL:
                    raise ValueError("live_provider_redirect_refused")
                encoded = response.read(_MAX_PROVIDER_OUTPUT_BYTES + 1)
                if len(encoded) > _MAX_PROVIDER_OUTPUT_BYTES:
                    raise OrchestrationBlocked("live_provider_response_too_large:openai")
                payload = json.loads(encoded.decode("utf-8"))
        except OrchestrationBlocked:
            raise
        except (OSError, ValueError) as exc:
            raise OrchestrationBlocked("live_provider_command_failed:openai") from exc
        if not isinstance(payload, Mapping):
            raise OrchestrationBlocked("live_provider_response_invalid:openai")
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
            _validated_role_object(text, "openai", role),
            "",
            {
                "prompt_tokens": _usage_count(usage_map.get("input_tokens", 0), "openai"),
                "completion_tokens": _usage_count(
                    usage_map.get("output_tokens", 0), "openai"
                ),
                "reasoning_tokens": _usage_count(
                    detail_map.get("reasoning_tokens", 0), "openai"
                ),
            },
            Provenance("openai", model, False),
        )


__all__ = ["LiveStageDispatcher", "current_environment", "provider_binary_path"]
