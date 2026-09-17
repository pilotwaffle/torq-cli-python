from __future__ import annotations

from pathlib import Path

import pytest

from torq_cli.safety.primary_transaction import AnchoredPrimary
from torq_cli.safety.task_workspace import digest_bytes


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
