"""Release contract and builder-contract tests (release trust pack R0/R0B).

These tests verify the machine-readable release contract and the fail-closed
behavior of scripts/release_build.py's pure verification functions. They do
not build artifacts; the workflow run exercises the full build path.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "release" / "v0.2.0" / "release-contract.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_build  # noqa: E402


def load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml must declare a version"
    return match.group(1)


def test_contract_schema_is_complete() -> None:
    contract = load_contract()
    for key in (
        "contract_schema_version",
        "package",
        "candidate",
        "intended_tag",
        "paths",
        "required_ci_jobs",
        "required_artifacts",
        "policies",
        "required_trust_disclaimer_tokens",
    ):
        assert key in contract, f"contract missing key: {key}"
    for field in ("distribution_name", "import_name", "version", "supported_python_versions"):
        assert field in contract["package"], f"contract package missing: {field}"


def test_contract_version_matches_pyproject_and_intended_tag() -> None:
    contract = load_contract()
    version = contract["package"]["version"]
    assert version == pyproject_version()
    assert contract["intended_tag"] == f"v{version}"


def test_contract_ci_jobs_are_the_eleven_job_matrix() -> None:
    contract = load_contract()
    jobs = contract["required_ci_jobs"]
    assert len(jobs) == 11
    assert "headless-linux-py311" in jobs
    assert "linux-systemd-experimental-evidence" in jobs
    for platform in ("linux", "macos", "windows"):
        for py in ("py311", "py312", "py313"):
            assert f"quality-{platform}-{py}" in jobs


def test_contract_artifact_names_match_metadata_convention() -> None:
    contract = load_contract()
    version = contract["package"]["version"]
    import_name = contract["package"]["import_name"]
    artifacts = contract["required_artifacts"]
    assert artifacts["wheel"] == f"{import_name}-{version}-py3-none-any.whl"
    assert artifacts["sdist"] == f"{import_name}-{version}.tar.gz"


def test_contract_forbids_publication_and_production_trust() -> None:
    policies = load_contract()["policies"]
    assert policies["pypi_publication_permitted"] is False
    assert policies["production_trust_claimed"] is False
    assert policies["tag_or_release_creation_permitted"] is False
    assert policies["dirty_tree_permitted"] is False
    assert policies["non_main_release_permitted"] is False
    assert policies["cross_commit_artifact_substitution_permitted"] is False


def test_release_notes_and_changelog_exist_for_contract() -> None:
    contract = load_contract()
    notes = REPO_ROOT / contract["paths"]["release_notes"]
    changelog = REPO_ROOT / contract["paths"]["changelog"]
    assert notes.is_file(), f"missing release notes: {notes}"
    assert changelog.is_file(), f"missing changelog: {changelog}"
    version = contract["package"]["version"]
    assert f"[{version}]" in changelog.read_text(encoding="utf-8")


def test_release_notes_carry_required_trust_disclaimers() -> None:
    contract = load_contract()
    notes = (REPO_ROOT / contract["paths"]["release_notes"]).read_text(encoding="utf-8")
    for token in contract["required_trust_disclaimer_tokens"]:
        assert token in notes, f"release notes missing trust disclaimer token: {token}"


def test_expected_sha_validation_rejects_malformed_input() -> None:
    for bad in ("", "abc", "g" * 40, "A" * 40, "0" * 39, "0" * 41, "0" * 40 + " "):
        with pytest.raises(release_build.ReleaseContractError, match="candidate_sha_invalid_format"):
            release_build.validate_expected_sha(bad)


def test_expected_sha_validation_accepts_canonical_sha() -> None:
    assert release_build.validate_expected_sha("0" * 40) == "0" * 40


def test_source_state_fails_closed_on_head_mismatch() -> None:
    contract = load_contract()
    with pytest.raises(release_build.ReleaseContractError, match="candidate_sha_not_head"):
        release_build.verify_source_state(
            contract, "a" * 40, head_sha="b" * 40, porcelain="", on_main=True
        )


def test_source_state_fails_closed_on_dirty_tree() -> None:
    contract = load_contract()
    sha = "c" * 40
    with pytest.raises(release_build.ReleaseContractError, match="working_tree_dirty"):
        release_build.verify_source_state(
            contract, sha, head_sha=sha, porcelain=" M pyproject.toml", on_main=True
        )


def test_source_state_fails_closed_on_contract_sha_mismatch() -> None:
    contract = load_contract()
    contract = json.loads(json.dumps(contract))
    contract["candidate"]["sha"] = "d" * 40
    sha = "e" * 40
    with pytest.raises(
        release_build.ReleaseContractError, match="candidate_sha_contract_mismatch"
    ):
        release_build.verify_source_state(
            contract, sha, head_sha=sha, porcelain="", on_main=True
        )


def test_source_state_fails_closed_when_not_on_protected_main() -> None:
    contract = load_contract()
    sha = "f" * 40
    with pytest.raises(
        release_build.ReleaseContractError, match="candidate_sha_not_on_protected_main"
    ):
        release_build.verify_source_state(
            contract, sha, head_sha=sha, porcelain="", on_main=False
        )


def test_metadata_field_parser() -> None:
    text = "Metadata-Version: 2.4\nName: torq-cli\nVersion: 0.2.0\n"
    assert release_build._metadata_field(text, "Name") == "torq-cli"
    assert release_build._metadata_field(text, "Version") == "0.2.0"
    assert release_build._metadata_field(text, "Summary") == ""


def test_release_build_workflow_is_sha_pinned_and_non_publishing() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-build.yml").read_text(
        encoding="utf-8"
    )
    refs = re.findall(r"^\s*-?\s*uses:\s*(?P<ref>[^#\s]+)", workflow, re.MULTILINE)
    assert refs, "release-build workflow must reference actions"
    pin = re.compile(r"^[A-Za-z0-9_.\-/]+@[0-9a-f]{40}$")
    for ref in refs:
        assert pin.fullmatch(ref), f"unpinned action reference: {ref}"
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow, "release-build must not run on a schedule"
