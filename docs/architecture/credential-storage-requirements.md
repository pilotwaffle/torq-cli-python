# Credential Storage Requirements

Status: T-04 requirements only. The Foundation Slice implements no credential
storage, credential resolution, secret input, cryptography, provider call,
migration, or credential runtime API.

## Foundation boundary

The Foundation has exactly two credential-related findings:

- `raw_credential_field_forbidden` rejects raw secret fields in declarative
  configuration.
- `credential_ref_invalid` rejects every reference except the exact syntax
  `credref_[0-9a-f]{32}`.

Those findings establish syntax and configuration hygiene only. A valid
`credential_ref` proves neither that a credential exists nor that it is valid,
entitled, resolvable, or usable. The Foundation never looks up the reference.
There is currently no backend, plaintext or encrypted secret store, secret
input mechanism, crypto implementation, provider integration, migration path,
or credential API.

## Required future backend boundary

Future backend targets are Windows Credential Manager, macOS Keychain, and
Linux Secret Service. A future attended-only headless Linux encrypted fallback
is a separate target. Backend selection is explicit: there is no automatic
backend substitution, fallback, downgrade, escrow, or provider-side
substitution when a selected backend is unavailable.

For this document, a “prompt” means any agent-visible, provider-visible, or
tool-visible input. A future fallback passphrase may be accepted only from
local no-echo attended terminal input. It must never be supplied through
arguments, configuration, environment variables, pipes, CI, child processes,
agents, providers, logs, receipts, or telemetry. Unattended secret delivery is
unavailable and remains forbidden.

## Future v1 encrypted envelope

The following is a closed future contract; it creates no current runtime
capability.

- There is one credential per `<credential_ref>.tqcv` file.
- The raw file is exactly 1..98304 bytes before parsing.
- The file is canonical UTF-8 JSON with no BOM, trailing bytes, newline,
  whitespace, non-ASCII strings, floats, exponents, nulls, arrays, booleans,
  duplicate keys, unknown fields, or missing fields.
- Object keys sort lexicographically at every nesting level.
- Integers use the shortest base-10 decimal representation.
- Canonical reserialization must byte-equal the input.
- The exact outer field order is `aead`, `ciphertext_b64`, `format`, `kdf`,
  `metadata`, `version`.
- The outer object contains exactly those six fields.
- `format` is exactly `torq-credential-vault`.
- `version` is the integer `1`.
- The `aead` object contains exactly `algorithm` and `nonce_b64`.
- `aead.algorithm` is exactly `xchacha20poly1305-ietf`.
- `nonce_b64` is canonical Base64 for exactly 24 cryptographically secure
  random bytes.
- The `kdf` object contains exactly `algorithm`, `memory_kib`, `parallelism`,
  `salt_b64`, `time_cost`, and `version`.
- `kdf.algorithm` is exactly `argon2id`.
- `kdf.memory_kib` is exactly `65536`, `kdf.parallelism` is exactly `1`,
  `kdf.time_cost` is exactly `3`, and `kdf.version` is exactly `19`.
- `salt_b64` is canonical Base64 for exactly 16 cryptographically secure
  random bytes.
- A nonce is never reused with the same key.
- The derived key is exactly 32 bytes.
- The `metadata` object contains exactly `backend`, `credential_ref`,
  `generation`, and `provider_id`.
- `metadata.backend` is exactly `headless_encrypted_file`.
- `metadata.credential_ref` uses the exact `credref_[0-9a-f]{32}` syntax.
- `metadata.generation` is an integer in the inclusive range
  `1..9007199254740991`.
- `metadata.provider_id` is exactly one of `anthropic`, `openai`, `grok`,
  `moonshot`, `zai`, or `deepseek`.
- Metadata contains no accounts, endpoints, models, labels, timestamps,
  paths, usage data, or secret hashes.
- Every `*_b64` value uses padded standard RFC 4648 Base64 only: `A-Z`,
  `a-z`, `0-9`, `+`, `/`, and mandatory `=` padding.
- Base64 rejects whitespace and the URL-safe alphabet, and decode/re-encode
  must equal the input.
- Decoded ciphertext is exactly 17..16400 bytes.
- Plaintext is raw secret bytes of exactly 1..16384 bytes and is never nested
  JSON.
- The passphrase is accepted only from local attended no-echo terminal input.
- The passphrase is NFC-normalized, strict UTF-8, contains no NUL, is exactly
  1..1024 bytes, and is not trimmed except for one terminal line ending.
- A passphrase is forbidden in arguments, configuration, environment
  variables, pipes, CI, child processes, agents, providers, logs, receipts,
  and telemetry.
- Associated data is the canonical outer JSON excluding only
  `ciphertext_b64`.
- Authentication must succeed before decryption or plaintext parsing.
- The requested reference, backend, and provider must exactly match the
  authenticated metadata.
- Wrong input, tampering, malformed ciphertext, corruption, unsupported
  version, and metadata mismatch collapse to one future non-disclosing
  opaque-unlock outcome. T-04 adds no runtime finding.

## Lifecycle, recovery, and privacy

- Credential creation, rotation, and revocation are operator-only actions.
- Locked, absent, unavailable, unresolved, and unsupported backends fail
  closed.
- Rotation for a stable reference increments authenticated `generation` and
  atomically replaces the final record.
- An interruption during rotation leaves the prior final record authoritative.
- Local revocation is deletion/absence only. Provider revocation is a
  separate operator action and is not implied by local deletion.
- A future vault uses exclusive interprocess locking with a five-second
  bounded acquisition and fail-closed timeout.
- A same-directory encrypted temporary file has restrictive permissions
  before writing.
- The future implementation flushes the file and directory where supported
  before atomic replacement.
- There is no plaintext temporary file, backup, downgrade, automatic
  promotion, or automatic migration.
- Unknown and old formats fail closed without automatic migration.
- Any future migration requires a separate Gate 1.
- Evidence of corruption, interruption, or wrong input is preserved as
  untrusted evidence and is never overwritten, promoted, or disclosed.
- Future POSIX permissions are `0700` for the store directory and `0600` for
  records. Windows must verify current-user ACLs before use.
- A future local status probe may report only coarse backend availability,
  lock state, and reference presence.
- A status probe must not call a provider, decrypt, resolve a secret, assert
  entitlement, expose agent/tool data, or imply validity.
- Metadata is privacy-sensitive even when it is not secret. Backend,
  credential reference, generation, and provider identity must not be emitted
  to logs, receipts, telemetry, prompts, or public results unless a later
  contract explicitly permits a nonsecret state projection.

Accepted residual risks include rollback restoring locally revoked material,
lost passphrases and damaged stores being unrecoverable, local administrators
or same-user malware accessing process memory, swap, core dumps, or backups,
OS synchronization exposing records, metadata revealing provider use, and
provider validity, grants, remote revocation, billing, and diagnostics
remaining unknown. Clean-machine, native Windows/macOS/Linux, headless,
ACL, roaming, and synchronization behavior require later evidence.

## Future CI boundary

T-07 must use deterministic credential-free conformance fixtures for:

- canonical rejection;
- duplicate and unknown fields;
- Base64 rejection;
- all raw-file, ciphertext, plaintext, passphrase, generation, and metadata
  bounds;
- metadata mismatch;
- tamper and authentication failure;
- deletion/revocation;
- interruption and no-downgrade behavior.

Those fixtures may use only an approved test-only crypto adapter. Production CI
must contain no real credentials or passphrases, production unlock channel,
OS-store access, provider calls, or agent/provider-visible secrets. Fixtures
must not be reused as production secrets or establish backend effectiveness.

Crypto implementation, dependency choice, backend implementation, secret
input, provider interaction, and runtime APIs require a separate Gate 1.

This document is not clean-machine verification, provider setup, keychain
access, rotation proof, credential-backend evidence, or release evidence.
