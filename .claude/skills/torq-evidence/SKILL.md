---
name: torq-evidence
model: opus
effort: high
description: Working on the Torq-CLI signed-receipt evidence layer — the schema-v2 run/attempt/command contracts in domain/run_evidence.py, evidence_transitions.py, safety/receipts.py, safety/evidence_broker.py, and the orchestrator/supervisor writers. Load this before changing any receipt validation, transition authority, or the command/context/replan lifecycle so you don't repeat the mistakes in the Gotchas section.
---

# Torq-CLI evidence layer

The evidence layer is an append-only, Ed25519-signed, hash-chained receipt store
with AES-256-GCM artifacts (`run_id` as AAD), a rolling signed manifest, and a
machine-readable transition-authority matrix. It is the **trust boundary of the
whole product** — if a receipt verifies, everything downstream believes it. Treat
every change here as security-critical.

Key files:
- `src/torq_cli/domain/run_evidence.py` — `validate_receipt_payload` (per-receipt
  shape) and `validate_v2_receipt_contract` (cross-receipt lifecycle). The findings
  are snake_case strings; the verifier returns the first one that trips.
- `src/torq_cli/domain/evidence_transitions.py` — the `(writer_role, transition)`
  authority matrix. Every governed transition needs a row.
- `src/torq_cli/safety/receipts.py` — `ReceiptChain.append`/`seal`/`write_artifact`,
  `verify_receipt_store`, the rolling manifest, `terminalize`, `covered_receipts`.
- `src/torq_cli/safety/evidence_broker.py` — capability-scoped writer facade.
- `src/torq_cli/application/orchestrator.py`, `supervisor.py` — the writers.

Writer roles: `orchestrator`, `supervisor`, `operator_gateway`, `recovery`.
Evidence basis: `observed` | `derived` | `submitted`.

## Gotchas — the rules

Each rule below cost real time or a real defect. Read the matching section in
`references/gotchas.md` for the reproduction, commands, and war-story before you
act on the one you're hitting.

1. **Check `origin/main` before building anything substantial** — an entire
   feature was once built parallel to already-merged work. `git fetch` and diff
   `merge-base..origin/main` first; if main has it, you're extending, not building.
2. **`PYTHONPATH`/worktree trap** — the editable `.pth` points at another tree, so
   bare `pytest` runs the wrong code. Always `PYTHONPATH="<this-tree>/src"`, forward
   slashes (backslashes silently fail on Windows).
3. **Harden the verifier, not the producer** — bounding only ingress leaves the
   trust boundary open. Guard in `run_evidence.py` with a key allowlist + per-key
   value schema + closed vocabularies + an oversize floor, never a one-name denylist.
4. **Producer/verifier must agree, or you wedge the run** — a verifier stricter
   than the producer raises a raw `ValueError` mid-run. Capture real producer
   payloads first; `command_rejected` echoes refused input so bound it by length only.
5. **Windows traps** — emit `path.as_posix()` in receipts; avoid huge pytest params
   (env-block limit); never `git reset --hard` to escape a worktree checkout conflict.
6. **Every guard needs a reproduced exploit AND a named mutant** — reproduce the
   exploit, add a negative test, add a mutant that reverts the guard and must be
   killed. Anchor mutants on the smallest unique line.
7. **Nested mappings and lists are prose channels too** — `_extra_keys` only checks
   top-level keys. Give nested mappings an exact key set and cap list lengths.
8. **Authority matrix and `evidence_basis`** — a new transition needs a
   `TransitionRule` row and the right basis-derivation entry; changing an existing
   row makes already-sealed runs read `tampered` (check released tags first).
9. **Enforce resolution at the terminal decision, not only at seal** — a store is
   never obliged to seal, so a seal-only guarantee is optional. Enforce when
   `terminal_decision` becomes true, and again at seal.

## Workflow when changing this layer

1. `git fetch origin` and confirm you're not duplicating merged work (Gotcha 1).
2. If the change targets `origin/main`'s design, work in a worktree off `origin/main`
   and point PYTHONPATH at it (Gotcha 2).
3. Capture real producer payloads before tightening the verifier (Gotcha 4).
4. For each guard: reproduce the exploit → guard in the verifier → negative test →
   named mutant (Gotchas 3, 6).
5. Run the full gate against the tree you edited. `PYTHONPATH` must be set on
   EVERY command — an inline `VAR=... cmd` prefix applies only to that one command,
   so a shared prefix would leave `mypy`/`pytest` importing the wrong tree (the very
   trap in Gotcha 2). Export it once for the shell, or repeat it per command:
   ```bash
   export PYTHONPATH="<tree>/src"
   python -m ruff check src tests
   python -m mypy src
   python -m pytest tests
   python scripts/run_named_mutants.py   # sets its own PYTHONPATH internally, but ROOT must be the edited tree
   ```
   Report the actual numbers.
6. Protected `main` requires the four-job CI matrix (quality Windows/macOS/Linux +
   headless Linux). Open a PR; do not merge past branch protection.
