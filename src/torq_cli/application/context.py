"""Attended bridge from Fleet input to governed orchestration context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.safety.receipts import ReceiptWriter


class GovernedContextInjector:
    """Bind one active orchestrator and its authenticated receipt chain."""

    def __init__(self, orchestrator: GovernedOrchestrator, chain: ReceiptWriter) -> None:
        self.orchestrator = orchestrator
        self.chain = chain
        self._mutation_lock = RLock()

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
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        with self._mutation_lock:
            return self.orchestrator.inject_context(
                self.chain,
                content,
                target_role=target_role,
                media_type=media_type,
                source_name=source_name,
                confirm_direct=confirm_direct,
                command_id=command_id,
            )

    def inject_artifact(
        self,
        content: bytes,
        *,
        media_type: str,
        source_name: str,
        target_role: str | None = None,
        confirm_direct: bool = False,
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        with self._mutation_lock:
            return self.orchestrator.inject_artifact(
                self.chain,
                content,
                media_type=media_type,
                source_name=source_name,
                target_role=target_role,
                confirm_direct=confirm_direct,
                command_id=command_id,
            )

    def resolve_action(
        self,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        with self._mutation_lock:
            return self.orchestrator.resolve_action(
                self.chain,
                action_id=action_id,
                resolution=resolution,
                resolver_identity=resolver_identity,
            )


__all__ = ["GovernedContextInjector"]
