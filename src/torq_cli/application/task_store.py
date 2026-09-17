"""Durable CAS drafts, immutable plans, and idempotent task reservations."""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.safety.receipts import (
    fsync_directory,
    restrict_owner_only_directory,
    restrict_owner_only_file,
    signing_file_permissions_are_restricted,
)
from torq_cli.safety.task_workspace import canonical_json, digest_json

MAX_GOAL_CHARS = 16_384


class TaskStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(self.root)
        self.path = self.root / "tasks.json"
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write({"schema": "torq-task-store-v1", "drafts": {}, "plans": {}, "requests": {}, "tasks": {}})

    def _read(self) -> dict[str, Any]:
        metadata = self.path.stat(follow_symlinks=False)
        if (
            not self.path.is_file()
            or self.path.is_symlink()
            or metadata.st_nlink != 1
            or metadata.st_size > 4_194_304
            or not signing_file_permissions_are_restricted(self.path)
        ):
            raise ValueError("task_store_unsafe")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            before = os.fstat(descriptor)
            raw = bytearray()
            while len(raw) <= 4_194_304:
                chunk = os.read(descriptor, min(65_536, 4_194_305 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(raw) > 4_194_304 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("task_store_changed")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, child in pairs:
                if key in result:
                    raise ValueError("task_store_duplicate_key")
                result[key] = child
            return result

        def reject_constant(value: str) -> object:
            del value
            raise ValueError("task_store_non_finite")

        value = json.loads(bytes(raw), object_pairs_hook=unique, parse_constant=reject_constant)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "drafts", "plans", "requests", "tasks"}
            or value.get("schema") != "torq-task-store-v1"
            or not all(isinstance(value.get(key), dict) for key in ("drafts", "plans", "requests", "tasks"))
        ):
            raise ValueError("task_store_invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        temporary.write_text(canonical_json(value), encoding="utf-8", newline="\n")
        restrict_owner_only_file(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        restrict_owner_only_file(self.path)
        fsync_directory(self.root)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def put_draft(
        self,
        project_id: str,
        *,
        goal: str,
        input_paths: list[str],
        output_paths: list[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        if not goal.strip() or len(goal) > MAX_GOAL_CHARS or "\x00" in goal:
            raise ValueError("task_goal_invalid")
        with self._lock:
            data = self._read()
            current = data["drafts"].get(project_id)
            revision = 0 if current is None else current["revision"]
            if expected_revision != revision:
                raise ValueError("task_draft_revision_conflict")
            draft = {
                "project_id": project_id,
                "goal": goal,
                "input_paths": input_paths,
                "output_paths": output_paths,
                "revision": revision + 1,
            }
            data["drafts"][project_id] = draft
            self._write(data)
            return draft

    def delete_draft(self, project_id: str, *, expected_revision: int) -> None:
        with self._lock:
            data = self._read()
            current = data["drafts"].get(project_id)
            if current is None or current["revision"] != expected_revision:
                raise ValueError("task_draft_revision_conflict")
            data["drafts"][project_id] = {
                "project_id": project_id,
                "goal": None,
                "input_paths": [],
                "output_paths": [],
                "revision": expected_revision + 1,
            }
            referenced = {
                task.get("plan_hash")
                for task in data["tasks"].values()
                if isinstance(task, dict)
            }
            data["plans"] = {
                digest: plan
                for digest, plan in data["plans"].items()
                if digest in referenced
                or not (
                    isinstance(plan, dict)
                    and plan.get("project_id") == project_id
                    and isinstance(plan.get("draft_revision"), int)
                    and plan["draft_revision"] <= expected_revision
                )
            }
            self._write(data)

    def save_plan(self, body: Mapping[str, Any]) -> dict[str, Any]:
        plan = dict(body)
        plan_hash = digest_json(plan)
        with self._lock:
            data = self._read()
            existing = data["plans"].get(plan_hash)
            if existing is not None and existing != plan:
                raise ValueError("task_plan_hash_collision")
            data["plans"][plan_hash] = plan
            self._write(data)
        return {**plan, "plan_hash": plan_hash}

    def reserve(self, request_id: str, request_body: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        if not request_id or len(request_id) > 128:
            raise ValueError("task_request_id_invalid")
        if set(request_body) != {"plan_hash", "project_id"} or not all(
            isinstance(request_body.get(key), str) and request_body[key]
            for key in ("plan_hash", "project_id")
        ):
            raise ValueError("task_request_body_invalid")
        request_digest = digest_json(request_body)
        with self._lock:
            data = self._read()
            prior = data["requests"].get(request_id)
            if prior is not None:
                if prior["request_digest"] != request_digest:
                    raise ValueError("task_request_replay_conflict")
                return dict(data["tasks"][prior["task_id"]]), False
            task_id = "run-task-" + secrets.token_hex(12)
            ordinals = [
                item["history_ordinal"] for item in data["tasks"].values()
                if isinstance(item, dict)
                and isinstance(item.get("history_ordinal"), int)
                and not isinstance(item["history_ordinal"], bool)
                and item["history_ordinal"] > 0
            ]
            task = {
                **dict(request_body),
                "task_id": task_id,
                "request_id": request_id,
                "request_digest": request_digest,
                "state": "reserved",
                "provider_dispatch": False,
                "history_ordinal": max(ordinals, default=0) + 1,
                "finding": None,
            }
            data["requests"][request_id] = {"request_digest": request_digest, "task_id": task_id}
            data["tasks"][task_id] = task
            self._write(data)
            return task, True

    def replay(self, request_id: str, plan_hash: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            prior = data["requests"].get(request_id)
            if prior is None:
                return None
            task = data["tasks"].get(prior.get("task_id"))
            if not isinstance(task, dict) or task.get("plan_hash") != plan_hash:
                raise ValueError("task_request_replay_conflict")
            return dict(task)

    def update_task(self, task_id: str, **changes: object) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            current = data["tasks"].get(task_id)
            if current is None:
                raise ValueError("task_unknown")
            current.update(changes)
            self._write(data)
            return dict(current)


__all__ = ["MAX_GOAL_CHARS", "TaskStore"]
