"""Trusted systemd-contained bootstrap for Linux provider processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

_BOOTSTRAP_LIMIT = 4_000_000


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise OSError("owned_process_bootstrap_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_environment_and_prompt() -> tuple[dict[str, str], bytes]:
    length = int.from_bytes(_read_exact(0, 4), "big")
    if length > _BOOTSTRAP_LIMIT:
        raise ValueError("owned_process_environment_too_large")
    decoded = json.loads(_read_exact(0, length).decode("utf-8"))
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in key
        or "=" in key
        or "\x00" in value
        for key, value in decoded.items()
    ):
        raise ValueError("owned_process_environment_invalid")
    prompt_length = int.from_bytes(_read_exact(0, 8), "big")
    if prompt_length > 42_000_000:
        raise ValueError("owned_process_input_too_large")
    return decoded, _read_exact(0, prompt_length)


def _write_prompt(process: subprocess.Popen[bytes], content: bytes) -> None:
    stream = process.stdin
    if stream is None:
        return
    try:
        stream.write(content)
        stream.flush()
    finally:
        stream.close()


def main(arguments: list[str] | None = None) -> int:
    """Supervise the provider and make coordinator lifetime a kill lease."""
    args = list(sys.argv[1:] if arguments is None else arguments)
    if not args or args[0] != "--" or len(args) == 1:
        raise ValueError("owned_process_command_invalid")
    environment, prompt = _read_environment_and_prompt()
    provider = subprocess.Popen(
        args[1:],
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=None,
        stderr=None,
        close_fds=True,
    )
    writer = threading.Thread(target=_write_prompt, args=(provider, prompt), daemon=True)
    writer.start()
    lease_lost = threading.Event()

    def watch_lease() -> None:
        try:
            os.read(0, 1)
        finally:
            lease_lost.set()

    threading.Thread(target=watch_lease, daemon=True).start()
    while provider.poll() is None:
        if lease_lost.wait(0.02):
            # Exiting the systemd service main process activates its
            # KillMode=control-group cleanup for every provider descendant.
            return 125
    writer.join(timeout=1)
    return provider.returncode


if __name__ == "__main__":
    raise SystemExit(main())
