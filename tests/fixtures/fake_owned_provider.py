"""Deterministic adversarial process tree for OwnedProcess tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time


def _ignore_stop() -> None:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _emit(kind: str, **fields: object) -> None:
    print(json.dumps({"kind": kind, "pid": os.getpid(), **fields}), flush=True)


def _sleep_forever() -> None:
    while True:
        time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--role",
        choices=(
            "parent",
            "child",
            "grandchild",
            "exiting-parent",
            "partial",
            "dual-stream",
            "complete",
            "failed",
            "flood",
            "setsid-escape",
            "double-fork-escape",
        ),
        required=True,
    )
    args = parser.parse_args()
    _ignore_stop()
    if args.role == "complete":
        _emit("complete")
        return 0
    if args.role == "failed":
        print("provider-failed", file=sys.stderr, flush=True)
        return 7
    if args.role == "partial":
        sys.stdout.buffer.write(b"partial-token")
        sys.stdout.buffer.flush()
        _sleep_forever()
    if args.role == "dual-stream":
        barrier = threading.Barrier(3)

        def emit_bytes(descriptor: int, value: bytes) -> None:
            barrier.wait()
            os.write(descriptor, value)

        threads = (
            threading.Thread(target=emit_bytes, args=(1, b"stdout-event\n")),
            threading.Thread(target=emit_bytes, args=(2, b"stderr-event\n")),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        _sleep_forever()
    if args.role == "flood":
        block = b"x" * 4096
        while True:
            os.write(1, block)
    if args.role == "setsid-escape":
        os.setsid()
        _emit("ready", role="setsid-escape")
        _sleep_forever()
    if args.role == "double-fork-escape":
        first = os.fork()
        if first != 0:
            _emit("ready", role="double-fork-parent", child_pid=first)
            _sleep_forever()
        os.setsid()
        second = os.fork()
        if second != 0:
            os._exit(0)
        _emit("ready", role="double-fork-grandchild")
        _sleep_forever()
    if args.role == "grandchild":
        _emit("ready", role="grandchild")
        _sleep_forever()
    if args.role == "child":
        grandchild = subprocess.Popen(
            (sys.executable, __file__, "--role", "grandchild"),
            stdin=subprocess.DEVNULL,
        )
        _emit("ready", role="child", child_pid=grandchild.pid)
        _sleep_forever()
    if args.role == "exiting-parent":
        child = subprocess.Popen(
            (sys.executable, __file__, "--role", "child"),
            stdin=subprocess.DEVNULL,
        )
        _emit("ready", role="exiting-parent", child_pid=child.pid)
        return 0
    child = subprocess.Popen(
        (sys.executable, __file__, "--role", "child"),
        stdin=subprocess.DEVNULL,
    )
    _emit("ready", role="parent", child_pid=child.pid)
    for index in range(3):
        _emit("chunk", index=index)
        time.sleep(0.01)
    _sleep_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
