# Receipt key hierarchy and writer authorization

Status: **implemented on `feat/receipt-authority`; merge pending.**

Receipt schema `2.0.0` replaces the draft shared-key authority claim with a
root-certified per-run key hierarchy.

## Key separation

The evidence root retains one long-lived Ed25519 identity and public trust
anchor. For each run, the configured key store creates independent random keys
for:

- manifest signing;
- orchestrator receipt signing;
- supervisor receipt signing;
- operator-gateway receipt signing; and
- artifact encryption.

File-backed private run keys live under `.torq-run-identities/<run-id-hash>/`,
outside the exportable run directory, and receive the same owner-only
permissions as the root private identity. The run directory contains only a
root-signed `run-certificate.json` with public keys and key IDs. Compromise of a
run writer or manifest key does not reveal the root identity, another run's
private keys, or the separate artifact key.

## Receipt and manifest signatures

Every v2 receipt carries `writer_role`, `evidence_basis`, `writer_key_id`, and
`writer_signature`. The writer signs the canonical receipt body before the
writer signature and receipt hash are added. The receipt hash then covers both
the body and writer signature, and the next receipt links to that hash.

The rolling manifest carries the receipt schema, manifest key ID, and hash of
the run certificate. It is signed by the certified per-run manifest key. The
offline verifier authenticates the root anchor, certificate, manifest, hash
chain, writer key, writer signature, and writer permission before returning
`verified`.

## Writer permissions

- `orchestrator` may write run planning, routing, attempts, stage results, and
  run decisions, using `observed` or `derived` evidence.
- `supervisor` may write only `stage_interrupted` and `run_decision`, and only
  with `derived` evidence.
- `operator_gateway` may write only submitted `context_injected` and
  `action_resolved` receipts.

An invalid role, basis, key, signature, or writer/transition combination is
`tampered`, including when all receipt hashes and the manifest are regenerated
consistently.

## Legacy policy

Schema `1.0.0` retains its original root-signed verification contract. The
unreleased draft `1.1.0` authority shape also retains its original verifier for
stack compatibility, but Fleet labels all schema-v1 writer provenance
`legacy_unclassified`. Legacy evidence is never silently upgraded to v2 writer
attribution.
