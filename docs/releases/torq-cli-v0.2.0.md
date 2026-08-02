# TORQ CLI 0.2.0 release candidate notes

Status: prepared from the merged baseline commit `8212efe` and its documentation
descendants.
Do not interpret this file as a tag, signature, GitHub release, protected-main
CI result, or published-artifact attestation. Those release actions remain to
be performed and recorded.

## Feature release

- Adds the installed, fail-closed `torq run --live` dispatcher factory with
  exact provider/model binding and persistent entitlement accounting.
- Adds an attended XChaCha20-Poly1305/Argon2id headless credential vault with
  explicit backend/root selection, bounded locking, rotation, revocation, and
  restrictive filesystem protections. There is no unattended or plaintext
  fallback.
- Adds evidence-backed Fleet control and governed interactive chat, including
  streaming, attachments, Stop/cancellation evidence, and usage accounting.
- Adds Windows production process-tree ownership with Job Objects.
- Routes process-backed Claude-compatible live stages through the same owned
  process boundary. They are production-enabled only on Windows; OpenAI direct
  HTTPS remains direct and excludes ambient proxy settings.
- Adds an experimental Linux user-systemd/cgroup-v2 evidence harness. Linux
  production chat remains unavailable because a distinct-identity system
  broker is required.
- Records the macOS native containment feasibility boundary. The ordinary
  Python wheel fails closed pending a signed/notarized native product.
- Adds a machine-readable production-trust readiness contract. The bundled
  local signer and same-volume anchor intentionally report not ready.

## Credential and trust boundaries

Governed `torq run --live` resolves the credential source explicitly declared
by saved configuration: external env file, platform keychain, or attended
headless vault. Fleet chat is narrower in v0.2.0: direct-provider chat requires
an explicit absolute `--credential-file` and does not read platform/headless
vault references.

Explicit external env files must already be owner-only (POSIX `0600` or a
Windows owner-only DACL). Token Plan base URLs are restricted to canonical
`https://token-plan.<region>.maas.aliyuncs.com/apps/anthropic` endpoints with no
userinfo, query, or fragment.

This release does not claim a non-exportable platform signing identity or an
independently operated remote transparency anchor. `torq trust readiness`
reports the exact blocking findings. See `SECURITY.md` and
`docs/architecture/production-trust-hardening-decision.md`. The historical
production-readiness audit remains the evidence record for v0.1.0, not this
candidate.

## External prerequisites

- Python 3.11–3.13 and an installed wheel through pipx or `uv tool`.
- Provider subscriptions/credentials and entitled model grants are owned by
  the operator; the package contains none.
- Provider command-line transports must be installed where a selected adapter
  requires them.
- Windows governed chat requires the supported Job Object path.
- The Linux experimental evidence suite requires cgroup v2, a running user
  systemd manager, a protected runtime directory, and user D-Bus. It is not a
  production enablement switch.

## Non-goals

- Production governed chat on Linux or macOS.
- Browser-held credentials or process handles.
- Unattended headless-vault unlock.
- Production-trust-hardening claims for the local signer/anchor.
- A claim that local tests prove provider effectiveness or release readiness.

## Release gate still required

Before publication: run the complete test, Ruff, strict mypy, package-build,
installed-wheel smoke, platform ownership/evidence, and protected-main CI
gates; review the diff; create and verify the signed `v0.2.0` tag; publish wheel,
source distribution, and signed checksums; then verify clean re-downloads. No
step in this paragraph is claimed complete by these candidate notes.
