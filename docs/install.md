# Install TORQ CLI 0.1.0

Python 3.11 or newer is required. The supported distribution is the signed
wheel installed with `pipx` or `uv tool`; source checkout is not required.

## Windows

1. Install Python 3.11+ and `pipx`.
2. Run `pipx install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version`.
4. To reuse the Console team credentials, run
   `torq auth status --credential-file E:\TORQ-CONSOLE\.env`.
5. Run `torq setup --config .torq/config.yaml --answers examples/torq-v5-6-live.answers.json --credential-file E:\TORQ-CONSOLE\.env`.
   TORQ stores only the external file path, never its values.

## macOS

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `uv tool install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version` and `torq auth status`.
4. Complete `torq setup`; use the platform vault or an explicitly supplied
   external credential file.

## Linux

1. Install Python 3.11+ and `pipx` or `uv`.
2. Run `pipx install torq_cli-0.1.0-py3-none-any.whl`.
3. Run `torq --version` and `torq auth status`.
4. Desktop sessions use Linux Secret Service. Headless sessions require the
   attended encrypted-file unlock; unattended plaintext fallback is forbidden.

Clean-machine verification must record the OS image, Python version, wheel
SHA-256, `torq --version`, and an installed-artifact credential-backend probe.
