"""TORQ governed agent runner and Fleet control surface."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("torq-cli")
except PackageNotFoundError:
    # Source-tree fallback; release tooling verifies this against pyproject.toml.
    __version__ = "0.2.0"
