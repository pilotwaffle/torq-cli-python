# T-06A normalized V5 import

T-06A is a bounded, read-only compatibility projection. `torq config import-v5-normalized
--config ABSOLUTE_PATH` accepts only the authenticated, sanitized
`torq-oracle-v5-config-v1` JSON fixture shape. It does not import raw `.torq/v5/config.yaml`,
resolve credentials, call providers, write configuration, persist state, or claim runtime
effectiveness.

The command authenticates the packaged registry and the raw manifest/V5 fixture bytes before
reading or parsing the supplied document. The input is UTF-8 JSON without BOM, at most 65,536
bytes, maximum depth 8, and has exactly six unique role records. Duplicate keys, secret-shaped
keys, unknown/missing/duplicate roles, malformed values, and reference mismatches fail closed.

The successful output is the fixed registry-authoritative lossy projection: exactly 1,029 UTF-8
bytes, one final LF, SHA-256
`63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e`. The six source records are
canonicalized to the immutable registry bindings; source CLI, endpoint, prompt, and selected
model identities are not live routing. Every expected failure has `data: {}`. Unexpected errors
have only the fixed internal-error envelope, exit 5, and a null snapshot.

The source path is read through the existing lexical guard and a bounded native final-handle
reader. Relative, cross-platform, protected, reparse, alias, hardlink, non-regular, and mutated
objects fail closed. `--output` is deliberately rejected; this slice is stdout-only.

This command remains the strict normalized-JSON import boundary. Raw Console YAML is handled by
the separate T-06C `import-v5-console` command; format auto-detection is deliberately prohibited.
Persistence, migration, credentials, provider access, runtime attestation, and the remaining
Phase 1/PRD tasks require separate authorization and verification.
