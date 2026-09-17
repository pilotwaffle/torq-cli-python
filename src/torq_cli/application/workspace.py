"""Read-only workspace classification and capability metadata."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_EVIDENCE_FILES = ("receipts.jsonl", "terminal-manifest.json", "run-certificate.json")
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COLLECTION_RUN_ID = re.compile(r"run-[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_TRUSTED_STATES = frozenset({"live_verified", "sealed_verified", "live_catching_up"})
_MAX_COLLECTION_RUNS = 64
_MAX_SCAN_ENTRIES = 256


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(details, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _has_link_component(path: Path) -> bool:
    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        if _is_link_or_reparse(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _workspace_id(root: Path) -> str:
    canonical = os.path.normcase(os.path.abspath(os.fspath(root)))
    digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"workspace_{digest[:32]}"


def _safe_run_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_RUN_ID.fullmatch(value) else None


def _collection_runs(root: Path) -> list[str]:
    candidates: list[str] = []
    try:
        scanner = os.scandir(root)
    except OSError:
        return candidates
    with scanner:
        for index, entry in enumerate(scanner):
            if index >= _MAX_SCAN_ENTRIES or len(candidates) >= _MAX_COLLECTION_RUNS:
                break
            run_id = _safe_run_id(entry.name)
            child = Path(entry.path)
            if (
                run_id is None
                or _COLLECTION_RUN_ID.fullmatch(entry.name) is None
                or _is_link_or_reparse(child)
            ):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False) or not all(
                    (child / name).is_file() for name in _EVIDENCE_FILES
                ):
                    continue
            except OSError:
                continue
            candidates.append(run_id)
    return sorted(candidates, key=str.casefold)


def classify_workspace_root(
    root: Path,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one configured root without creating keys, journals, or directories."""
    result: dict[str, Any] = {
        "workspace_id": _workspace_id(root),
        "root_kind": "unavailable",
        "selected_run_id": None,
        "runs": [],
        "trusted": False,
        "verification_state": "unknown",
        "run_mode": "unknown",
        "reason_code": "workspace_root_unavailable",
        "message": "This workspace could not be opened.",
        "remediation": "Check the run location and try again.",
    }
    if _has_link_component(root):
        return {
            **result,
            "reason_code": "workspace_root_link_denied",
            "message": "Linked run folders are not opened by Workspace.",
            "remediation": "Launch Workspace with the real, local individual run folder.",
        }
    if not root.exists():
        return {
            **result,
            "root_kind": "missing",
            "reason_code": "workspace_root_missing",
            "message": "This run folder does not exist.",
            "remediation": "Launch Workspace with an existing individual run folder.",
        }
    try:
        if not root.is_dir():
            return {
                **result,
                "reason_code": "workspace_root_not_directory",
                "message": "The selected workspace is not a folder.",
                "remediation": "Choose an individual TORQ run folder.",
            }
        with os.scandir(root) as scanner:
            has_entries = next(scanner, None) is not None
    except OSError:
        return result
    if not has_entries:
        return {
            **result,
            "root_kind": "empty",
            "reason_code": "workspace_root_empty",
            "message": "This folder does not contain a TORQ run yet.",
            "remediation": "Create a run with the CLI, then launch Workspace with that run folder.",
        }

    has_evidence_shape = any((root / name).exists() for name in _EVIDENCE_FILES)
    if has_evidence_shape:
        verification = snapshot.get("verification") if isinstance(snapshot, Mapping) else None
        run = snapshot.get("run") if isinstance(snapshot, Mapping) else None
        verification_state = (
            str(verification.get("state")) if isinstance(verification, Mapping) else "unknown"
        )
        selected_run_id = _safe_run_id(run.get("run_id")) if isinstance(run, Mapping) else None
        trusted = bool(
            isinstance(snapshot, Mapping)
            and snapshot.get("data_status") == "available"
            and verification_state in _TRUSTED_STATES
            and selected_run_id is not None
        )
        if trusted:
            mode = run.get("mode") if isinstance(run, Mapping) else None
            return {
                **result,
                "root_kind": "individual_run",
                "selected_run_id": selected_run_id,
                "trusted": True,
                "verification_state": verification_state,
                "run_mode": mode if isinstance(mode, str) else "unknown",
                "reason_code": None,
                "message": "This run is ready to inspect.",
                "remediation": None,
            }
        return {
            **result,
            "root_kind": "individual_run",
            "selected_run_id": selected_run_id,
            "verification_state": verification_state,
            "reason_code": "workspace_run_untrusted",
            "message": "This run's history could not be verified.",
            "remediation": "Inspect Fleet details or choose another verified individual run.",
        }

    runs = _collection_runs(root)
    if runs:
        return {
            **result,
            "root_kind": "collection",
            "runs": runs,
            "reason_code": "workspace_run_selection_required",
            "message": "Choose an individual run to open.",
            "remediation": "Relaunch with --run-root followed by one of the relative run IDs below.",
        }
    return {
        **result,
        "reason_code": "workspace_root_unrecognized",
        "message": "This folder is not a TORQ run or run collection.",
        "remediation": "Choose a folder containing one verified run, or a collection of run-* folders.",
    }


__all__ = ["classify_workspace_root"]
