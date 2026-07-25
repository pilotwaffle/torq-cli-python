"""Attended bridge from Fleet input to governed orchestration context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.safety.receipts import ReceiptWriter


class GovernedContextInjector:
    """Bind one active orchestrator and its authenticated receipt chain."""

    def __init__(self, orchestrator: GovernedOrchestrator, chain: ReceiptWriter) -> None:
        self.orchestrator = orchestrator
        self.chain = chain

    @property
    def root(self) -> Path:
        return self.chain.root

    def inject(
        self,
        content: str,
        *,
        target_role: str | None = None,
        media_type: str = "text/plain",
        source_name: str | None = None,
        confirm_direct: bool = False,
    ) -> Mapping[str, Any]:
        return self.orchestrator.inject_context(
            self.chain,
            content,
            target_role=target_role,
            media_type=media_type,
            source_name=source_name,
            confirm_direct=confirm_direct,
        )

    def inject_artifact(
        self,
        content: bytes,
        *,
        media_type: str,
        source_name: str,
        target_role: str | None = None,
        confirm_direct: bool = False,
    ) -> Mapping[str, Any]:
        return self.orchestrator.inject_artifact(
            self.chain,
            content,
            media_type=media_type,
            source_name=source_name,
            target_role=target_role,
            confirm_direct=confirm_direct,
        )


__all__ = ["GovernedContextInjector"]
