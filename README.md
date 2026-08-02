# TORQ CLI

TORQ CLI 0.2.0 is a standalone Python 3.11–3.13 governed agent runner. It validates immutable role profiles, connects providers through fail-closed adapters, runs provider processes under OS-enforced containment, records tamper-resistant evidence, and exposes an evidence-backed Fleet control and attended chat surface.

```text
torq profile validate --config PATH
torq status --offline --config PATH [--require-effective]
torq status --config PATH --require-effective --runtime ATTESTATION.json
torq config import-v5-normalized --config ABSOLUTE_PATH
torq config import-v5-console --config ABSOLUTE_PATH
torq auth status --credential-file E:\TORQ-CONSOLE\.env
torq auth store --provider deepseek --credential-ref credref_<32-lowercase-hex>
torq auth verify-access --provider deepseek --credential-ref credref_<32-lowercase-hex>
torq auth revoke --provider deepseek --credential-ref credref_<32-lowercase-hex>
torq auth store --provider deepseek --credential-ref credref_<32-lowercase-hex> --backend headless_encrypted_file --store-root ABSOLUTE_PATH
torq harness inspect --expected PROFILE.json --actual LIVE.json
torq setup --config .torq/config.yaml --answers examples/torq-v5-6-live.answers.json --credential-file E:\TORQ-CONSOLE\.env
torq run --goal "..." --run-root RUNS --identity ID.json --expected PROFILE.json --actual LIVE.json
torq run --goal "..." --run-root RUNS --identity ID.json --expected PROFILE.json --actual LIVE.json --config .torq/config.yaml --live --allow-live --policy-allow-live
torq evidence verify --run-root RUN_DIRECTORY
torq fleet --run-root RUN_DIRECTORY --serve
torq fleet --run-root RUN_DIRECTORY --serve --chat-provider deepseek --chat-model deepseek-v4-pro --credential-file ABSOLUTE_ENV_PATH
torq trust readiness
torq --version
```

Dry-run is the default. Live execution requires both `--allow-live` and `--policy-allow-live`. Agents never commit, push, or merge. The primary worktree remains unchanged until an audited, tree-pinned proposal receives explicit approval.

`torq run` invokes the governed orchestration boundary. Dry-run records the
four-stage plan without provider calls. An injected dispatcher remains available
for embedding and tests. The installed live command loads the validated saved
config, resolves its explicit credential source, and constructs the persistent
entitlement ledger before executing G1D -> G1R -> Builder -> G2A. It routes
HIGH defects through the bound repair lane, and performs a targeted G2A
re-audit before returning `awaiting_approval`. Invalid config, credentials,
regional routing, or missing transport binaries fail before a run directory is
created. See `docs/architecture/governed-orchestration.md`.

Process-backed Claude-compatible live stages use the owned-process boundary and
are production-enabled only on Windows. Linux refuses them with
`distinct_identity_system_broker_required`; macOS refuses them with
`owned_process_strong_containment_unavailable`. OpenAI's direct HTTPS adapter
does not launch a provider process and does not inherit ambient proxy settings.

The T-06A import command reads only the authenticated normalized V5 fixture shape and emits a fixed registry-authoritative stdout projection. It does not read raw Console configuration, write files, resolve credentials, access providers, or claim T-06/Phase 1 completion.

The T-06C Console import command accepts the bounded raw Console V5 YAML shape and emits the same canonical `torq-v5-repo-compat` v1 projection without manual translation. It is read-only: it does not discover a default Console path, write configuration, copy endpoints or wrapper names, resolve credentials, or access providers.

`status --offline` is intentionally `offline_unattested`; effective status requires a runtime attestation. Installation instructions are in `docs/install.md`, and the security/threat model is in `SECURITY.md`.

The optional `--credential-file` compatibility source reuses an explicit
external provider env file without copying its values into TORQ configuration.
For the Console harness it maps `QWEN_TOKEN_PLAN_API_KEY` (which covers both
the Qwen and DeepSeek lanes, since both bill to one Alibaba Token Plan),
`KIMI_CODE_API_KEY` (falling back to `KIMI_API_KEY`), and `GLM_API_KEY` into
isolated Claude-compatible child environments. See
`docs/external-credential-source.md`.
External env files are accepted only when already owner-only: POSIX mode `0600`
or a Windows DACL containing only the owner. TORQ does not repair a permissive
secret file. Token Plan routing accepts only the canonical
`https://token-plan.<region>.maas.aliyuncs.com/apps/anthropic` shape, with no
userinfo, query, or fragment.

Native provider credentials are stored through `keyring` 25.7 in the current
user's Windows Credential Manager, macOS Keychain, or Linux Secret Service.
`auth store` accepts the value only from an attended no-echo terminal and
refuses redirected input. Configuration contains only opaque
`credref_<32-lowercase-hex>` handles. `auth verify-access` deliberately resolves
the selected record inside TORQ but emits only coarse success/failure state; it
does not call a provider or print the value. The attended headless encrypted-
file backend is implemented with an explicit absolute `--store-root`. It uses a
no-echo passphrase prompt and never acts as an automatic fallback.

Fleet chat currently accepts direct-provider credentials only from an explicit
absolute `--credential-file`; it does not resolve the configured platform or
headless vault. The browser receives neither credentials nor process handles.
Windows is the only production governed-chat platform: Job Objects own and
confirm termination of the provider tree. Linux fails closed with
`distinct_identity_system_broker_required`; its per-user systemd/cgroup-v2
adapter is experimental evidence only. macOS fails closed with
`owned_process_strong_containment_unavailable` pending a separately signed and
notarized native containment product.

Local signing and the same-volume manifest anchor are tamper-resistant, not
production-trust hardened. `torq trust readiness` reports
`production_signing_identity_exportable` and
`production_receipt_anchor_not_independent` until a non-exportable platform
signer and independently operated remote transparency service are integrated.

Local mock/conformance results do not prove provider effectiveness, hosted
multi-OS CI, clean-machine installation, branch protection, or release
readiness. The v0.2.0 candidate requires fresh protected-main CI and release
evidence; this source tree does not claim those gates have run.
