from __future__ import annotations

import ctypes
import errno
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest

import torq_cli.safety.primary_transaction as primary_module
from torq_cli.safety.primary_transaction import AnchoredPrimary
from torq_cli.safety.task_workspace import digest_bytes


class _NativeFunction:
    def __init__(self, callback: Callable[..., Any]) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: Any) -> Any:
        return self.callback(*args)


class _DarwinLibc:
    def __init__(
        self,
        *,
        xattrs: bytes = b"",
        acl: bool = False,
        xattr_error: int | None = None,
        acl_error: int | None = None,
    ) -> None:
        def list_xattrs(
            _descriptor: Any, buffer: Any, size: Any, _options: Any
        ) -> int:
            if xattr_error is not None:
                ctypes.set_errno(xattr_error)
                return -1
            if buffer is None:
                return len(xattrs)
            if int(size) < len(xattrs):
                ctypes.set_errno(errno.ERANGE)
                return -1
            ctypes.memmove(buffer, xattrs, len(xattrs))
            return len(xattrs)

        def get_acl(_descriptor: Any, _kind: Any) -> int | None:
            if acl_error is not None:
                ctypes.set_errno(acl_error)
                return None
            if not acl:
                ctypes.set_errno(errno.ENOENT)
                return None
            return 1234

        def get_entry(*_args: Any) -> int:
            return 0

        self.flistxattr = _NativeFunction(list_xattrs)
        self.acl_get_fd_np = _NativeFunction(get_acl)
        self.acl_get_entry = _NativeFunction(get_entry)
        self.acl_free = _NativeFunction(lambda *_args: 0)


def test_darwin_fd_metadata_adapter_is_bounded_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_module, "_darwin_libc", lambda: _DarwinLibc())
    assert primary_module._darwin_xattrs(7) == []
    assert primary_module._darwin_acl_present(7) is False

    monkeypatch.setattr(
        primary_module, "_darwin_libc", lambda: _DarwinLibc(xattrs=b"user.note\x00")
    )
    assert primary_module._darwin_xattrs(7) == ["user.note"]
    monkeypatch.setattr(primary_module, "_darwin_libc", lambda: _DarwinLibc(acl=True))
    assert primary_module._darwin_acl_present(7) is True

    malformed = _DarwinLibc(xattrs=b"missing-terminator")
    monkeypatch.setattr(primary_module, "_darwin_libc", lambda: malformed)
    with pytest.raises(ValueError, match="task_apply_metadata_unsupported"):
        primary_module._darwin_xattrs(7)
    unavailable = _DarwinLibc(xattr_error=errno.ENOTSUP)
    monkeypatch.setattr(primary_module, "_darwin_libc", lambda: unavailable)
    with pytest.raises(RuntimeError, match="task_apply_metadata_unreadable"):
        primary_module._darwin_xattrs(7)
    acl_unavailable = _DarwinLibc(acl_error=errno.ENOTSUP)
    monkeypatch.setattr(primary_module, "_darwin_libc", lambda: acl_unavailable)
    with pytest.raises(RuntimeError, match="task_apply_metadata_unreadable"):
        primary_module._darwin_acl_present(7)


def test_anchored_primary_replaces_creates_and_restores_exact_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    target = nested / "value.py"
    target.write_bytes(b"value = 1\n")

    with AnchoredPrimary(project) as primary:
        before = primary.inspect("src/value.py")
        assert before.content == b"value = 1\n"
        with pytest.raises(RuntimeError, match="task_apply_primary_busy"):
            AnchoredPrimary(project)

        replaced = primary.replace(
            "src/value.py",
            expected_before_hash=before.content_hash,
            expected_parent_identity=before.parent_identity,
            expected_target_identity=before.target_identity,
            content=b"value = 2\n",
            permission_policy=before.permission_policy or {},
            application_id="apply-" + "1" * 24,
            operation_index=0,
        )
        assert replaced.content_hash == digest_bytes(b"value = 2\n")
        assert replaced.permission_policy == before.permission_policy

        absent = primary.inspect("src/new.py")
        created = primary.replace(
            "src/new.py",
            expected_before_hash=None,
            expected_parent_identity=absent.parent_identity,
            expected_target_identity=None,
            content=b"created = True\n",
            permission_policy=primary.create_permission_policy(),
            application_id="apply-" + "1" * 24,
            operation_index=1,
        )
        assert created.content == b"created = True\n"

        restored = primary.replace(
            "src/value.py",
            expected_before_hash=replaced.content_hash,
            expected_parent_identity=replaced.parent_identity,
            expected_target_identity=replaced.target_identity,
            content=before.content,
            permission_policy=before.permission_policy or {},
            application_id="apply-" + "1" * 24,
            operation_index=2,
        )
        assert restored.content_hash == before.content_hash
        assert restored.permission_policy == before.permission_policy
        assert created.target_identity is not None
        primary.remove(
            "src/new.py",
            expected_hash=created.content_hash or "",
            expected_parent_identity=created.parent_identity,
            expected_target_identity=created.target_identity,
        )

    assert target.read_bytes() == b"value = 1\n"
    assert not (nested / "new.py").exists()
    assert list(nested.glob(".torq-*.tmp")) == []


def test_anchored_primary_requires_existing_parent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with AnchoredPrimary(project) as primary:
        with pytest.raises(ValueError, match="task_apply_parent_missing"):
            primary.replace(
                "missing/value.py",
                expected_before_hash=None,
                expected_parent_identity={},
                expected_target_identity=None,
                content=b"value = 1\n",
                permission_policy=primary.create_permission_policy(),
                application_id="apply-" + "2" * 24,
                operation_index=0,
            )
