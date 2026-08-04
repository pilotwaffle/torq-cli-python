"""Release consistency gates (release trust pack R0C).

Fail-closed consistency checks across version metadata, release notes,
artifact naming, workflow pinning, and documentation links. Report-only
security metrics (coverage, mutants, dependency audit) live in the CI job
``report-only-metrics-linux`` and never gate here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CONTRACT_PATH = REPO_ROOT / "release" / "v0.2.0" / "release-contract.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_links  # noqa: E402
import release_build  # noqa: E402


def _contract() -> dict:
    import json

    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml must declare a version"
    return match.group(1)


def test_package_version_and_intended_tag_agree() -> None:
    contract = _contract()
    version = _pyproject_version()
    assert contract["package"]["version"] == version
    assert contract["intended_tag"] == f"v{version}"


def test_changelog_contains_contract_version() -> None:
    contract = _contract()
    changelog = (REPO_ROOT / contract["paths"]["changelog"]).read_text(encoding="utf-8")
    assert f"[{contract['package']['version']}]" in changelog


def test_release_notes_do_not_claim_pending_after_release_exists() -> None:
    contract = _contract()
    tag = contract["intended_tag"]
    listed = subprocess.run(
        ["git", "tag", "--list", tag],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    notes = (REPO_ROOT / contract["paths"]["release_notes"]).read_text(encoding="utf-8")
    if listed == tag:
        assert "release actions remain to be performed" not in notes, (
            f"release notes still claim pending actions although {tag} exists"
        )
        assert "Do not interpret this file as a tag" not in notes


def test_artifact_filenames_match_metadata_convention() -> None:
    contract = _contract()
    version = contract["package"]["version"]
    import_name = contract["package"]["import_name"]
    artifacts = contract["required_artifacts"]
    assert artifacts["wheel"] == f"{import_name}-{version}-py3-none-any.whl"
    assert artifacts["sdist"] == f"{import_name}-{version}.tar.gz"
    assert artifacts["sha256sums"] == "SHA256SUMS"


def test_all_workflows_use_only_full_sha_pins() -> None:
    pin = re.compile(r"^[A-Za-z0-9_.\-/]+@[0-9a-f]{40}$")
    uses = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>[^#\s]+)", re.MULTILINE)
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert workflows, "expected at least one workflow"
    for workflow in workflows:
        for ref in uses.findall(workflow.read_text(encoding="utf-8")):
            assert pin.fullmatch(ref), f"{workflow.name}: unpinned action reference: {ref}"


def test_credential_evidence_carries_no_stale_hardcoded_date() -> None:
    workflow = (WORKFLOWS_DIR / "credential-evidence.yml").read_text(encoding="utf-8")
    assert not re.search(r"--date\s+['\"]?\d{4}-\d{2}-\d{2}['\"]?", workflow)


def test_release_build_workflow_requires_expected_sha_and_cannot_self_schedule() -> None:
    workflow = (WORKFLOWS_DIR / "release-build.yml").read_text(encoding="utf-8")
    assert "expected_sha" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow


def test_sha256sums_gate_fails_closed(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(release_build.ReleaseContractError, match="artifact_hash_missing"):
        release_build.verify_sha256sums(tmp_path)


def test_sha256sums_gate_detects_tampered_bytes(tmp_path: Path) -> None:
    import pytest

    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"original-bytes")
    release_build.write_sha256sums(tmp_path, [artifact])
    artifact.write_bytes(b"tampered-bytes")
    with pytest.raises(release_build.ReleaseContractError, match="artifact_hash_mismatch"):
        release_build.verify_sha256sums(tmp_path)


def test_release_scope_document_links_resolve() -> None:
    failures: list[str] = []
    for path in check_links.iter_markdown_files(REPO_ROOT):
        if not check_links.is_release_scope(path):
            continue
        local_failures, _ = check_links.check_file(path)
        failures.extend(
            f"{path.relative_to(REPO_ROOT)}: {failure}" for failure in local_failures
        )
    assert not failures, "broken release-scope links:\n" + "\n".join(failures)


def test_link_checker_detects_broken_local_link(tmp_path: Path) -> None:
    document = tmp_path / "doc.md"
    document.write_text("[missing](does-not-exist.md)\n", encoding="utf-8")
    failures, _ = check_links.check_file(document)
    assert failures == ["broken local link: does-not-exist.md"]
