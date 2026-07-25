from __future__ import annotations

import http.client
import threading
from html.parser import HTMLParser
from pathlib import Path

from torq_cli.application.fleet import FleetProjector
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain
from torq_cli.interfaces.fleet_http import create_fleet_server


class _MarkupAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.inline_styles = 0
        self.landmarks: set[str] = set()
        self.controls: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "script" and "src" not in values:
            self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        if tag in {"header", "main", "footer"}:
            self.landmarks.add(tag)
        if tag == "button" and values.get("id"):
            self.controls.add(str(values["id"]))


def _server(tmp_path: Path) -> tuple[object, threading.Thread]:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "ui-secret-run-id",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    chain.append("run_attested", {"mode": "dry_run"})
    chain.seal()
    server = create_fleet_server(FleetProjector(chain.root), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(server: object, path: str) -> tuple[int, dict[str, str], bytes]:
    host, port = server.server_address[:2]  # type: ignore[attr-defined]
    connection = http.client.HTTPConnection(host, port)
    connection.request("GET", path)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_fleet_ui_shell_is_public_but_contains_no_run_data(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    try:
        status, headers, body = _get(server, "/")
    finally:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
        thread.join(timeout=2)

    markup = body.decode("utf-8")
    audit = _MarkupAudit()
    audit.feed(markup)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
    assert "ui-secret-run-id" not in markup
    assert audit.inline_scripts == 0
    assert audit.inline_styles == 0
    assert audit.landmarks == {"header", "main", "footer"}
    assert audit.controls == {"notify-button"}
    assert 'href="#fleet-board"' in markup
    assert 'aria-live="polite"' in markup


def test_fleet_ui_assets_are_local_no_store_and_host_guarded(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    host, port = server.server_address[:2]  # type: ignore[attr-defined]
    try:
        css_status, css_headers, css = _get(server, "/assets/fleet.css")
        js_status, js_headers, javascript = _get(server, "/assets/fleet.js")

        connection = http.client.HTTPConnection(host, port)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", f"attacker.example:{port}")
        connection.endheaders()
        rebound = connection.getresponse()
        rebound_body = rebound.read()
        connection.close()
    finally:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
        thread.join(timeout=2)

    assert css_status == 200
    assert css_headers["Content-Type"] == "text/css; charset=utf-8"
    assert css_headers["Cache-Control"] == "no-store"
    assert b"prefers-reduced-motion" in css
    assert b":focus-visible" in css
    assert js_status == 200
    assert js_headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert js_headers["Cache-Control"] == "no-store"
    assert b'fetch("/api/v1/fleet"' in javascript
    assert b"innerHTML" not in javascript
    assert rebound.status == 421
    assert b"fleet_host_denied" in rebound_body


def test_fleet_bootstrap_lands_on_ui_without_exposing_session_in_url(
    tmp_path: Path,
) -> None:
    server, thread = _server(tmp_path)
    host, port = server.server_address[:2]  # type: ignore[attr-defined]
    nonce = str(getattr(server, "fleet_bootstrap_nonce"))
    try:
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", f"/bootstrap?nonce={nonce}")
        response = connection.getresponse()
        location = response.getheader("Location")
        cookie = str(response.getheader("Set-Cookie"))
        response.read()
        connection.close()
    finally:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
        thread.join(timeout=2)

    assert response.status == 303
    assert location == "/"
    assert "torq_fleet_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/api/v1" in cookie
    assert "nonce" not in str(location)
    assert "torq_fleet_session" not in str(location)
