# TORQ CLI

TORQ CLI 0.1.0 is a standalone Python 3.11+ governed agent runner. It validates immutable role profiles, connects providers through fail-closed adapters, executes work in an isolated sandbox, records tamper-resistant evidence, and applies an audited proposal only after explicit operator approval.

```text
torq profile validate --config PATH
torq status --offline --config PATH [--require-effective]
torq status --config PATH --require-effective --runtime ATTESTATION.json
torq config import-v5-normalized --config ABSOLUTE_PATH
torq config import-v5-console --config ABSOLUTE_PATH
torq auth status --credential-file E:\TORQ-CONSOLE\.env
torq harness inspect --expected PROFILE.json --actual LIVE.json
torq setup --config .torq/config.yaml --answers examples/torq-v5-6-live.answers.json --credential-file E:\TORQ-CONSOLE\.env
torq run --goal "..." --run-root RUNS --identity ID.json --expected PROFILE.json --actual LIVE.json
torq evidence verify --run-root RUN_DIRECTORY
torq --version
```

Dry-run is the default. Live execution requires both `--allow-live` and `--policy-allow-live`. Agents never commit, push, or merge. The primary worktree remains unchanged until an audited, tree-pinned proposal receives explicit approval.

`torq run` now invokes the governed orchestration boundary. Dry-run records the
four-stage plan without provider calls. A live application embedding an
injected `ConnectorDispatcher` executes G1D -> G1R -> Builder -> G2A, routes
HIGH defects through the bound repair lane, and performs a targeted G2A
re-audit before returning `awaiting_approval`. The standalone CLI currently
has no production transport factory, so `--live` fails closed as
`live_dispatcher_required`; mock-transport conformance is not described as a
live provider run. See `docs/architecture/governed-orchestration.md`.

The T-06A import command reads only the authenticated normalized V5 fixture shape and emits a fixed registry-authoritative stdout projection. It does not read raw Console configuration, write files, resolve credentials, access providers, or claim T-06/Phase 1 completion.

The T-06C Console import command accepts the bounded raw Console V5 YAML shape and emits the same canonical `torq-v5-repo-compat` v1 projection without manual translation. It is read-only: it does not discover a default Console path, write configuration, copy endpoints or wrapper names, resolve credentials, or access providers.

`status --offline` is intentionally `offline_unattested`; effective status requires a runtime attestation. Installation instructions are in `docs/install.md`, and the security/threat model is in `SECURITY.md`.

The optional `--credential-file` compatibility source reuses an explicit
external provider env file without copying its values into TORQ configuration.
For the Console harness it maps `DEEPSEEK_API_KEY`, `KIMI_CODE_API_KEY`
(falling back to `KIMI_API_KEY`), and `GLM_API_KEY` into isolated
Claude-compatible child environments. See `docs/external-credential-source.md`.

Local mock/conformance results do not prove provider effectiveness, hosted multi-OS CI, clean-machine installation, branch protection, or release readiness. Those are separate release gates and are recorded honestly in the production-readiness audit.
