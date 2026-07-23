"""Provider-neutral engine contracts adapted from TORQ MMH."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, cast


class RetryClass(str, Enum):
    RETRYABLE = "retryable"
    FATAL = "fatal"


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retry_class(self) -> RetryClass:
        return RetryClass.RETRYABLE if self.status in {408, 409, 425, 429} or (self.status or 0) >= 500 else RetryClass.FATAL


class BudgetExceeded(RuntimeError):
    """Raised before or after a call would exceed its configured ceiling."""


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    model: str
    messages: Sequence[Mapping[str, str]]
    max_tokens: int = 1024


@dataclass(frozen=True)
class Provenance:
    provider: str
    model: str
    fallback_used: bool

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedResponse:
    visible_text: str
    reasoning_trace: str
    usage: Mapping[str, int]
    provenance: Provenance


_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def normalize_response(request: ProviderRequest, payload: Mapping[str, Any]) -> NormalizedResponse:
    choices = payload.get("choices") or [{}]
    message = choices[0].get("message") or {}
    visible = str(message.get("content") or "")
    reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
    inline = _THINK.findall(visible)
    if inline:
        reasoning = "\n".join(part for part in (reasoning, *inline) if part).strip()
        visible = _THINK.sub("", visible).strip()
    raw_usage = payload.get("usage") or {}
    usage = {
        "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
        "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
        "reasoning_tokens": int((raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or raw_usage.get("reasoning_tokens") or 0),
    }
    return NormalizedResponse(
        visible,
        reasoning,
        usage,
        Provenance(
            provider=str(payload.get("provider") or request.provider),
            model=str(payload.get("model") or request.model),
            fallback_used=bool(payload.get("provider_fallback", False)),
        ),
    )


@dataclass
class BudgetLedger:
    limit_usd: float
    spent_usd: float = 0.0

    def preflight(self, worst_case_usd: float) -> None:
        if worst_case_usd < 0 or self.spent_usd + worst_case_usd > self.limit_usd:
            raise BudgetExceeded("budget_preflight_blocked")

    def charge(self, actual_usd: float) -> None:
        self.spent_usd += actual_usd
        if self.spent_usd > self.limit_usd:
            raise BudgetExceeded("budget_exceeded")


T = TypeVar("T")


def call_with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return operation()
        except ProviderError as exc:
            if exc.retry_class is RetryClass.FATAL or attempt + 1 == attempts:
                raise
            sleep(base_delay * (2**attempt))
    raise AssertionError("unreachable")


class RunStore:
    """Small deterministic JSON store for run/stage metadata."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, run_id: str, metadata: Mapping[str, Any], stages: Sequence[Mapping[str, Any]]) -> Path:
        target = self.root / run_id / "run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"metadata": metadata, "stages": stages}, sort_keys=True), encoding="utf-8")
        return target

    def load(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads((self.root / run_id / "run.json").read_text(encoding="utf-8")))
