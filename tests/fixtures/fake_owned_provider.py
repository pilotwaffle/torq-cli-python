"""Deterministic adversarial process tree for OwnedProcess tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
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
    parser.add_argument("--role", choices=("parent", "child", "grandchild"), required=True)
    args = parser.parse_args()
    _ignore_stop()
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
