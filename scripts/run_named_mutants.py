"""Run the fourteen approved security/governance mutants in isolated copies."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MUTANT_ROOT = Path(r"E:\TORQ_CLI_EVIDENCE\t06b-20260718\luna-builder-evidence\named-mutants-tmp")


@dataclass(frozen=True)
class Mutation:
    identifier: str
    relative_file: str
    before: str
    after: str
    target: str


MUTATIONS = (
    Mutation(
        "M01", "src/torq_cli/domain/config_schema.py",
        'findings.append(FindingCatalog.make("config_schema_invalid", path=child_path))',
        "pass",
        "tests/test_config_schema.py::test_unknown_key_rejected",
    ),
    Mutation("M02", "src/torq_cli/domain/config_schema.py", "_CRED_REF.fullmatch(value)", "_CRED_REF.search(value)", "tests/test_config_schema.py::test_malformed_credential_ref_rejected"),
    Mutation(
        "M03", "src/torq_cli/domain/config_schema.py",
        'findings.append(FindingCatalog.make("binding_override_forbidden", path=f"/binding_overrides/{role_id}"))',
        "pass",
        "tests/test_config_schema.py::test_provider_override_rejected",
    ),
    Mutation("M04", "src/torq_cli/application/offline_status.py", '"runtime_state": "offline_unattested"', '"runtime_state": "runtime_effective"', "tests/test_resolution.py::test_offline_never_effective"),
    Mutation("M05", "src/torq_cli/interfaces/cli.py", "return 4", "return 0", "tests/test_cli.py::test_require_effective_exits_four"),
    Mutation("M06", "src/torq_cli/domain/hermetic.py", 'raise ProtectedPathError("protected path access denied")', "return", "tests/test_hermetic.py::test_protected_path_denied_before_read"),
    Mutation(
        "M07", "src/torq_cli/domain/config_schema.py",
        'if version > 1:\n        return [FindingCatalog.make("config_version_unsupported", path="/config_version")]',
        "if version > 1:\n        return []",
        "tests/test_config_schema.py::test_future_version_rejected",
    ),
    Mutation("M08", "src/torq_cli/domain/registry_schema.py", 'findings.append("profile_version_unknown")', "pass", "tests/test_registry_schema.py::test_unknown_profile_version_rejected"),
    Mutation(
        "M09", "src/torq_cli/domain/registry_schema.py",
        'if binding.model_id.startswith("glm") and binding.role_id != "refine_ui":\n        return False',
        "if False:\n        return False",
        "tests/test_registry_schema.py::test_glm_builder_rejected",
    ),
    Mutation("M10", "src/torq_cli/domain/registry_schema.py", "return edge in EXPECTED_TRANSITIONS", "return True", "tests/test_registry_schema.py::test_invalid_transition_rejected"),
    Mutation("M11", "src/torq_cli/domain/drift_oracle.py", 'return _resource(name).read_bytes()', 'Path("E:/TORQ-CONSOLE/torq_console/conductor/runner/role_map.py").read_text()', "tests/test_hermetic.py::test_oracle_has_no_upstream_worktree_read"),
    Mutation("M12", "src/torq_cli/domain/hermetic.py", '"os", "subprocess", "socket"', '"os", "socket"', "tests/test_hermetic.py::test_production_imports_forbid_subprocess"),
    Mutation("M13", "src/torq_cli/application/resolve.py", "config = parse_config_text(text)", "config = yaml.safe_load(text)", "tests/test_resolution.py::test_duplicate_yaml_mapping_is_rejected_before_schema_validation"),
    Mutation("M14", "src/torq_cli/domain/config_schema.py", "if identity in identities:\n            _parser_fail()", "if False:\n            _parser_fail()", "tests/test_config_schema.py::test_nfc_equivalent_duplicate_mapping_keys_are_parser_invalid"),
)


def _apply(root: Path, mutation: Mutation) -> None:
    source_path = root / mutation.relative_file
    source = source_path.read_text(encoding="utf-8")
    if source.count(mutation.before) != 1:
        raise RuntimeError(f"{mutation.identifier}: transformation occurrence was not exactly one")
    mutated = source.replace(mutation.before, mutation.after, 1)
    source_path.write_text(mutated, encoding="utf-8")


def _run(root: Path, mutation: Mutation) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", mutation.target],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    temporary_parent = Path(os.environ.get("TORQ_T06B_MUTANT_ROOT", str(DEFAULT_MUTANT_ROOT)))
    temporary_parent.mkdir(parents=True, exist_ok=True)
    killed = 0
    try:
        for mutation in MUTATIONS:
            with tempfile.TemporaryDirectory(dir=temporary_parent, prefix=f"{mutation.identifier}-") as directory:
                worktree = Path(directory)
                ignore = shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache"
                )
                shutil.copytree(ROOT / "src", worktree / "src", ignore=ignore)
                shutil.copytree(ROOT / "tests", worktree / "tests", ignore=ignore)
                _apply(worktree, mutation)
                result = _run(worktree, mutation)
                if result.returncode == 0:
                    print(f"{mutation.identifier} survived")
                    print(result.stdout)
                    return 1
                killed += 1
        print(f"named_mutants: {killed}/14 killed")
        return 0 if killed == 14 else 1
    finally:
        try:
            temporary_parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
