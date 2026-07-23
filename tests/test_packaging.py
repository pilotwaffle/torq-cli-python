from pathlib import Path

from scripts.run_named_mutants import MUTATIONS


def test_packaging_and_ci_artifacts_are_declared() -> None:
    root = Path(__file__).parents[1]

    assert (root / "pyproject.toml").is_file()
    assert (root / "scripts" / "wheel_smoke.py").is_file()
    assert (root / "scripts" / "run_named_mutants.py").is_file()
    assert (root / ".github" / "workflows" / "ci.yml").is_file()
    assert (root / "README.md").is_file()
    assert (root / "docs" / "architecture" / "extraction-viability-audit-draft.md").is_file()
    assert (root / "docs" / "architecture" / "credential-storage-requirements.md").is_file()


def test_console_import_fixture_is_declared_for_source_distribution() -> None:
    root = Path(__file__).parents[1]
    manifest = root / "MANIFEST.in"

    assert manifest.is_file()
    assert "include tests/fixtures/torq-console-v5-config.yaml" in manifest.read_text(encoding="utf-8")


def test_ci_declares_exact_four_required_jobs() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()

    for job in (
        "quality-windows-py311",
        "quality-macos-py311",
        "quality-linux-py311",
        "headless-linux-py311",
    ):
        assert job in workflow


def test_named_mutants_preserve_exact_m04_and_m11_contract() -> None:
    mutations = {mutation.identifier: mutation for mutation in MUTATIONS}

    assert mutations["M04"].relative_file == "src/torq_cli/application/offline_status.py"
    assert mutations["M04"].before == '"runtime_state": "offline_unattested"'
    assert mutations["M04"].after == '"runtime_state": "runtime_effective"'
    assert mutations["M04"].target == "tests/test_resolution.py::test_offline_never_effective"
    assert mutations["M11"].relative_file == "src/torq_cli/domain/drift_oracle.py"
    assert mutations["M11"].target == "tests/test_hermetic.py::test_oracle_has_no_upstream_worktree_read"
    assert "Path(\"E:/TORQ-CONSOLE/torq_console/conductor/runner/role_map.py\").read_text()" in mutations["M11"].after
