"""Deterministic governed execution graph."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    role: str
    depends_on: tuple[str, ...]
    optional: bool = False


@dataclass(frozen=True)
class CompiledGraph:
    strategy: str
    nodes: tuple[GraphNode, ...]


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    completed: tuple[str, ...]
    stopped_at: str | None = None
    recompiles: int = 0


_PROFILES = {
    "light_v1": ("g1d", "builder", "g2a"),
    "standard_v1": ("g1d", "g1r", "builder", "g2a"),
}


def compile_graph(strategy: str) -> CompiledGraph:
    if strategy not in _PROFILES:
        raise ValueError(f"unknown strategy profile: {strategy}")
    nodes: list[GraphNode] = []
    prior: tuple[str, ...] = ()
    for index, role in enumerate(_PROFILES[strategy], start=1):
        node_id = f"node-{index:02d}"
        nodes.append(GraphNode(node_id, role, prior[-1:]))
        prior = (*prior, node_id)
    return CompiledGraph(strategy, tuple(nodes))


class GraphExecutor:
    def __init__(self, *, max_recompiles: int = 2) -> None:
        self.max_recompiles = max_recompiles

    def execute(
        self,
        graph: CompiledGraph,
        *,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
        live_opt_in: bool = False,
        policy_opt_in: bool = False,
        gate: Callable[[str], bool] = lambda _role: True,
        recompile: Callable[[int], bool] = lambda _cycle: False,
    ) -> ExecutionResult:
        if mode is ExecutionMode.LIVE and not (live_opt_in and policy_opt_in):
            raise ValueError("live execution requires double opt-in")
        if any(node.role in {"push", "merge"} for node in graph.nodes):
            raise ValueError("push and merge are not executable graph roles")
        completed: list[str] = []
        for node in graph.nodes:
            if not gate(node.role):
                return ExecutionResult("gate_stopped", tuple(completed), node.role)
            completed.append(node.role)
        count = 0
        while recompile(count):
            if count >= self.max_recompiles:
                return ExecutionResult("recompile_limit", tuple(completed), recompiles=count)
            count += 1
        return ExecutionResult("completed", tuple(completed), recompiles=count)


class ExecutionEvidenceStore:
    """Persist deterministic graph manifest, trace, and terminal snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, run_id: str, graph: CompiledGraph, result: ExecutionResult) -> tuple[Path, ...]:
        run_root = self.root / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        documents = {
            "manifest.json": {"strategy": graph.strategy, "nodes": [asdict(node) for node in graph.nodes]},
            "trace.json": {"completed": result.completed, "stopped_at": result.stopped_at},
            "snapshot.json": asdict(result),
        }
        paths: list[Path] = []
        for name, payload in documents.items():
            target = run_root / name
            target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            paths.append(target)
        return tuple(paths)
