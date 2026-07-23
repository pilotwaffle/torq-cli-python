"""Install and exercise one built wheel in a clean virtual environment."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
from pathlib import Path


VALID_CONFIG = """config_version: 1
profile:
  id: torq-v5-6-live
  version: 1.0.0
binding_overrides: {}
connectors: {}
policy:
  independence_mode: profile_minimum
  unattestable_action: deny
  loop_budget: 1
  resource_limits:
    max_runtime_seconds: 60
    max_cost_cents: 100
    max_file_count: 10
    max_changed_lines: 100
"""


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv[1:]
    dist = Path(arguments[0] if arguments else "dist")
    wheels = sorted(dist.resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {dist}")
    parent = Path.cwd() / ".wheel-smoke-tmp"
    parent.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=parent, prefix="run-") as directory:
            root = Path(directory)
            venv_dir = root / "venv"
            venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            torq = venv_dir / ("Scripts/torq.exe" if sys.platform == "win32" else "bin/torq")
            fixture = root / "valid.yaml"
            fixture.write_text(VALID_CONFIG, encoding="utf-8")
            commands = (
                [str(python), "-m", "pip", "install", str(wheels[0])],
                [str(torq), "profile", "validate", "--config", str(fixture)],
                [str(torq), "status", "--offline", "--config", str(fixture)],
            )
            for command in commands:
                result = _run(command, root)
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
                    return result.returncode
            require = _run([str(torq), "status", "--offline", "--config", str(fixture), "--require-effective"], root)
            if require.returncode != 4:
                print(require.stdout)
                print(require.stderr)
                return 1
            invalid = root / "invalid.yaml"
            invalid.write_text("config_version: 2\n", encoding="utf-8")
            invalid_result = _run([str(torq), "profile", "validate", "--config", str(invalid)], root)
            if invalid_result.returncode != 2:
                print(invalid_result.stdout)
                print(invalid_result.stderr)
                return 1
            print("wheel_smoke: clean install and command checks passed")
            return 0
    finally:
        try:
            parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
