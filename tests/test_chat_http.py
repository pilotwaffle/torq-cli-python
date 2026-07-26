from __future__ import annotations

import http.client
import json
import queue
import threading
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from torq_cli.interfaces.fleet_http import FleetSessionManager, create_fleet_server


class _Projector:
    def __init__(self, root: Path) -> None:
        self.run_root = root
        self.operational_state: Mapping[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "torq-fleet-snapshot-v3",
            "verification": {
                "state": "live_verified",
                "finding": None,
                "covered_sequence": 1,
                "manifest_generation": 1,
            },
            "data_status": "available",
            "run": {"run_id": self.run_root.name, "workflow_state": "running"},
            "summary": {"open_actions": 0},
            "lanes": [],
            "actions": [],
            "settlement": None,
        }


class _Chat:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.active: str | None = None
        self.submitted: Mapping[str, Any] | None = None
        self.cancelled: str | None = None
        self.channel: queue.Queue[Any] = queue.Queue()

    def submit(
        self,
        *,
        turn_id: str,
        text: str,
        attachments: list[Mapping[str, str]],
    ) -> Mapping[str, Any]:
        self.active = turn_id
        self.submitted = {"turn_id": turn_id, "text": text, "attachments": attachments}
        return {"sequence": 1}

    def cancel(self, turn_id: str, *, timeout: float = 5.0) -> Mapping[str, Any]:
        assert timeout == 5.0
        self.cancelled = turn_id
        self.active = None
        return {"sequence": 2}

    def subscribe(self, *, capacity: int = 256) -> queue.Queue[Any]:
        assert capacity == 256
        return self.channel

    def unsubscribe(self, channel: queue.Queue[Any]) -> None:
        assert channel is self.channel

    def snapshot(self) -> Mapping[str, Any]:
        return {"active_turn_id": self.active, "stream_sequence": 0}

    def shutdown(self, *, timeout: float = 5.0) -> Mapping[str, Any] | None:
        del timeout
        return None


def _request(
    server: Any,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    body: Mapping[str, Any] | None = None,
    origin: str | None = None,
) -> tuple[int, Mapping[str, str], dict[str, Any]]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {"Host": f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    if encoded is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    if origin:
        headers["Origin"] = origin
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    result = json.loads(response.read())
    response_headers = {key.casefold(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, result


def test_chat_routes_are_session_and_same_origin_gated(tmp_path: Path) -> None:
    projector = _Projector(tmp_path / "run-chat-http")
    projector.run_root.mkdir()
    chat = _Chat(projector.run_root)
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        projector,  # type: ignore[arg-type]
        port=0,
        sessions=sessions,
        chat_controller=chat,
        chat_snapshot_provider=lambda: {
            "schema": "torq-chat-projection-v1",
            "data_status": "available",
            "messages": [],
            "active_turn_id": None,
            "last_sequence": 0,
            "status": "ready",
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/chat")
        assert status == 401
        assert body["finding"] == "fleet_session_required"

        status, _, body = _request(server, "GET", "/api/v1/chat", cookie=cookie)
        assert status == 200
        assert body["status"] == "ready"

        status, _, body = _request(
            server,
            "POST",
            "/api/v1/chat/turns",
            cookie=cookie,
            origin="https://attacker.invalid",
            body={"turn_id": "turn-1", "text": "hello", "attachments": []},
        )
        assert status == 403
        assert body["finding"] == "fleet_origin_denied"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_chat_submit_and_cancel_are_forwarded_to_single_owner(tmp_path: Path) -> None:
    projector = _Projector(tmp_path / "run-chat-control")
    projector.run_root.mkdir()
    chat = _Chat(projector.run_root)
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        projector,  # type: ignore[arg-type]
        port=0,
        sessions=sessions,
        chat_controller=chat,
        chat_snapshot_provider=lambda: {
            "schema": "torq-chat-projection-v1",
            "data_status": "available",
            "messages": [],
            "last_sequence": 0,
            "status": "ready",
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    origin = f"http://127.0.0.1:{port}"
    try:
        status, headers, body = _request(
            server,
            "POST",
            "/api/v1/chat/turns",
            cookie=cookie,
            origin=origin,
            body={"turn_id": "turn-1", "text": "hello", "attachments": []},
        )
        assert status == 202
        assert body["result"]["sequence"] == 1
        assert chat.submitted == {"turn_id": "turn-1", "text": "hello", "attachments": []}
        cookie = headers["set-cookie"].split(";", 1)[0]

        status, _, body = _request(
            server,
            "POST",
            "/api/v1/chat/turns/turn-1/cancel",
            cookie=cookie,
            origin=origin,
            body={},
        )
        assert status == 202
        assert body["result"]["sequence"] == 2
        assert chat.cancelled == "turn-1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_chat_sse_streams_provisional_output_without_persisting_it(tmp_path: Path) -> None:
    projector = _Projector(tmp_path / "run-chat-sse")
    projector.run_root.mkdir()
    chat = _Chat(projector.run_root)
    chat.active = "turn-1"
    chat.channel.put(
        SimpleNamespace(turn_id="turn-1", sequence=1, kind="stdout", data=b"partial token")
    )
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        projector,  # type: ignore[arg-type]
        port=0,
        sessions=sessions,
        chat_controller=chat,
        chat_snapshot_provider=lambda: {
            "schema": "torq-chat-projection-v1",
            "data_status": "available",
            "messages": [],
            "last_sequence": 1,
            "status": "running",
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(
            "GET",
            "/api/v1/chat/events",
            headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie},
        )
        response = connection.getresponse()
        assert response.status == 200
        line = response.fp.readline().decode("utf-8")
        assert "output_delta" in line
        assert "partial token" in line
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_chat_controller_must_match_projected_run(tmp_path: Path) -> None:
    projector = _Projector(tmp_path / "run-a")
    projector.run_root.mkdir()
    other = tmp_path / "run-b"
    other.mkdir()
    with pytest.raises(ValueError, match="fleet_control_run_mismatch"):
        create_fleet_server(
            projector,  # type: ignore[arg-type]
            port=0,
            chat_controller=_Chat(other),
            chat_snapshot_provider=lambda: {},
        )
