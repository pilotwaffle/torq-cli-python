"""Run the approved security/governance mutants in isolated copies.

M01-M14 cover configuration, registry, and hermeticity. M15-M18 cover the
schema-v2 evidence-authority guards added during Fleet Release 0 hardening.
M19-M23 cover the rest of the evidence layer: the signing encoder (distinct
from the sanitizer M15 covers), lane state projection, monetary accounting, and
Windows binary-write fidelity. M24-M28 cover the verifier-side prose bounds that
keep operator content out of receipt bodies: the per-key command value schema,
the bounded MIME token, the bounded action-receipt labels, the run_decision key
allowlist, and the shared extra-key refusal. M29 covers the oversize floor
applied to every receipt transition, and M30 covers the orchestrator
run-planned -> observed transition rule.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MUTANT_ROOT = ROOT / "tmp" / "named-mutants"


@dataclass(frozen=True)
class Mutation:
    identifier: str
    relative_file: str
    before: str
    after: str
    target: str
    platforms: tuple[str, ...] = ()

    def applies_here(self) -> bool:
        return not self.platforms or os.name in self.platforms


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
    Mutation(
        "M15",
        "src/torq_cli/safety/receipts.py",
        "json.dumps(payload, sort_keys=True, allow_nan=False)",
        "json.dumps(payload, sort_keys=True, allow_nan=True)",
        "tests/test_fleet_run_contracts.py::test_non_finite_payload_is_a_pre_append_finding",
    ),
    Mutation(
        "M16",
        "src/torq_cli/domain/evidence_transitions.py",
        "if transition == \"run_decision\" and any(\n            candidate.writer_role == writer_role",
        "if False and any(\n            candidate.writer_role == writer_role",
        "tests/test_fleet_run_contracts.py::test_invented_orchestrator_decision_status_is_rejected_before_append",
    ),
    Mutation(
        "M17",
        "src/torq_cli/domain/run_evidence.py",
        "if waiting_on_operator or open_actions:",
        "if False or open_actions:",
        "tests/test_fleet_run_contracts.py::test_awaiting_approval_state_blocks_recovery_after_action_resolution",
    ),
    Mutation(
        "M18",
        "src/torq_cli/domain/run_evidence.py",
        "if waiting_on_operator or open_actions:",
        "if waiting_on_operator or False:",
        "tests/test_fleet_run_contracts.py::test_open_operator_action_blocks_recovery_abandonment",
    ),
    Mutation(
        "M19",
        "src/torq_cli/core/canonical_json.py",
        "        ensure_ascii=True,\n        allow_nan=False,",
        "        ensure_ascii=True,\n        allow_nan=True,",
        "tests/test_evidence_encoder_contract.py::test_signing_encoder_refuses_non_finite_floats",
    ),
    Mutation(
        "M20",
        "src/torq_cli/core/canonical_json.py",
        "        ensure_ascii=True,\n        allow_nan=False,",
        "        ensure_ascii=False,\n        allow_nan=False,",
        "tests/test_evidence_encoder_contract.py::test_signing_encoder_escapes_non_ascii",
    ),
    Mutation(
        "M21",
        "src/torq_cli/application/fleet.py",
        '            row["state"] = "blocked"\n            attempt["state"] = "blocked"',
        '            row["state"] = "needs_you"\n            attempt["state"] = "needs_you"',
        "tests/test_fleet_backend.py::test_completed_and_blocked_lanes_project_receipt_backed_values",
    ),
    Mutation(
        "M22",
        "src/torq_cli/safety/usage.py",
        "amount = Decimal(str(value))",
        "amount = Decimal(float(value))",
        "tests/test_phase4_safety.py::test_usage_summary_reconstructs_totals_and_preserves_unreported",
    ),
    Mutation(
        "M23",
        "src/torq_cli/safety/receipts.py",
        '_BINARY = getattr(os, "O_BINARY", 0)',
        "_BINARY = 0",
        "tests/test_fleet_release0.py::test_atomic_binary_write_preserves_ciphertext_newlines",
        ("nt",),
    ),
    Mutation(
        "M24",
        "src/torq_cli/domain/run_evidence.py",
        "        if not legacy and not _command_values_ok(payload):\n            return \"command_accept_invalid\"",
        "        if False:\n            return \"command_accept_invalid\"",
        "tests/test_receipt_prose_bounds.py::test_command_accepted_rejects_prose_shaped_values",
    ),
    Mutation(
        "M25",
        "src/torq_cli/domain/run_evidence.py",
        '        ("media_type", is_bounded_media_type(payload.get("media_type"))),',
        '        ("media_type", isinstance(payload.get("media_type"), str)),',
        "tests/test_receipt_prose_bounds.py::test_command_accepted_rejects_prose_shaped_values",
    ),
    Mutation(
        "M26",
        "src/torq_cli/domain/run_evidence.py",
        "            \"summary\",\n        )\n        if any(\n            not (\n                isinstance(payload.get(field), str)\n                if legacy\n                else _bounded_label(payload.get(field))\n            )\n            for field in required\n        ):",
        "            \"summary\",\n        )\n        if False:",
        "tests/test_receipt_prose_bounds.py::test_action_opened_rejects_unbounded_summary",
    ),
    Mutation(
        "M27",
        "src/torq_cli/domain/run_evidence.py",
        "    if _extra_keys(payload, RUN_DECISION_KEYS):\n        return \"run_decision_text_invalid\"",
        "    if False:\n        return \"run_decision_text_invalid\"",
        "tests/test_receipt_prose_bounds.py::test_run_decision_rejects_prose_and_undeclared_keys",
    ),
    Mutation(
        "M28",
        "src/torq_cli/domain/run_evidence.py",
        "def _extra_keys(payload: Mapping[str, Any], allowed: frozenset[str]) -> bool:\n    \"\"\"True when the payload carries a key outside the allowed set.\"\"\"\n    return bool(set(payload) - allowed)",
        "def _extra_keys(payload: Mapping[str, Any], allowed: frozenset[str]) -> bool:\n    \"\"\"True when the payload carries a key outside the allowed set.\"\"\"\n    return False",
        "tests/test_receipt_prose_bounds.py::test_command_accepted_rejects_an_undeclared_key",
    ),
    Mutation(
        "M29",
        "src/torq_cli/domain/run_evidence.py",
        "    if not legacy and _oversized_value(dict(payload)):\n        return \"receipt_value_oversized\"",
        "    if False:\n        return \"receipt_value_oversized\"",
        "tests/test_receipt_prose_bounds.py::test_oversize_floor_applies_to_every_transition",
    ),
    Mutation(
        "M30",
        "src/torq_cli/domain/evidence_transitions.py",
        'TransitionRule("orchestrator", "run_planned", "observed", "run_not_planned")',
        'TransitionRule("orchestrator", "run_planned", "observed", "mutated_precondition")',
        "tests/test_fleet_conformance_corpus.py::test_generated_corpus_is_byte_reproducible",
    ),
)


def _apply(root: Path, mutation: Mutation) -> None:
    source_path = root / mutation.relative_file
    source = source_path.read_text(encoding="utf-8")
    if source.count(mutation.before) != 1:
        raise RuntimeError(f"{mutation.identifier}: transformation occurrence was not exactly one")
    mutated = source.replace(mutation.before, mutation.after, 1)
    source_path.write_text(mutated, encoding="utf-8")


# Per-worktree pytest configuration. The mutant worktrees live beneath
# ROOT/tmp/named-mutants, which is *inside* the parent repository, so pytest's
# upward config discovery would otherwise find the parent pyproject.toml. That
# parent config declares `[tool.pytest.ini_options] pythonpath = ["src"]`,
# pointing at the parent's *unmutated* src, and pytest would import the
# unmutated module instead of the mutant copy — making every mutant appear to
# "survive" against code that was never actually mutated. A dedicated config
# file in the worktree, selected with `pytest -c`, pins rootdir to the worktree
# and stops upward discovery from reaching the parent.
_WORKTREE_PYTEST_INI = """\
[pytest]
pythonpath = src
testpaths = tests
addopts = -q -p no:cacheprovider
"""

# A sitecustomize shim written into each worktree at <worktree>/src/. Python
# auto-imports `sitecustomize` at interpreter startup from any sys.path dir, and
# the harness runs with PYTHONPATH=<worktree>/src, so this runs before pytest.
# It forces the worktree's `src` to the front of sys.path and drops every OTHER
# sys.path entry that holds a `torq_cli` package, so an editable install (.pth),
# a stray PYTHONPATH, or pytest's own path manipulation cannot shadow the mutant
# copy with the parent repo's unmutated `src`.
#
# A meta_path finder that called importlib.util.find_spec() was tried and
# rejected: find_spec() re-enters the finder at sys.meta_path[0], recursing
# infinitely during collection (RecursionError, pytest rc 4), which the harness
# mistook for a kill. The sys.path filter below is sufficient AND non-recursive.
_WORKTREE_SITECUSTOMIZE = '''\
"""Mutant isolation guard — keep the mutant worktree authoritative on sys.path."""
import os
import sys

# __file__ is <worktree>/src/sitecustomize.py; the worktree src dir is its parent.
_WORKTREE_SRC = os.path.dirname(os.path.abspath(__file__))
if _WORKTREE_SRC not in sys.path:
    sys.path.insert(0, _WORKTREE_SRC)

# Drop any sys.path entry that holds a torq_cli package and is not our worktree,
# so an editable install or stray PYTHONPATH cannot shadow the mutant copy.
sys.path[:] = [
    p for p in sys.path
    if not (os.path.isdir(os.path.join(p, "torq_cli")) and os.path.abspath(p) != _WORKTREE_SRC)
]
'''


def _materialize_worktree(worktree: Path) -> None:
    """Write the isolation config and startup guard into a mutant worktree."""
    (worktree / "pytest.ini").write_text(_WORKTREE_PYTEST_INI, encoding="utf-8")
    # sitecustomize.py on the worktree src dir is auto-imported at startup once
    # that dir is on sys.path. We write it under src so PYTHONPATH=src loads it.
    (worktree / "src" / "sitecustomize.py").write_text(
        _WORKTREE_SITECUSTOMIZE, encoding="utf-8"
    )


def _run(root: Path, mutation: Mutation) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # The worktree's own pytest.ini (selected via -c) sets pythonpath=src; we
    # also set PYTHONPATH for non-pytest resolution (e.g. the sitecustomize guard
    # itself) and strip any inherited PYTEST_ADDOPTS so it cannot override -c.
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    config = str(root / "pytest.ini")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", config, "-q", mutation.target],
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
    applicable = tuple(mutation for mutation in MUTATIONS if mutation.applies_here())
    skipped = tuple(m.identifier for m in MUTATIONS if not m.applies_here())
    if skipped:
        print(f"named_mutants: skipping {', '.join(skipped)} (not observable on {os.name})")
    try:
        for mutation in applicable:
            with tempfile.TemporaryDirectory(dir=temporary_parent, prefix=f"{mutation.identifier}-") as directory:
                worktree = Path(directory)
                ignore = shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache"
                )
                shutil.copytree(ROOT / "src", worktree / "src", ignore=ignore)
                shutil.copytree(ROOT / "tests", worktree / "tests", ignore=ignore)
                _materialize_worktree(worktree)
                _apply(worktree, mutation)
                result = _run(worktree, mutation)
                if result.returncode == 0:
                    print(f"{mutation.identifier} survived")
                    print(result.stdout)
                    return 1
                killed += 1
        print(f"named_mutants: {killed}/{len(applicable)} killed")
        return 0 if killed == len(applicable) else 1
    finally:
        try:
            temporary_parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
