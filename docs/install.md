# Install TORQ CLI 0.2.0

Python 3.11 or newer is required. The supported distribution is the signed
wheel installed with `pipx` or `uv tool`; source checkout is not required.

## Windows

1. Install Python 3.11+ and `pipx`.
2. Run `pipx install torq_cli-0.2.0-py3-none-any.whl`.
3. Run `torq --version`.
4. To reuse the Console team credentials temporarily, run
   `torq auth status --credential-file E:\TORQ-CONSOLE\.env`.
   The file must already have an owner-only Windows DACL; permissive ACLs fail
   closed and TORQ does not rewrite them.
5. For native storage, create an opaque `credref_<32-lowercase-hex>` handle and
   run `torq auth store --provider PROVIDER --credential-ref REF`. Enter the
   value only at the no-echo prompt; redirected input is refused.
6. Run `torq setup --config .torq/config.yaml --answers ANSWERS.json`, with a
   `credential_refs` mapping in the answers for direct providers. Alternatively,
   run `torq setup --config .torq/config.yaml --answers examples/torq-v5-6-live.answers.json --credential-file E:\TORQ-CONSOLE\.env`.
   TORQ stores only the external file path, never its values.
7. Governed Fleet chat is production-enabled on Windows. Supply direct-provider
   chat credentials with an explicit absolute `--credential-file`; Fleet chat
   does not currently read platform-keychain or headless-vault references.

## macOS

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `uv tool install torq_cli-0.2.0-py3-none-any.whl`.
3. Run `torq --version`, then use `torq auth store` and `torq auth verify-access`
   with an opaque credential reference.
4. Complete `torq setup`; use the macOS Keychain or an explicitly supplied
   external credential file. An external file must already be owner-only mode
   `0600`; TORQ does not chmod it.
5. Governed chat fails closed with
   `owned_process_strong_containment_unavailable`. Keychain support does not
   imply macOS process-containment support.

## Linux

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `pipx install torq_cli-0.2.0-py3-none-any.whl`.
3. Run `torq --version`, then use `torq auth store` and `torq auth verify-access`
   with an opaque credential reference.
   Explicit external credential files must already be owner-only mode `0600`.
4. Desktop sessions may use Linux Secret Service. For an attended headless
   host, select `--backend headless_encrypted_file --store-root ABSOLUTE_PATH`;
   TORQ prompts locally without echo. There is no unattended passphrase channel
   and no plaintext or automatic fallback.
5. Production governed chat fails closed with
   `distinct_identity_system_broker_required`. The per-user systemd/cgroup-v2
   path is an experimental evidence harness and cannot be enabled as production
   chat by configuration.

The v0.2.0 candidate has not yet completed protected-main or clean-machine
release verification. That verification must record the OS image, Python version, wheel
SHA-256, `torq --version`, and an installed-artifact credential-backend probe.
