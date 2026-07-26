"""Crash a coordinator after its adversarial provider tree is running."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from torq_cli.adapters.process import ExperimentalLinuxOwnedProcess


def _unified_cgroup(pid: int) -> str:
    for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers and path.startswith("/"):
            return path
    raise RuntimeError("owned_process_unified_cgroup_missing")


def main() -> int:
    output = Path(sys.argv[1])
    provider = Path(sys.argv[2])
    owner = ExperimentalLinuxOwnedProcess(
        (
            sys.executable,
            str(provider),
            "--role",
            "lease-theft",
            "--coordinator-pid",
            str(os.getpid()),
        ),
        cwd=str(output.parent),
        env=dict(os.environ),
    )
    pids = {owner.pid}
    buffered = b""
    deadline = time.monotonic() + 10
    lease_opened: int | None = None
    while len(pids) < 4 and time.monotonic() < deadline:
        event = owner.next_event(timeout=0.1)
        if event is None or event.channel != "stdout":
            continue
        buffered += event.data
        lines = buffered.split(b"\n")
        buffered = lines.pop()
        for line in lines:
            row = json.loads(line)
            if "lease_opened" in row:
                lease_opened = int(row["lease_opened"])
            pids.add(int(row["pid"]))
            child = row.get("child_pid")
            if child is not None:
                pids.add(int(child))
    if len(pids) != 4 or lease_opened is None:
        raise RuntimeError("owned_process_expected_tree_incomplete")
    control_group = _unified_cgroup(owner.pid)
    unit = Path(control_group).name
    if not unit.startswith("torq-chat-") or not unit.endswith(".service"):
        raise RuntimeError("owned_process_transient_unit_invalid")
    output.write_text(
        json.dumps(
            {
                "control_group": control_group,
                "pids": sorted(pids),
                "lease_opened": lease_opened,
                "unit": unit,
            }
        ),
        encoding="utf-8",
    )
    os._exit(91)


if __name__ == "__main__":
    raise SystemExit(main())
