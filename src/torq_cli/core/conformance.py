"""Vendored, credential-free conformance oracles for extracted contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from torq_cli.core.engine import ProviderRequest, normalize_response

_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def mmh_reference_normalize(seat: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Frozen projection of MMH adapter 0a.1's observable normalization contract."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    visible = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    inline = _THINK.findall(visible)
    if inline:
        reasoning = (reasoning + "\n" + "\n".join(inline)).strip()
        visible = _THINK.sub("", visible).strip()
    usage = payload.get("usage") or {}
    return {
        "visible_text": visible,
        "reasoning_trace": reasoning,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens") or 0),
        },
        "provenance": {
            "provider": payload.get("provider") or seat["provider"],
            "model": payload.get("model") or seat["model"],
            "fallback_used": bool(payload.get("provider_fallback", False)),
        },
    }


def extracted_projection(seat: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    response = normalize_response(ProviderRequest(seat["provider"], seat["model"], ()), payload)
    return {
        "visible_text": response.visible_text,
        "reasoning_trace": response.reasoning_trace,
        "usage": dict(response.usage),
        "provenance": response.provenance.as_dict(),
    }


def assert_conformant(seat: Mapping[str, str], payload: Mapping[str, Any]) -> None:
    expected = mmh_reference_normalize(seat, payload)
    actual = extracted_projection(seat, payload)
    if actual != expected:
        raise AssertionError({"expected": expected, "actual": actual})

