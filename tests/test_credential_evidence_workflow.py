"""Scoped hardening tests for credential-evidence.yml (release trust pack R0A).

These tests verify the workflow discipline declared in the workflow header:
full-SHA action pins, ``persist-credentials: false`` on every checkout, a
derived (never hardcoded) evidence date, and a wheel-manifest substitution
guard in every native evidence job. The workflow is parsed as text so the
test suite gains no YAML dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "credential-evidence.yml"
NATIVE_JOBS = ("native-macos", "native-windows", "native-linux")

USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>[^#\s]+)", re.MULTILINE)
FULL_SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_.\-/]+@[0-9a-f]{40}$")
HARDCODED_DATE_RE = re.compile(r"--date\s+['\"]?\d{4}-\d{2}-\d{2}['\"]?")
CHECKOUT_RE = re.compile(r"uses:\s*actions/checkout@")
JOB_HEADER_RE = re.compile(r"^  (?P<name>[a-z][a-z0-9-]*):\s*$", re.MULTILINE)


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_sections(text: str) -> dict[str, str]:
    headers = list(JOB_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        sections[header.group("name")] = text[header.start() : end]
    return sections


def test_workflow_uses_only_full_sha_pins() -> None:
    refs = USES_RE.findall(workflow_text())
    assert refs, "expected at least one action reference"
    for ref in refs:
        assert FULL_SHA_PIN_RE.fullmatch(ref), f"unpinned action reference: {ref}"


def test_every_checkout_disables_persisted_credentials() -> None:
    text = workflow_text()
    checkouts = [match.start() for match in CHECKOUT_RE.finditer(text)]
    assert checkouts, "expected at least one checkout step"
    for start in checkouts:
        block = text[start : start + 400]
        assert "persist-credentials: false" in block, (
            f"checkout near offset {start} must set persist-credentials: false"
        )


def test_no_hardcoded_evidence_dates() -> None:
    assert not HARDCODED_DATE_RE.search(workflow_text()), (
        "credential evidence workflow must not hardcode --date values"
    )


def test_every_native_job_derives_its_evidence_date() -> None:
    sections = job_sections(workflow_text())
    for job in NATIVE_JOBS:
        assert job in sections, f"missing job section: {job}"
        assert "id: evidence_date" in sections[job], (
            f"{job} must derive its evidence date from the candidate commit "
            "or a validated workflow input"
        )


def test_every_native_job_verifies_the_wheel_manifest() -> None:
    sections = job_sections(workflow_text())
    for job in NATIVE_JOBS:
        assert "Verify wheel manifest against this run" in sections[job], (
            f"{job} must verify the wheel manifest against its own run context "
            "before installing the wheel"
        )
