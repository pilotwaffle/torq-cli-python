"""Read-only Conductor/MMH extraction-viability auditor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_SUBSYSTEMS = (
    "engine",
    "adapters",
    "telemetry",
    "redaction",
    "graph_compiler",
    "policy_gates",
    "receipts",
    "resume",
)

_SPECS: dict[str, dict[str, Any]] = {
    "engine": {
        "verdict": "REUSE",
        "paths": ("torq_mmh/router/engine.py",),
        "reason": "Core orchestration and budget contracts have direct Phase 0A fixture coverage.",
    },
    "adapters": {
        "verdict": "WRAP",
        "paths": ("torq_mmh/router/adapters.py",),
        "reason": "Provider HTTP and credential surfaces require injected standalone transports.",
    },
    "telemetry": {
        "verdict": "REBUILD",
        "paths": ("torq_mmh/router/telemetry.py",),
        "reason": "SQLite persistence needs an explicit schema and lifecycle boundary.",
    },
    "redaction": {
        "verdict": "REUSE",
        "paths": ("torq_mmh/router/redaction.py",),
        "reason": "Pure fail-closed pattern behavior is already fixture-covered.",
    },
    "graph_compiler": {
        "verdict": "REUSE",
        "paths": ("torq_console/conductor/compile.py",),
        "reason": "Deterministic graph compilation is separable from writer services.",
    },
    "policy_gates": {
        "verdict": "REUSE",
        "paths": ("torq_console/conductor/policy.py",),
        "reason": "Policy evaluation is a pure contract with focused tests.",
    },
    "receipts": {
        "verdict": "WRAP",
        "paths": ("torq_console/conductor/receipt_emitter.py",),
        "reason": "Receipt projection is reusable but Supabase persistence must stay behind an adapter.",
    },
    "resume": {
        "verdict": "WRAP",
        "paths": ("torq_mmh/tests/test_phase0a.py", "torq_mmh/router/engine.py"),
        "reason": "Hash-gated resume is proven, but its telemetry store must use the standalone boundary.",
    },
}

_COUPLING_MARKERS = ("SUPABASE", "RAILWAY", "os.environ", "httpx", "sqlite3")


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _evidence(root: Path, relative_paths: tuple[str, ...]) -> tuple[list[str], list[str], int]:
    evidence: list[str] = []
    couplings: set[str] = set()
    lines = 0
    for relative in relative_paths:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        count = len(text.splitlines())
        lines += count
        evidence.append(f"{relative}:{count} lines")
        for marker in _COUPLING_MARKERS:
            if marker in text:
                couplings.add(marker)
    return evidence, sorted(couplings), lines


def audit_extraction(root: Path) -> dict[str, Any]:
    """Return a deterministic evidence report for an explicit upstream root."""
    subsystems: dict[str, Any] = {}
    missing: list[str] = []
    total_rebuild = 0
    for name in REQUIRED_SUBSYSTEMS:
        spec = _SPECS[name]
        relative_paths = tuple(spec["paths"])
        evidence, couplings, source_lines = _evidence(root, relative_paths)
        if len(evidence) != len(relative_paths):
            missing.append(name)
        verdict = str(spec["verdict"])
        multiplier = {"REUSE": 0.0, "WRAP": 0.25, "REBUILD": 1.0}[verdict]
        rebuild_lines = int(round(source_lines * multiplier))
        total_rebuild += rebuild_lines
        subsystems[name] = {
            "verdict": verdict,
            "reason": spec["reason"],
            "evidence": evidence,
            "couplings": couplings,
            "source_lines": source_lines,
            "estimated_rebuild_lines": rebuild_lines,
        }
    resume_test = root / "torq_mmh/tests/test_phase0a.py"
    resume_contract_present = resume_test.is_file() and "test_resume_after_failure_no_rebill" in resume_test.read_text(
        encoding="utf-8", errors="replace"
    )
    return {
        "schema": "torq-extraction-audit-v1",
        "complete": not missing,
        "missing_subsystems": missing,
        "resume_contract_present": resume_contract_present,
        "subsystems": subsystems,
        "estimated_rebuild_lines": total_rebuild,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    report = audit_extraction(args.root.resolve())
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
