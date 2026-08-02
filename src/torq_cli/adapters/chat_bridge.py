"""Minimal Anthropic-compatible streaming bridge executed as an owned child."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

_ALLOWED_MEDIA = frozenset(
    {
        "application/json",
        "application/pdf",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/markdown",
        "text/plain",
    }
)
_PROVIDER_ORIGINS = {
    "deepseek": (".maas.aliyuncs.com", "/apps/anthropic"),
    "qwen": (".maas.aliyuncs.com", "/apps/anthropic"),
    "kimi": ("api.kimi.com", "/coding"),
    "zai": ("api.z.ai", "/api/anthropic"),
}
_MAX_SSE_LINE_BYTES = 1_048_576
_MAX_SSE_TOTAL_BYTES = 16_777_216


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise urllib.error.URLError("chat_redirect_denied")


def _endpoint(provider: str, base_url: str) -> str:
    from urllib.parse import urlsplit

    policy = _PROVIDER_ORIGINS.get(provider)
    parsed = urlsplit(base_url)
    if (
        policy is None
        or parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or not parsed.hostname
    ):
        raise ValueError("chat_provider_endpoint_denied")
    host_policy, path_prefix = policy
    hostname = parsed.hostname.casefold()
    host_allowed = (
        hostname.endswith(host_policy) if host_policy.startswith(".") else hostname == host_policy
    )
    normalized_path = parsed.path.rstrip("/")
    if not host_allowed or normalized_path != path_prefix:
        raise ValueError("chat_provider_endpoint_denied")
    return base_url.rstrip("/") + "/v1/messages"


def _content_blocks(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    text = request.get("text")
    attachments = request.get("attachments")
    if not isinstance(text, str) or not text.strip() or not isinstance(attachments, list):
        raise ValueError("chat_bridge_request_invalid")
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for item in attachments:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "media_type",
            "content_base64",
        }:
            raise ValueError("chat_bridge_attachment_invalid")
        name = item.get("name")
        media_type = item.get("media_type")
        encoded = item.get("content_base64")
        if (
            not isinstance(name, str)
            or not isinstance(media_type, str)
            or media_type not in _ALLOWED_MEDIA
            or not isinstance(encoded, str)
        ):
            raise ValueError("chat_bridge_attachment_invalid")
        source = {"type": "base64", "media_type": media_type, "data": encoded}
        if media_type.startswith("image/"):
            blocks.append({"type": "image", "source": source})
        elif media_type == "application/pdf":
            blocks.append({"type": "document", "source": source, "title": name})
        else:
            # Text-like inputs remain explicit attachments while avoiding a
            # second parser and filesystem materialization in the child.
            import base64

            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise ValueError("chat_bridge_attachment_invalid") from exc
            blocks.append({"type": "text", "text": f"\n--- attachment: {name} ---\n{decoded}"})
    return blocks


def main() -> int:
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    model = os.environ.get("TORQ_CHAT_MODEL", "")
    provider = os.environ.get("TORQ_CHAT_PROVIDER", "")
    if not token or not model:
        return 3
    try:
        raw = sys.stdin.buffer.read(42_000_001)
        if not raw or len(raw) > 42_000_000:
            return 3
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            return 3
        payload = {
            "model": model,
            "max_tokens": 8192,
            "stream": True,
            "messages": [{"role": "user", "content": _content_blocks(value)}],
        }
        endpoint = _endpoint(provider, base_url)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        usage_fields: set[str] = set()
        with opener.open(request, timeout=600) as response:
            total = 0
            while True:
                raw_line = response.readline(_MAX_SSE_LINE_BYTES + 1)
                if not raw_line:
                    break
                total += len(raw_line)
                if len(raw_line) > _MAX_SSE_LINE_BYTES or total > _MAX_SSE_TOTAL_BYTES:
                    return 4
                line = raw_line.decode("utf-8", errors="strict").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                event = json.loads(data)
                if not isinstance(event, Mapping):
                    continue
                usage_raw = event.get("usage")
                message = event.get("message")
                if not isinstance(usage_raw, Mapping) and isinstance(message, Mapping):
                    usage_raw = message.get("usage")
                if isinstance(usage_raw, Mapping):
                    aliases = {
                        "input_tokens": ("input_tokens", "prompt_tokens"),
                        "output_tokens": ("output_tokens", "completion_tokens"),
                        "reasoning_tokens": ("reasoning_tokens",),
                    }
                    for target, names in aliases.items():
                        for name in names:
                            value = usage_raw.get(name)
                            if (
                                isinstance(value, int)
                                and not isinstance(value, bool)
                                and value >= 0
                            ):
                                usage[target] = value
                                usage_fields.add(target)
                                break
                delta = event.get("delta")
                if isinstance(delta, Mapping) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        sys.stdout.write(text)
                        sys.stdout.flush()
                if event.get("type") == "error":
                    return 4
        if {"input_tokens", "output_tokens"}.issubset(usage_fields):
            sys.stderr.write(
                "TORQ_CHAT_USAGE\t"
                + json.dumps(usage, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            sys.stderr.flush()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return 4
    finally:
        token = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
