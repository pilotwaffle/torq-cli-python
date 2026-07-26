from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from torq_cli.adapters.chat_bridge import _content_blocks, _endpoint
from torq_cli.adapters.chat_provider import ChatProviderCommandFactory


class _Vault:
    def get(self, provider: str) -> str | None:
        assert provider == "deepseek"
        return "secret-plan-token"

    def base_url(self, provider: str) -> str | None:
        assert provider == "deepseek"
        return "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"


def test_plan_provider_secret_and_prompt_never_enter_argv(tmp_path: Path) -> None:
    factory = ChatProviderCommandFactory(
        provider="deepseek",
        model="deepseek-v4-pro",
        cwd=tmp_path,
        base_environment={"PATH": "safe", "UNRELATED_SECRET": "do-not-copy"},
        vault=_Vault(),
    )
    command = factory("turn-1", "private prompt", ())
    argv = " ".join(command.argv)
    assert "private prompt" not in argv
    assert "secret-plan-token" not in argv
    assert command.argv[1:3] == ("-I", "-S")
    assert command.argv[3].endswith("chat_bridge.py")
    assert Path(command.cwd) == Path(command.argv[3]).parent
    assert command.environment["ANTHROPIC_AUTH_TOKEN"] == "secret-plan-token"
    assert "UNRELATED_SECRET" not in command.environment
    request = json.loads(command.input_data or b"")
    assert request == {"attachments": [], "text": "private prompt"}


def test_subscription_provider_uses_stdin_and_rejects_unsupported_files(
    tmp_path: Path,
) -> None:
    factory = ChatProviderCommandFactory(
        provider="claude",
        model="claude-opus-4-8",
        cwd=tmp_path,
        base_environment={"PATH": "safe", "USERPROFILE": "profile"},
    )
    command = factory("turn-1", "private prompt", ())
    assert "private prompt" not in " ".join(command.argv)
    assert command.input_data == b"private prompt"
    with pytest.raises(ValueError, match="chat_subscription_attachments_unsupported"):
        factory(
            "turn-2",
            "inspect",
            (
                {
                    "name": "image.png",
                    "media_type": "image/png",
                    "content_base64": base64.b64encode(b"png").decode("ascii"),
                },
            ),
        )


def test_bridge_builds_multimodal_anthropic_content_without_paths() -> None:
    blocks = _content_blocks(
        {
            "text": "inspect these",
            "attachments": [
                {
                    "name": "screen.png",
                    "media_type": "image/png",
                    "content_base64": base64.b64encode(b"image").decode("ascii"),
                },
                {
                    "name": "brief.pdf",
                    "media_type": "application/pdf",
                    "content_base64": base64.b64encode(b"%PDF-test").decode("ascii"),
                },
                {
                    "name": "notes.txt",
                    "media_type": "text/plain",
                    "content_base64": base64.b64encode(b"safe notes").decode("ascii"),
                },
            ],
        }
    )
    assert [block["type"] for block in blocks] == ["text", "image", "document", "text"]
    assert "safe notes" in blocks[-1]["text"]


def test_bridge_endpoint_is_provider_pinned_and_rejects_credential_sinks() -> None:
    assert _endpoint(
        "deepseek",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
    ).endswith("/apps/anthropic/v1/messages")
    for target in (
        "https://attacker.invalid/apps/anthropic",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com.evil/apps/anthropic",
        "https://user@token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic?redirect=1",
    ):
        with pytest.raises(ValueError, match="chat_provider_endpoint_denied"):
            _endpoint("deepseek", target)
