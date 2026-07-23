"""Copy-sandbox workspace isolation with locks and path-layer protection."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class WorkspaceBusy(RuntimeError):
    pass


class PathAccessDenied(RuntimeError):
    pass


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return "sha256:" + digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(relative.encode())
            digest.update(b"\0SYMLINK\0")
            digest.update(os.readlink(path).encode())
            digest.update(b"\0")
            continue
        if not path.is_file():
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


class GuardedPaths:
    _PROTECTED_NAMES = {".env", ".git", ".ssh", ".aws", ".azure", ".config", "evidence", "keys"}

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, requested: Path) -> Path:
        candidate = requested.resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise PathAccessDenied("path_escape") from exc
        for part in relative.parts:
            if part.lower() in self._PROTECTED_NAMES or part.lower().startswith(".env."):
                raise PathAccessDenied(f"protected_path:{part}")
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink() and not current.resolve().is_relative_to(self.root):
                raise PathAccessDenied("symlink_escape")
        return candidate

    def read_text(self, requested: Path) -> str:
        return self.resolve(requested).read_text(encoding="utf-8")

    def write_text(self, requested: Path, content: str) -> None:
        target = self.resolve(requested)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@dataclass
class WorkspaceHandle:
    root: Path
    primary: Path
    pinned_tree_hash: str
    paths: GuardedPaths
    lock_path: Path
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self.lock_path.unlink(missing_ok=True)
            self._released = True

    def __enter__(self) -> WorkspaceHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class WorkspaceManager:
    def __init__(self, sandbox_root: Path, *, dirty_policy: str = "refuse") -> None:
        self.sandbox_root = sandbox_root
        self.dirty_policy = dirty_policy

    def create(self, primary: Path, run_id: str, *, dirty: bool) -> WorkspaceHandle:
        if dirty and self.dirty_policy == "refuse":
            raise ValueError("dirty_primary_refused")
        resolved_primary = primary.resolve()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(str(resolved_primary).encode()).hexdigest() + ".lock"
        lock_path = self.sandbox_root / lock_name
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError as exc:
            raise WorkspaceBusy("workspace_lock_busy") from exc
        destination = self.sandbox_root / run_id / "workspace"
        try:
            shutil.copytree(resolved_primary, destination, symlinks=True)
        except Exception:
            lock_path.unlink(missing_ok=True)
            raise
        return WorkspaceHandle(destination, resolved_primary, tree_hash(resolved_primary), GuardedPaths(destination), lock_path)
