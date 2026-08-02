"""Regression: the mutation harness must import the *mutant* copy, not the parent.

The mutant worktrees live beneath ``ROOT/tmp/named-mutants``, which is inside
the parent repository. ``pyproject.toml`` declares
``[tool.pytest.ini_options] pythonpath = ["src"]``, so without explicit
isolation pytest discovers the parent config, prepends the parent's unmutated
``src``, and every mutant "survives" against code that was never mutated.

This test exercises the harness's REAL isolation machinery
(``scripts.run_named_mutants._materialize_worktree``) — not a stand-in — so a
defect in the shipped ``sitecustomize.py`` / ``pytest.ini`` template (e.g. the
infinite-recursion meta_path finder that previously crashed collection and was
mistaken for a kill) is caught here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# The harness lives in scripts/ which is not on sys.path by default.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import run_named_mutants as _harness  # noqa: E402


def _write_parent_repo(parent: Path) -> None:
    """A parent repo with pythonpath=['src'] and a trivial torq_cli package."""
    (parent / "pyproject.toml").write_text(
        "[project]\nname = 'parent'\nversion = '0'\n"
        "[tool.pytest.ini_options]\npythonpath = ['src']\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    pkg = parent / "src" / "torq_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # The "unmutated" marker: parent's value is PARENT.
    (pkg / "marker.py").write_text('VALUE = "PARENT"\n', encoding="utf-8")


def _write_mutant_worktree(worktree: Path) -> None:
    """A mutant worktree beneath the parent; its marker value is MUTANT.

    Uses the harness's real ``_materialize_worktree`` so the regression covers
    the exact isolation files that ship, including ``sitecustomize.py``.
    """
    pkg = worktree / "src" / "torq_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "marker.py").write_text('VALUE = "MUTANT"\n', encoding="utf-8")
    tests = worktree / "tests"
    tests.mkdir()
    (tests / "test_marker.py").write_text(
        "from torq_cli.marker import VALUE\n"
        "def test_marker_is_mutant():\n"
        "    assert VALUE == 'MUTANT', f'imported wrong copy: {VALUE}'\n",
        encoding="utf-8",
    )
    # Materialize the EXACT isolation files the harness writes per worktree.
    _harness._materialize_worktree(worktree)


def _run_pytest(worktree: Path, *, isolate: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(worktree / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    args = [sys.executable, "-m", "pytest", "-q", "tests/test_marker.py"]
    if isolate:
        # The harness's invocation: explicit -c pins rootdir to the worktree.
        args[3:3] = ["-c", str(worktree / "pytest.ini")]
    return subprocess.run(
        args, cwd=worktree, env=env, capture_output=True, text=True, check=False,
    )


def test_mutant_worktree_imports_mutant_not_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent-repo"
    parent.mkdir()
    _write_parent_repo(parent)

    # Worktree BENEATH the parent, mirroring the real tmp/named-mutants layout
    # so pytest's upward config discovery would otherwise find the parent.
    worktree = parent / "tmp" / "named-mutants" / "M-regression"
    worktree.mkdir(parents=True)
    _write_mutant_worktree(worktree)

    # Sanity: the harness actually wrote its isolation files.
    assert (worktree / "pytest.ini").is_file(), "harness did not materialize pytest.ini"
    assert (worktree / "src" / "sitecustomize.py").is_file(), (
        "harness did not materialize sitecustomize.py"
    )

    isolated = _run_pytest(worktree, isolate=True)
    # Isolation works -> mutant copy imported -> test passes (rc 0).
    assert isolated.returncode == 0, (
        "mutant worktree imported the PARENT copy instead of the mutant, OR the "
        "harness isolation crashed. Isolation regressed.\n"
        f"returncode: {isolated.returncode}\nstdout:\n{isolated.stdout}\n"
        f"stderr:\n{isolated.stderr}"
    )
    # No recursion crash from a defective sitecustomize (regression for the
    # _OriginGuard infinite-recursion bug that previously produced rc 4).
    assert "RecursionError" not in isolated.stdout + isolated.stderr, (
        "isolation sitecustomize recursed — collection crashed.\n"
        f"stdout:\n{isolated.stdout}\nstderr:\n{isolated.stderr}"
    )


def test_without_isolation_the_parent_leaks(tmp_path: Path) -> None:
    """Negative control: prove the isolation is actually necessary.

    If the parent config does NOT leak (e.g. on a pytest version that ignores
    upward discovery), this test is the signal that the regression no longer
    reproduces the original bug and the isolation may be redundant. It must NOT
    silently pass — it must fail loudly so the harness's necessity is re-justified.
    """
    parent = tmp_path / "parent-repo"
    parent.mkdir()
    _write_parent_repo(parent)
    worktree = parent / "tmp" / "named-mutants" / "M-control"
    worktree.mkdir(parents=True)
    _write_mutant_worktree(worktree)
    # Remove the harness isolation so only the parent's pyproject.toml can govern.
    (worktree / "pytest.ini").unlink()
    (worktree / "src" / "sitecustomize.py").unlink()

    leaked = _run_pytest(worktree, isolate=False)
    # We expect the parent to leak and the mutant test to FAIL (rc != 0). If it
    # passes (rc 0) the isolation isn't doing anything observable and the
    # regression test's premise must be revisited.
    assert leaked.returncode != 0, (
        "Without isolation the mutant test still passed — the parent config did "
        "NOT leak on this pytest version, so this regression test cannot prove "
        "isolation is necessary. Revisit test_named_mutants_isolation."
    )
