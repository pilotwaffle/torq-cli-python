"""Regression: the mutation harness must import the *mutant* copy, not the parent.

The mutant worktrees live beneath ``ROOT/tmp/named-mutants``, which is inside
the parent repository. ``pyproject.toml`` declares
``[tool.pytest.ini_options] pythonpath = ["src"]``, so without explicit
isolation pytest discovers the parent config, prepends the parent's unmutated
``src``, and every mutant "survives" against code that was never mutated. This
test reproduces that layout and asserts the mutant copy is the one imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path



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


def _write_mutant_worktree(worktree: Path, parent_src: Path) -> None:
    """A mutant worktree beneath the parent; its marker value is MUTANT."""
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
    # The isolation config + guard the real harness writes (see
    # scripts/run_named_mutants.py::_materialize_worktree). Keep this in sync.
    (worktree / "pytest.ini").write_text(
        "[pytest]\npythonpath = src\ntestpaths = tests\naddopts = -q -p no:cacheprovider\n",
        encoding="utf-8",
    )
    (worktree / "src" / "sitecustomize.py").write_text(
        "import os, sys\n"
        "_w = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')\n"
        "_w = os.path.abspath(_w)\n"
        "_src = os.path.join(_w, 'src')\n"
        "if _src not in sys.path:\n"
        "    sys.path.insert(0, _src)\n"
        "sys.path[:] = [p for p in sys.path if not (os.path.isdir(os.path.join(p, 'torq_cli')) and os.path.abspath(p) != _src)]\n",
        encoding="utf-8",
    )


def test_mutant_worktree_imports_mutant_not_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent-repo"
    parent.mkdir()
    _write_parent_repo(parent)

    # Worktree BENEATH the parent, mirroring tmp/named-mutants layout.
    worktree = parent / "tmp" / "named-mutants" / "M-regression"
    worktree.mkdir(parents=True)
    _write_mutant_worktree(worktree, parent / "src")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(worktree / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)

    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-c", str(worktree / "pytest.ini"),
            "-q", "tests/test_marker.py",
        ],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # If isolation works, the mutant copy is imported and the test PASSES (rc 0).
    # If the parent config leaked, VALUE == "PARENT" and the test FAILS (rc 1).
    assert result.returncode == 0, (
        "mutant worktree imported the PARENT copy instead of the mutant — "
        f"isolation regressed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
