from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from torq_cli.application.workspace import classify_workspace_root
from torq_cli.interfaces.fleet_http import FleetSessionManager, create_fleet_server


class _Projector:
    def __init__(self, root: Path, *, closed: bool = False) -> None:
        self.run_root = root
        self.closed = closed
        self.operational_state: Mapping[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "torq-fleet-snapshot-v3",
            "verification": {
                "state": "sealed_verified" if self.closed else "live_verified",
                "finding": None,
                "covered_sequence": 1,
                "manifest_generation": 1,
            },
            "data_status": "available",
            "run": {
                "run_id": self.run_root.name,
                "workflow_state": "closed" if self.closed else "open",
                "mode": "dry_run",
            },
            "summary": {"open_actions": 0},
            "lanes": [],
            "actions": [],
            "settlement": None,
        }


class _TamperedProjector(_Projector):
    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "torq-fleet-snapshot-v3",
            "verification": {
                "state": "tampered",
                "finding": "receipt_hash_mismatch",
                "covered_sequence": None,
                "manifest_generation": None,
            },
            "data_status": "unavailable",
            "run": None,
            "summary": None,
            "lanes": [],
            "actions": [],
            "settlement": None,
        }


class _Chat:
    def __init__(self, root: Path, active: str | None = None) -> None:
        self.root = root
        self.active = active

    def snapshot(self) -> Mapping[str, Any]:
        return {"active_turn_id": self.active, "stream_sequence": 0}

    def subscribe(self, *, capacity: int = 256) -> Any:
        raise AssertionError(capacity)

    def unsubscribe(self, channel: Any) -> None:
        raise AssertionError(channel)

    def submit(self, **kwargs: Any) -> Mapping[str, Any]:
        raise AssertionError(kwargs)

    def cancel(self, turn_id: str, *, timeout: float = 5.0) -> Mapping[str, Any]:
        raise AssertionError((turn_id, timeout))

    def shutdown(self, *, timeout: float = 5.0) -> Mapping[str, Any] | None:
        return None


def _evidence_shape(root: Path) -> None:
    root.mkdir()
    for name in ("receipts.jsonl", "terminal-manifest.json", "run-certificate.json"):
        (root / name).write_text("{}", encoding="utf-8")


def _get(server: Any, path: str, cookie: str | None = None) -> tuple[int, dict[str, Any]]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    return response.status, body


def _post_chat(server: Any, cookie: str) -> tuple[int, dict[str, Any]]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    payload = json.dumps({"turn_id": "turn-direct", "text": "hello", "attachments": []})
    connection.request(
        "POST",
        "/api/v1/chat/turns",
        body=payload,
        headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Content-Length": str(len(payload.encode("utf-8"))),
        },
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    return response.status, body


def test_workspace_root_classification_is_bounded_stable_and_separate(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    collection = tmp_path / "collection"
    collection.mkdir()
    for index in range(80):
        (collection / f"notes-{index:03d}").mkdir()
    _evidence_shape(collection / "run-second")

    missing_first = classify_workspace_root(missing)
    missing_second = classify_workspace_root(missing)
    assert missing_first["root_kind"] == "missing"
    assert missing_first["workspace_id"] == missing_second["workspace_id"]
    assert missing_first["workspace_id"] != classify_workspace_root(empty)["workspace_id"]
    assert classify_workspace_root(empty)["root_kind"] == "empty"
    classified_collection = classify_workspace_root(collection)
    assert classified_collection["root_kind"] == "collection"
    assert classified_collection["runs"] == ["run-second"]


def test_workspace_classifies_verified_and_untrusted_individual_runs(tmp_path: Path) -> None:
    root = tmp_path / "run-selected"
    _evidence_shape(root)
    verified = classify_workspace_root(root, _Projector(root).snapshot())
    assert verified["root_kind"] == "individual_run"
    assert verified["trusted"] is True
    assert verified["selected_run_id"] == "run-selected"
    assert verified["run_mode"] == "dry_run"

    custom_root = tmp_path / "historical.custom-id"
    _evidence_shape(custom_root)
    custom = classify_workspace_root(custom_root, _Projector(custom_root).snapshot())
    assert custom["trusted"] is True
    assert custom["selected_run_id"] == "historical.custom-id"

    tampered = classify_workspace_root(
        root,
        {
            "data_status": "unavailable",
            "verification": {"state": "tampered", "finding": "receipt_hash_mismatch"},
            "run": None,
        },
    )
    assert tampered["root_kind"] == "individual_run"
    assert tampered["trusted"] is False
    assert tampered["reason_code"] == "workspace_run_untrusted"


def test_workspace_rejects_linked_root_or_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "actual"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory_symlink_unavailable")
    assert classify_workspace_root(link)["reason_code"] == "workspace_root_link_denied"
    assert classify_workspace_root(link / "missing")["reason_code"] == "workspace_root_link_denied"


@pytest.mark.parametrize(
    ("provider", "attachment_count"),
    [("claude", 0), ("deepseek", 8), ("invented", 0)],
)
def test_workspace_metadata_is_authenticated_redacted_and_provider_driven(
    tmp_path: Path,
    provider: str,
    attachment_count: int,
) -> None:
    root = tmp_path / "run-metadata"
    _evidence_shape(root)
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        _Projector(root),  # type: ignore[arg-type]
        port=0,
        sessions=sessions,
        chat_controller=_Chat(root),  # type: ignore[arg-type]
        chat_snapshot_provider=lambda: {
            "data_status": "available",
            "messages": [],
            "status": "ready",
        },
        workspace_chat_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
    try:
        status, denied = _get(server, "/api/v1/workspace")
        assert status == 401
        assert denied["finding"] == "fleet_session_required"
        status, body = _get(server, "/api/v1/workspace", cookie)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    after = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))

    assert before == after
    assert body["workspace_id"].startswith("workspace_")
    assert str(tmp_path) not in json.dumps(body)
    assert body["capabilities"]["can_start_task"] is False
    assert body["capabilities"]["execution_supported"] is False
    assert len(body["capabilities"]["attachment_types"]) == attachment_count
    assert body["provider"]["authentication"] == "not_checked"


def test_workspace_stop_capability_is_independent_of_send(tmp_path: Path) -> None:
    root = tmp_path / "run-active"
    _evidence_shape(root)
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        _Projector(root),  # type: ignore[arg-type]
        port=0,
        sessions=sessions,
        chat_controller=_Chat(root, "turn-active"),  # type: ignore[arg-type]
        chat_snapshot_provider=lambda: {"data_status": "available", "messages": []},
        workspace_chat_provider="deepseek",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/api/v1/workspace", cookie)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert body["capabilities"]["can_discuss_run"] is False
    assert body["capabilities"]["can_cancel"] is True
    assert body["guidance"]["reason_code"] == "chat_turn_active"


@pytest.mark.parametrize(
    ("projector_factory", "chat_data_status", "active_turn", "finding"),
    [
        (lambda root: _Projector(root, closed=True), "available", None, "fleet_session_read_only"),
        (lambda root: _TamperedProjector(root), "available", None, "workspace_run_untrusted"),
        (lambda root: _Projector(root), "unavailable", None, "chat_runtime_unavailable"),
        (lambda root: _Projector(root), "available", "turn-active", "chat_turn_active"),
    ],
)
def test_direct_chat_submit_rechecks_fresh_workspace_eligibility_before_get(
    tmp_path: Path,
    projector_factory: Any,
    chat_data_status: str,
    active_turn: str | None,
    finding: str,
) -> None:
    root = tmp_path / "run-submit-gate"
    _evidence_shape(root)
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        projector_factory(root),
        port=0,
        sessions=sessions,
        chat_controller=_Chat(root, active_turn),  # type: ignore[arg-type]
        chat_snapshot_provider=lambda: {
            "data_status": chat_data_status,
            "messages": [],
        },
        workspace_chat_provider="claude",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_chat(server, cookie)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 409
    assert body["finding"] == finding
