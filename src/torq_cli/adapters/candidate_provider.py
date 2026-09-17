"""Closed command and wire contracts for goal-to-candidate providers."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from torq_cli.connectors.credential_sources import (
    CredentialVault,
    claude_compatible_environment,
    safe_child_environment,
)
from torq_cli.safety.task_workspace import digest_bytes, validate_relative_path

OUTPUT_CONTRACT = "torq-candidate-output-v1"
MAX_PROVIDER_OUTPUT = 1_048_576


@dataclass(frozen=True, slots=True)
class CandidateProviderCommand:
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    input_data: bytes
    provider: str
    model: str


def _native_executable(path: Path) -> str:
    original = path.absolute()
    original_metadata = original.stat(follow_symlinks=False)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if original.is_symlink() or int(getattr(original_metadata, "st_file_attributes", 0)) & reparse:
        raise ValueError("task_provider_binary_unsafe")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or resolved.is_symlink():
        raise ValueError("task_provider_binary_unsafe")
    if os.name == "nt" and resolved.suffix.casefold() not in {".exe", ".com"}:
        raise ValueError("task_provider_binary_not_native")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise ValueError("task_provider_binary_not_executable")
    return str(resolved)


class CandidateProviderCommandFactory:
    """Create immutable tools-off commands; provider output never selects argv."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        runtime_root: Path,
        base_environment: Mapping[str, str],
        vault: CredentialVault | None = None,
        claude_binary: Path | None = None,
    ) -> None:
        if not model or len(model) > 128 or "\x00" in model:
            raise ValueError("task_model_invalid")
        self.provider = provider.casefold()
        self.model = model
        self.runtime_root = runtime_root.resolve()
        self.base_environment = dict(base_environment)
        self.vault = vault
        self.claude_binary = claude_binary

    def preflight(self) -> dict[str, object]:
        if self.provider == "claude":
            if self.claude_binary is None:
                raise ValueError("task_claude_binary_required")
            binary = _native_executable(self.claude_binary)
        elif self.provider in {"deepseek", "kimi", "qwen", "zai"}:
            bridge = Path(__file__).with_name("chat_bridge.py")
            _native_executable(Path(sys.executable))
            if bridge.is_symlink() or not bridge.is_file():
                raise ValueError("task_provider_bridge_unsafe")
            if self.vault is None:
                raise ValueError("task_credential_source_required")
            # Resolve the credential now without disclosing it. Authentication is
            # still unknown until the provider accepts an actual request.
            claude_compatible_environment(self.provider, self.vault, self.base_environment)
            binary = str(Path(sys.executable).resolve())
        else:
            raise ValueError("task_provider_unsupported")
        return {"provider": self.provider, "model": self.model, "binary": binary, "authentication": "not_checked"}

    def __call__(self, prompt: str) -> CandidateProviderCommand:
        if len(prompt.encode("utf-8")) > 3_000_000:
            raise ValueError("task_provider_input_too_large")
        if self.provider == "claude":
            if self.claude_binary is None:
                raise ValueError("task_claude_binary_required")
            argv: tuple[str, ...] = (
                _native_executable(self.claude_binary),
                "-p",
                "--output-format",
                "text",
                "--model",
                self.model,
                "--tools",
                "",
                "--no-session-persistence",
                "--permission-mode",
                "plan",
                "--disable-slash-commands",
                "--safe-mode",
                "--no-chrome",
            )
            environment = safe_child_environment(self.base_environment)
            input_data = prompt.encode("utf-8")
        else:
            if self.vault is None:
                raise ValueError("task_credential_source_required")
            bridge = Path(__file__).with_name("chat_bridge.py").resolve()
            argv = (_native_executable(Path(sys.executable)), "-I", "-S", str(bridge))
            environment = claude_compatible_environment(
                self.provider, self.vault, self.base_environment
            )
            environment.update(
                {"TORQ_CHAT_MODEL": self.model, "TORQ_CHAT_PROVIDER": self.provider, "PYTHONIOENCODING": "utf-8"}
            )
            input_data = json.dumps(
                {"text": prompt, "attachments": []}, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        return CandidateProviderCommand(argv, str(self.runtime_root), environment, input_data, self.provider, self.model)


def _reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("task_provider_duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    del value
    raise ValueError("task_provider_non_finite")


def parse_candidate_output(
    raw: bytes,
    *,
    plan_hash: str,
    input_hash: str,
) -> tuple[tuple[dict[str, object], ...], str]:
    if not raw or len(raw) > MAX_PROVIDER_OUTPUT:
        raise ValueError("task_provider_output_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("task_provider_output_invalid") from exc
    if not isinstance(value, dict) or set(value) != {"contract", "plan_hash", "input_hash", "operations"}:
        raise ValueError("task_provider_output_schema_invalid")
    if value["contract"] != OUTPUT_CONTRACT or value["plan_hash"] != plan_hash or value["input_hash"] != input_hash:
        raise ValueError("task_provider_output_binding_invalid")
    operations = value["operations"]
    if not isinstance(operations, list):
        raise ValueError("task_provider_operations_invalid")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {"operation", "path", "base_hash", "content"}:
            raise ValueError("task_provider_operation_schema_invalid")
        path_value = operation["path"]
        if not isinstance(path_value, str):
            raise ValueError("task_provider_path_invalid")
        path = validate_relative_path(path_value)
        if path.casefold() in seen:
            raise ValueError("task_provider_path_duplicate")
        seen.add(path.casefold())
        if operation["operation"] not in {"create", "replace"}:
            raise ValueError("task_provider_operation_unsupported")
        if operation["base_hash"] is not None and not (
            isinstance(operation["base_hash"], str)
            and len(operation["base_hash"]) == 71
            and operation["base_hash"].startswith("sha256:")
        ):
            raise ValueError("task_provider_base_hash_invalid")
        if not isinstance(operation["content"], str):
            raise ValueError("task_provider_content_invalid")
        normalized.append(dict(operation))
    return tuple(normalized), digest_bytes(raw)


def trusted_python_executable() -> str:
    """Return the interpreter identity only at the audited adapter boundary."""
    return _native_executable(Path(sys.executable))


__all__ = [
    "CandidateProviderCommand",
    "CandidateProviderCommandFactory",
    "MAX_PROVIDER_OUTPUT",
    "OUTPUT_CONTRACT",
    "parse_candidate_output",
    "trusted_python_executable",
]
