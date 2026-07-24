# Governed orchestration boundary

Status: implemented and locally verified on 2026-07-23.

`GovernedOrchestrator` is the application seam between immutable profile
bindings, normalized provider connectors, and receipt storage. The core live
path is fixed to `g1d -> g1r -> builder -> g2a`; `refine_bug` and `refine_ui`
are conditional repair lanes selected by executable G2A policy.

Each dispatch:

1. selects provider, model, and prompt identity from the selected profile;
2. applies the shared redaction registry before egress;
3. records `stage_started` without response content;
4. requires returned provider/model provenance to match the binding and
   rejects fallback attribution;
5. parses a closed JSON-object response contract and rejects off-contract
   stage verdicts;
6. encrypts the response artifact, hashes it, and records only the artifact
   path, hash, provenance, and usage in `stage_completed`.

The encrypted artifact hash is included in the next stage context, so G2A is
not dispatched without builder evidence. A HIGH bug or UI defect writes a
`repair_routed` receipt, dispatches the corresponding profile-bound refiner,
and sends that evidence to a targeted G2A re-audit. Repair cycles cannot exceed
the configured orchestrator loop budget. Critical defects halt for human
escalation; malformed, ambiguous, fallback, or model-mismatched responses fail
closed.

`RunController` performs prime-directive and double-opt-in checks before the
orchestrator, seals successful receipt chains, self-verifies the terminal
manifest, and returns the receipt pointer, verdict, timeline, usage summary,
proposal metadata, dispatched roles, and repair-cycle count.

## Current production boundary

The repository has injected connector and surface protocols plus hermetic mock
surfaces, but no concrete production transport factory. Consequently the
standalone CLI cannot create a live dispatcher and rejects `--live` with
`live_dispatcher_required` before creating a run directory. This phase proves
the orchestration and safety contracts under injected transports; it does not
claim an out-of-band provider call occurred.

## Verification

The phase gate passed locally on Windows:

- complete pytest suite (four platform-dependent skips);
- Ruff;
- strict mypy across 44 source files;
- 14/14 named mutants killed;
- source distribution and wheel build.
