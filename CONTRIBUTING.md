# Contributing to TORQ CLI

TORQ CLI is a governed multi-provider agent runner. Because it handles
credentials, runs untrusted model output under process containment, and emits
cryptographically signed evidence, the contribution bar is intentionally higher
than a typical Python project. This document describes how to contribute
without weakening those guarantees.

## Repository expectations

- **Never commit secrets.** Run-local Ed25519 signing seeds live under
  `.torq-run-identities/` and are gitignored. If you believe a private key has
  been committed, stop and contact a maintainer immediately.
- **Default to dry-run.** The tool's own rule — *dry-run is the default* —
  applies to development too: exercise changes against the bundled
  `tests/fixtures/fake_owned_provider.py` rather than live providers whenever
  possible.
- **No agent-authored commits.** Consistent with the product's security model,
  agents (including this one) do not commit, push, or merge. A human authorizes
  every change.

## Local setup

```bash
python -m pip install -e ".[dev]"   # installs runtime + dev extras, editable
python -m ruff check src tests
python -m mypy src
python -m pytest -q
```

`python -m pytest` works from a clean checkout because `src/` is on the pytest
path; you do not need a separate install step to run the suite (CI installs
anyway for the wheel-smoke gate).

## The contribution gate

Every pull request must keep these green — they mirror `.github/workflows/ci.yml`:

| Gate | Command | What it catches |
|---|---|---|
| Lint | `python -m ruff check src tests` | style + (with `S`) bandit security findings |
| Types | `python -m mypy src` | strict type errors across all source files |
| Tests | `python -m pytest -q` | behavioral + hermeticity + contract tests |
| Mutation | `python scripts/run_named_mutants.py` | defeats "tests pass but the security check is bypassed" |
| Build | `python -m build` | the wheel and sdist build cleanly |
| Wheel smoke | `python scripts/wheel_smoke.py dist` | the built wheel imports and runs |

CI runs this matrix on **Windows, macOS, and Linux** (Python 3.11/3.12/3.13)
with `TORQ_TEST_NETWORK_MODE=deny`. Tests must be hermetic: see
`tests/conftest.py`, which rejects non-loopback network egress.

### Hermeticity is enforced, not optional

`tests/conftest.py` monkeypatches `socket.getaddrinfo` to deny non-loopback
hosts and validates `TORQ_TEST_NETWORK_MODE=deny` at session start. A test that
reaches for a real provider hostname or the public internet will fail. Use the
fake owned-provider fixture for provider-shaped behavior.

## Branch and commit model

- Branch from `main` using a typed prefix: `feat/`, `fix/`, `docs/`, `test/`,
  `refactor/`, `sec/`, or `ent/` (enterprise/infra).
- Keep commits focused and write conventional-style messages
  (`feat:`, `fix:`, `test:`, `docs:`, `sec:`). Reference issues or PRs in the
  body when relevant.
- Squash or rebase before merge so history reads as a coherent set of reviewed
  changes.

## When you change security-relevant code

Changes to any of the following require extra care and will receive additional
review:

- `src/torq_cli/safety/` (receipts, evidence broker, production trust)
- `src/torq_cli/adapters/` containment (`linux_cgroup*`, `windows_job`,
  `macos_containment`, `owned_stream`, `process`)
- `src/torq_cli/domain/hermetic.py` (protected-path and import isolation)
- credential handling (`headless_credentials`, `credential_evidence`,
  `external_env_credentials`)
- the Fleet HTTP surface (`application/fleet*.py`)

For these areas:

1. **Add or extend a test** that demonstrates the guarantee still holds. If you
   add a new security check, add a named mutant in `scripts/run_named_mutants.py`
   so the check cannot be silently removed.
2. **Update `SECURITY.md` and/or `docs/security/threat-model.md`** if the threat
   surface, residual risk, or trust boundary changes. Honesty about limits is a
   product feature here — do not soften a known limitation to make a change look
   cleaner.
3. **Do not introduce** `shell=True`, `eval`/`exec`, `pickle`, or
   `yaml.load` (unsafe). The codebase is clean of these today; `yaml.safe_load`
   and argv-list `subprocess` are the only forms allowed.

## Adding a governed role profile

Role profiles are validated against a closed schema
(`src/torq_cli/domain/registry_schema.py`). To add one:

1. Add the profile under `src/torq_cli/data/registry/v1/` with a prompt
   manifest.
2. Run `python -m pytest tests/test_registry_schema.py -q` to confirm it passes
   the closed-config / forbidden-key / depth-and-event-limit validators.
3. Document the role's scope and its evidence writer permission in the
   relevant `docs/architecture/` note.

## Reporting security issues

Do not open a public issue for security vulnerabilities. See `SECURITY.md` for
the threat model and disclosure expectations, and report sensitive findings to
the maintainers privately.

## Sign-off

By submitting, you agree your contributions are licensed under the Apache-2.0
terms in `LICENSE`. A `Signed-off-by:` line (DCO style) is appreciated but not
currently required; this may become mandatory before a 1.0 release.
