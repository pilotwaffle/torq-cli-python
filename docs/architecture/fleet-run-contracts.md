# Fleet run evidence contracts

Status: **implemented and extended by Rev 5.5A; see
[`../prd-fleet-ui-rev-5-5a.md`](../prd-fleet-ui-rev-5-5a.md).**

This phase makes the Fleet read model a deterministic reduction of authenticated
evidence. The UI does not infer attempts from receipt counts or reconstruct lane
order from timing.

## Lane catalog

`run_planned` seals six lanes in display order: four core lanes followed by the
two conditional repair lanes. Every entry records lane kind, provider/model,
prompt ID/version, and response-contract ID/version. Conditional lanes begin
`dormant`; `repair_routed` activates one and binds the route to its allocated
attempt ID, ordinal, and repair cycle.

## Attempt lifecycle

Every provider lane allocates `stage_attempt_created` before preflight. All
later attempt receipts repeat the immutable role, attempt ID, per-role ordinal,
and repair cycle.

- `stage_blocked` is terminal with `provider_dispatch: false`.
- `stage_dispatch_started` records that transport invocation was attempted.
- `stage_completed`, `stage_failed`, and `stage_interrupted` are terminal.
- parsing, attestation, contract validation, artifact persistence, accounting,
  and governed exception handling complete before `stage_completed` is sealed.

The verifier rejects duplicate IDs, ordinal gaps, transitions without a created
attempt, dispatch contradictions, transitions after terminal state, and open
attempts in a sealed run. The pure Fleet reducer maps an attempt to `abandoned`
only after certified `run_abandoned` evidence; operational liveness annotations
never move evidence state.

## Actions and closure

An approval or escalation emits `action_opened`, followed by
`run_decision: awaiting_approval`. The rolling manifest remains unsealed while
the action is open.

Resolution is written by the operator gateway as `action_resolved`. The same
writer then emits a derived terminal `run_decision` whose closed value is
computed from the action's authenticated outcome map and which names the action
ID and exact resolution sequence. Only that linked decision seals the workflow.
Replay reduces open actions as opened IDs minus resolved IDs.

## Fleet snapshot v3

`torq-fleet-snapshot-v3`, inside `torq-fleet-envelope-v3`, exposes ordered lane metadata, current lane state,
attempt history, exact dispatch wording, writer provenance, run execution and
workflow states, open/resolved actions, reduction errors, and normalized live or
sealed verification state. Repeated audits remain separate attempts while lane
counts reflect only the latest attempt state. The envelope separately carries
operational annotations, session state, mutation eligibility, and pending
correlation IDs.
