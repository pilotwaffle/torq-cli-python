"""Explicit, tree-pinned audited change application boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from torq_cli.safety.workspace import GuardedPaths, tree_hash


@dataclass(frozen=True)
class ChangeProposal:
    pinned_tree_hash: str
    files: Mapping[str, bytes]
    diff_hash: str

    @classmethod
    def create(cls, pinned_tree_hash: str, files: Mapping[str, bytes]) -> ChangeProposal:
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(content)
        return cls(pinned_tree_hash, dict(files), "sha256:" + digest.hexdigest())


class ApprovalBoundary:
    def __init__(self, primary: Path) -> None:
        self.primary = primary.resolve()
        self.paths = GuardedPaths(self.primary)

    def apply(self, proposal: ChangeProposal, *, approved_by: str | None) -> dict[str, str]:
        if not approved_by:
            raise PermissionError("explicit_approval_required")
        if tree_hash(self.primary) != proposal.pinned_tree_hash:
            raise ValueError("primary_tree_hash_mismatch: re-run or re-baseline required")
        check = ChangeProposal.create(proposal.pinned_tree_hash, proposal.files)
        if check.diff_hash != proposal.diff_hash:
            raise ValueError("audited_diff_hash_mismatch")
        for relative, content in proposal.files.items():
            target = self.paths.resolve(self.primary / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return {
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "pinned_tree_hash": proposal.pinned_tree_hash,
            "diff_hash": proposal.diff_hash,
        }

