"""Provider command construction for the owned interactive chat runtime."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from torq_cli.application.chat_runtime import ChatProviderCommand
from torq_cli.connectors.credential_sources import (
    CredentialVault,
    claude_compatible_environment,
    safe_child_environment,
)


class ChatProviderCommandFactory:
    """Build secret-safe argv/stdin for one explicitly selected provider lane."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        cwd: Path,
        base_environment: Mapping[str, str],
        vault: CredentialVault | None = None,
        claude_binary: str = "claude",
    ) -> None:
        if not model or len(model) > 128 or "\x00" in model:
            raise ValueError("chat_model_invalid")
        self.provider = provider.casefold()
        self.model = model
        self.cwd = cwd.resolve()
        self.base_environment = dict(base_environment)
        self.vault = vault
        self.claude_binary = claude_binary

    def __call__(
        self,
        turn_id: str,
        text: str,
        attachments: Sequence[Mapping[str, str]],
    ) -> ChatProviderCommand:
        del turn_id
        if self.provider == "claude":
            if attachments:
                raise ValueError("chat_subscription_attachments_unsupported")
            environment = safe_child_environment(self.base_environment)
            return ChatProviderCommand(
                (
                    self.claude_binary,
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
                ),
                str(self.cwd),
                environment,
                text.encode("utf-8"),
                self.provider,
                self.model,
                "plan_covered",
            )
        if self.provider not in {"deepseek", "kimi", "qwen", "zai"}:
            raise ValueError("chat_provider_unsupported")
        if self.vault is None:
            raise ValueError("chat_credential_source_required")
        environment = claude_compatible_environment(
            self.provider, self.vault, self.base_environment
        )
        environment.update(
            {
                "TORQ_CHAT_MODEL": self.model,
                "TORQ_CHAT_PROVIDER": self.provider,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        bridge = Path(__file__).with_name("chat_bridge.py").resolve()
        if bridge.is_symlink() or not bridge.is_file():
            raise ValueError("chat_bridge_untrusted")
        request = {
            "text": text,
            "attachments": [dict(item) for item in attachments],
        }
        return ChatProviderCommand(
            (sys.executable, "-I", "-S", str(bridge)),
            str(bridge.parent),
            environment,
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
            self.provider,
            self.model,
            "plan_covered",
        )


def current_environment() -> dict[str, str]:
    """Read ambient process facts only at the audited provider adapter boundary."""
    return dict(os.environ)


__all__ = ["ChatProviderCommandFactory", "current_environment"]
