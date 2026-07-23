from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Generator
from types import MappingProxyType
from typing import Any

import pytest


def validate_network_mode(value: str) -> str:
    if value != "deny":
        raise RuntimeError("invalid_test_network_mode")
    return value


def _loopback(host: object) -> bool:
    text = host.decode("ascii", errors="ignore") if isinstance(host, bytes) else str(host)
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def guarded_getaddrinfo(
    original: Callable[..., list[Any]],
    host: object,
    port: object,
    *args: Any,
    **kwargs: Any,
) -> list[Any]:
    if not _loopback(host):
        raise RuntimeError("network_egress_denied")
    return original(host, port, *args, **kwargs)


def pytest_sessionstart(session: pytest.Session) -> None:
    validate_network_mode(os.environ.get("TORQ_TEST_NETWORK_MODE", "deny"))


@pytest.fixture(autouse=True)
def hermetic_test_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[MappingProxyType[str, str], None, None]:
    before = dict(os.environ)
    original = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: guarded_getaddrinfo(original, host, port, *args, **kwargs),
    )
    yield MappingProxyType(before)
    os.environ.clear()
    os.environ.update(before)
