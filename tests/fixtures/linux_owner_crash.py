"""Crash a coordinator after its adversarial provider tree is running."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from torq_cli.adapters.process import OwnedProcess


def main() -> int:
    output = Path(sys.argv[1])
    provider = Path(sys.argv[2])
    owner = OwnedProcess(
        (sys.executable, str(provider), "--role", "parent"),
        cwd=str(output.parent),
        env=dict(os.environ),
    )
    pids = {owner.pid}
    buffered = b""
    deadline = time.monotonic() + 10
    while len(pids) < 4 and time.monotonic() < deadline:
        event = owner.next_event(timeout=0.1)
        if event is None or event.channel != "stdout":
            continue
        buffered += event.data
        lines = buffered.split(b"\n")
        buffered = lines.pop()
        for line in lines:
            row = json.loads(line)
            pids.add(int(row["pid"]))
            child = row.get("child_pid")
            if child is not None:
                pids.add(int(child))
    output.write_text(json.dumps({"pids": sorted(pids)}), encoding="utf-8")
    os._exit(91)


if __name__ == "__main__":
    raise SystemExit(main())
