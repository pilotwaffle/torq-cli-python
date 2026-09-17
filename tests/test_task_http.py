from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

from torq_cli.application.fleet import FleetProjector
from torq_cli.interfaces.fleet_http import create_fleet_server


class _Tasks:
    def __init__(self) -> None:
        self.saved = 0
        self.started: tuple[str, str] | None = None

    def capabilities(self) -> dict[str, Any]:
        return {"schema": "torq-task-capabilities-v1", "projects": [{"id": "project", "label": "Project"}], "can_start_task": True, "active_task_id": None}

    def draft(self, project_id: str) -> dict[str, Any] | None:
        if project_id != "project":
            raise ValueError("task_project_unknown")
        return None

    def save_draft(self, project_id: str, *, goal: str, input_paths: list[str], output_paths: list[str], expected_revision: int) -> dict[str, Any]:
        self.saved += 1
        return {"project_id": project_id, "goal": goal, "input_paths": input_paths, "output_paths": output_paths, "revision": expected_revision + 1}

    def delete_draft(self, project_id: str, *, expected_revision: int) -> None:
        del project_id, expected_revision

    def review_plan(self, *, project_id: str, draft_revision: int, input_paths: list[str], output_paths: list[str]) -> dict[str, Any]:
        return {"project_id": project_id, "draft_revision": draft_revision, "input_paths": input_paths, "output_paths": output_paths, "plan_hash": "sha256:" + "1" * 64}

    def start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]:
        self.started = (request_id, plan_hash)
        return {"task_id": "run-task-" + "1" * 24, "state": "reserved"}

    def replay_start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]:
        if self.started != (request_id, plan_hash):
            raise ValueError("task_request_replay_missing")
        return self.start(request_id=request_id, plan_hash=plan_hash)

    def stop(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "stop_requested"}

    def task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "candidate_ready", "verified": True}

    def history(self) -> list[dict[str, Any]]:
        return []


def _request(server: object, method: str, path: str, *, cookie: str | None = None, origin: str | None = None, body: object | None = None) -> tuple[int, dict[str, str], dict[str, Any]]:
    host, port = server.server_address[:2]  # type: ignore[attr-defined]
    connection = http.client.HTTPConnection(host, port)
    headers: dict[str, str] = {}
    encoded = None
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    if body is not None:
        encoded = json.dumps(body)
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result = response.status, dict(response.getheaders()), json.loads(raw) if raw else {}
    connection.close()
    return result


def test_task_http_requires_session_origin_and_closed_request_body(tmp_path: Path) -> None:
    tasks = _Tasks()
    server = create_fleet_server(FleetProjector(tmp_path / "no-run"), port=0, task_service=tasks)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    try:
        assert _request(server, "GET", "/api/v1/tasks/capabilities")[0] == 401
        bootstrap = _request(server, "GET", f"/bootstrap?nonce={server.fleet_bootstrap_nonce}&view=task")
        assert bootstrap[0] == 303 and bootstrap[1]["Location"] == "/?view=task"
        cookie = bootstrap[1]["Set-Cookie"].split(";", 1)[0]
        status, _, caps = _request(server, "GET", "/api/v1/tasks/capabilities", cookie=cookie)
        assert status == 200 and caps["projects"][0]["id"] == "project"
        payload = {"goal": "Build", "input_paths": [], "output_paths": ["a.py"], "expected_revision": 0}
        denied = _request(server, "POST", "/api/v1/tasks/drafts/project", cookie=cookie, origin="http://attacker", body=payload)
        assert denied[0] == 403
        cookie = denied[1]["Set-Cookie"].split(";", 1)[0]
        status, headers, body = _request(server, "POST", "/api/v1/tasks/drafts/project", cookie=cookie, origin=origin, body=payload | {"unknown": True})
        assert status == 409 and body["finding"] == "task_draft_request_invalid" and tasks.saved == 0
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, body = _request(server, "POST", "/api/v1/tasks/drafts/project", cookie=cookie, origin=origin, body=payload)
        assert status == 200 and body["result"]["revision"] == 1 and tasks.saved == 1
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        start_body = {"request_id": "stable-request", "plan_hash": "sha256:" + "1" * 64}
        first = _request(server, "POST", "/api/v1/tasks", cookie=cookie, origin=origin, body=start_body)
        assert first[0] == 202
        # Keep the old cookie to simulate losing the response (and its rotated cookie).
        denied_replay = _request(server, "POST", "/api/v1/tasks", cookie=cookie, origin=origin, body=start_body | {"request_id": "different"})
        assert denied_replay[0] == 409 and "Set-Cookie" not in denied_replay[1]
        replay = _request(server, "POST", "/api/v1/tasks", cookie=cookie, origin=origin, body=start_body)
        assert replay[0] == 202 and "Set-Cookie" in replay[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
