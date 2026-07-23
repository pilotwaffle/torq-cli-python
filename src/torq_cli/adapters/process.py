"""The sole production subprocess boundary; audited by hermetic import tests."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence


class ManagedProcess:
    """Cross-platform process-tree boundary using process groups/job-tree fallback."""

    def __init__(self, command: Sequence[str], *, cwd: str, env: Mapping[str, str]) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            creationflags=flags,
            start_new_session=os.name != "nt",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def cancel_tree(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(("taskkill", "/PID", str(self.process.pid), "/T", "/F"), capture_output=True, check=False)
        else:
            killpg = getattr(os, "killpg")
            getpgid = getattr(os, "getpgid")
            killpg(getpgid(self.process.pid), getattr(signal, "SIGKILL"))
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

