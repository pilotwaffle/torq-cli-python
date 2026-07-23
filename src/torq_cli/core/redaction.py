"""Shared fail-closed redaction registry for egress and persistence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class RedactionBlocked(RuntimeError):
    def __init__(self, findings: tuple[str, ...]) -> None:
        super().__init__("redaction_blocked:" + ",".join(findings))
        self.findings = findings


@dataclass(frozen=True)
class Pattern:
    name: str
    expression: re.Pattern[str]
    block: bool


class PatternRegistry:
    def __init__(self) -> None:
        self._patterns: list[Pattern] = []

    @classmethod
    def default(cls) -> PatternRegistry:
        registry = cls()
        registry.add("PRIVATE_KEY", r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", block=True)
        registry.add("AWS_ACCESS_KEY", r"\bAKIA[0-9A-Z]{16}\b", block=True)
        registry.add("OPENAI_STYLE_KEY", r"\bsk-[A-Za-z0-9]{20,}\b", block=True)
        registry.add("KEY_ASSIGNMENT", r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]", block=False)
        registry.add("BEARER_TOKEN", r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}", block=False)
        return registry

    def add(self, name: str, expression: str, *, block: bool) -> None:
        self._patterns.append(Pattern(name, re.compile(expression), block))

    def scan(self, text: str) -> tuple[str, tuple[str, ...]]:
        blocked = tuple(sorted({p.name for p in self._patterns if p.block and p.expression.search(text)}))
        if blocked:
            raise RedactionBlocked(blocked)
        sanitized = text
        findings: list[str] = []
        for pattern in self._patterns:
            if pattern.block:
                continue
            sanitized, count = pattern.expression.subn(f"[REDACTED:{pattern.name}]", sanitized)
            if count:
                findings.append(pattern.name)
        return sanitized, tuple(sorted(set(findings)))


class SafeTransport:
    def __init__(self, registry: PatternRegistry, sender: Callable[[str], None]) -> None:
        self.registry = registry
        self.sender = sender

    def send(self, payload: str) -> tuple[str, ...]:
        clean, findings = self.registry.scan(payload)
        self.sender(clean)
        return findings


class SafePersistence:
    def __init__(self, registry: PatternRegistry) -> None:
        self.registry = registry

    def write_text(self, path: Path, payload: str) -> tuple[str, ...]:
        clean, findings = self.registry.scan(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(clean, encoding="utf-8")
        return findings

