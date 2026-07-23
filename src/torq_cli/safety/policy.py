"""Fail-closed execution policy for sandboxed agent work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from torq_cli.adapters.process import ManagedProcess

__all__ = ["ExecutionPolicy", "ManagedProcess", "PolicyHalt", "ResourceCeilings"]


class PolicyHalt(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourceCeilings:
    runtime_seconds: float
    cost_usd: float
    file_count: int
    changed_lines: int


class ExecutionPolicy:
    def __init__(self, *, commands: set[str], network_hosts: set[str], ceilings: ResourceCeilings) -> None:
        self.commands = {command.lower() for command in commands}
        self.network_hosts = {host.lower() for host in network_hosts}
        self.ceilings = ceilings

    def check_command(self, command: Sequence[str]) -> None:
        executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if command else ""
        executable = executable.removesuffix(".exe")
        if executable not in self.commands:
            raise PolicyHalt(f"command_not_allowed:{executable}")

    def check_network(self, host: str) -> None:
        if host.lower() not in self.network_hosts:
            raise PolicyHalt(f"network_host_not_allowed:{host}")

    def filter_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE"}
        return {key: value for key, value in environment.items() if key.upper() in allowed}

    def enforce_resources(self, **metrics: float | int) -> None:
        for name in ("runtime_seconds", "cost_usd", "file_count", "changed_lines"):
            if float(metrics.get(name, 0)) > float(getattr(self.ceilings, name)):
                raise PolicyHalt(f"ceiling_exceeded:{name}")
