# T-05 Runtime, Repository, and Packaging Decision

Date: 2026-07-23

## Decisions

- **Runtime: Python 3.11+.** The T-02 audit found the MMH engine, redaction,
  Conductor graph compiler, and policy contracts extractable in Python. Only
  the small telemetry lifecycle requires a contract-preserving rebuild.
- **Repository: standalone.** `E:\Torq-CLI` is the intended standalone product
  home, consuming contract-preserving extracted code rather than living inside
  TORQ-CONSOLE. Before release it must become a real standalone Git repository;
  its current accidental containment by the unrelated `E:\` repository is not
  acceptable release provenance.
- **Distribution: pipx/uv tool** from a Python wheel on Windows, macOS, and
  Linux for v0.1. Platform-native installers/binaries are deferred until the
  SDK/CLI subprocess and keychain dependencies have stable freezing hooks.
- **Version source:** `[project].version` in `pyproject.toml`, read at runtime
  with `importlib.metadata.version("torq-cli")`. Source-tree fallback may use a
  single package constant that is tested equal to project metadata.

## Tags and release identity

A standalone repository uses signed tag `v0.1.0`. If the product is ever moved
inside TORQ-CONSOLE, the tag must be namespaced `torq-cli-v0.1.0` because the
Console already has its own version line. Release tooling must refuse an
unnamespaced CLI tag inside the Console repository.

## Platform implications

| OS | v0.1 channel | Credential implication | Subprocess implication |
| --- | --- | --- | --- |
| Windows | pipx or `uv tool install` wheel | `keyring`/native adapter must reach per-user Windows Credential Manager from the installed environment; roaming is not assumed | Claude/Codex/Grok executables are discovered only by an attended connector setup step, never at import time |
| macOS | pipx or uv tool wheel | Keychain access may require an attended permission prompt and code-signing context | SDK/CLI child processes inherit only the filtered run environment |
| Linux desktop | pipx or uv tool wheel | Secret Service is optional and must fail clearly if unavailable | CLI binaries are optional connector dependencies |
| Linux headless | pipx or uv tool wheel | encrypted-file fallback only, with explicit unlock channel and no plaintext persistence | no desktop bus is assumed; subprocess tree control uses process groups |

Wheel installation keeps Python SDK and keychain adapters replaceable and
testable across three OSes. PyInstaller-class binaries are not selected for
v0.1 because they complicate native keychain loading, provider SDK updates,
and subprocess discovery without improving the current operator workflow.
