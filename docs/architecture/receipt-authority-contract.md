# Receipt authority contract

Status: **implemented on `feat/receipt-authority`; merge pending.**

TORQ receipt schema `1.1.0` adds a required, signed top-level `authority` field.
It records who is asserting a transition rather than which process happened to
hold the shared run signing identity.

## Authorities

- `worker` is the default and may assert existing worker transitions.
- `supervisor_derived` is restricted to `stage_interrupted` and
  `run_decision`. It exists so a local supervisor can close evidence after a
  worker dies without pretending it observed provider completion.

Any missing or unknown authority, unknown receipt schema, or
`supervisor_derived` value on another transition makes verification return
`tampered`. Validation is repeated by the offline verifier after receipt hashes
are checked, so a consistently regenerated and validly signed chain still fails
when its authority semantics violate the contract.

Schema `1.0.0` remains readable for existing evidence only when it has no
`authority` member. Fleet labels those receipts `legacy_unspecified`; it never
upgrades them silently to worker-attested evidence. Adding `authority` while
claiming the legacy schema fails closed as `receipt_authority_version_invalid`.

## Signing boundary

This phase authorizes a future local supervisor to append and seal after worker
loss, but it does not claim separate cryptographic identities for workers and
the supervisor. Both currently use the evidence root's authenticated private
identity. The signed authority value is therefore an authenticated assertion by
that local identity, not proof of process isolation. Distinct process keys would
require a separate key-management and delegation contract.

The Fleet projection exposes authority on each projected transition and exposes
the terminal decision authority on the run. Supervisor lifecycle, leases,
interruption recovery, and the reference reducer remain later Release 0 phases.
