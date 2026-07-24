# Install TORQ CLI 0.1.0

Python 3.11 or newer is required. The supported distribution is the signed
wheel installed with `pipx` or `uv tool`; source checkout is not required.

## Windows

1. Install Python 3.11+ and `pipx`.
2. Run `pipx install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version`.
4. To reuse the Console team credentials temporarily, run
   `torq auth status --credential-file E:\TORQ-CONSOLE\.env`.
5. For native storage, create an opaque `credref_<32-lowercase-hex>` handle and
   run `torq auth store --provider PROVIDER --credential-ref REF`. Enter the
   value only at the no-echo prompt; redirected input is refused.
6. Run `torq setup --config .torq/config.yaml --answers ANSWERS.json`, with a
   `credential_refs` mapping in the answers for direct providers. Alternatively,
   run `torq setup --config .torq/config.yaml --answers examples/torq-v5-6-live.answers.json --credential-file E:\TORQ-CONSOLE\.env`.
   TORQ stores only the external file path, never its values.

## macOS

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `uv tool install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version`, then use `torq auth store` and `torq auth verify-access`
   with an opaque credential reference.
4. Complete `torq setup`; use the macOS Keychain or an explicitly supplied
   external credential file.

## Linux

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `pipx install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version`, then use `torq auth store` and `torq auth verify-access`
   with an opaque credential reference.
4. Desktop sessions use Linux Secret Service. The attended headless encrypted-
   file implementation is not present in v0.1.0, so headless native storage
   fails closed; unattended plaintext fallback is forbidden.

Clean-machine verification must record the OS image, Python version, wheel
SHA-256, `torq --version`, and an installed-artifact credential-backend probe.
