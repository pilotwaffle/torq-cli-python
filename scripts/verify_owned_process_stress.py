"""Run the release-gate process ownership stress test outside normal pytest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from torq_cli.adapters.process import OwnedProcess

_PROVIDER = Path(__file__).parents[1] / "tests" / "fixtures" / "fake_owned_provider.py"


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return environment


def _cycle(cwd: Path) -> None:
    command = (sys.executable, str(_PROVIDER), "--role", "parent")
    with OwnedProcess(command, cwd=str(cwd), env=_environment()) as owned:
        ready: set[int] = set()
        buffered = b""
        deadline = time.monotonic() + 15
        while len(ready) < 3 and time.monotonic() < deadline:
            event = owned.next_event(timeout=0.25)
            if event is None or event.channel != "stdout":
                continue
            buffered += event.data
            lines = buffered.split(b"\n")
            buffered = lines.pop()
            for line in lines:
                value = json.loads(line)
                if value.get("kind") == "ready":
                    ready.add(int(value["pid"]))
        if len(ready) != 3:
            raise RuntimeError("owned_process_ready_timeout")
        observation = owned.force_stop(timeout=10)
        if not observation.confirmed or observation.active_processes != 0:
            raise RuntimeError("owned_process_survivor_detected")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequential", type=int, default=100)
    parser.add_argument("--concurrent", type=int, default=20)
    args = parser.parse_args()
    if os.name != "nt":
        print(json.dumps({"status": "blocked", "finding": "windows_job_required"}))
        return 3
    if args.sequential < 1 or args.concurrent < 1:
        return 2
    with tempfile.TemporaryDirectory(prefix="torq-owned-stress-") as temporary:
        cwd = Path(temporary)
        for _ in range(args.sequential):
            _cycle(cwd)
        barrier = threading.Barrier(args.concurrent + 1)
        errors: list[str] = []

        def worker() -> None:
            barrier.wait()
            try:
                _cycle(cwd)
            except BaseException as exc:
                errors.append(type(exc).__name__ + ":" + str(exc))

        threads = [threading.Thread(target=worker) for _ in range(args.concurrent)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=30)
        if any(thread.is_alive() for thread in threads):
            errors.append("concurrent_worker_timeout")
        if errors:
            print(json.dumps({"status": "failed", "errors": errors}, sort_keys=True))
            return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "sequential_cycles": args.sequential,
                "concurrent_cycles": args.concurrent,
                "survivors": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
