# Dashboard P2 G2A review

## Verdict

**APPROVE — bounded P2 goal-to-candidate scope.**

The initial provisional verdict was held while the browser review found a
slow-load draft-revision race, a lost rotated-cookie Start retry, and stale
post-terminal task capability. The final implementation disables draft controls
until their authoritative revision loads, admits only an authenticated,
origin-checked replay of the same task Start request, and refreshes task
capabilities after a terminal result. The targeted regressions and final
installed-wheel/browser validation passed.

The implementation creates a checked candidate only in its owner-controlled
work tree. Its readiness projection requires a verified, sealed task receipt
chain and exact artifact, source, checker, and candidate-inventory bindings.
It does not apply a candidate to the selected project or provide P3 review or
acceptance behavior.

## Independent review evidence

- Read the P2 contract and G1R receipt, then reviewed the task store,
  coordinator, workspace snapshot/materialization, provider command contract,
  structural checker, evidence state machine/projection, HTTP task seam, and
  task browser runtime.
- Ran the final focused suite:

  ```text
  PYTHONPATH=src python -m pytest -q tests/test_candidate_tasks.py \
    tests/test_candidate_task_integration.py tests/test_task_http.py \
    tests/test_task_evidence.py -p no:cacheprovider \
    --basetemp E:\tmp\torq-g2a-p2-final-focused-2
  23 passed
  ```

  This includes a real Windows `OwnedProcess` fixture provider and the real
  packaged checker, source-tree non-mutation, idempotent replay, signed-ready
  projection, tampered candidate downgrade, unknown-termination blocking, and
  the authenticated/origin-checked closed HTTP request contract.
- Ran the task browser-runtime contract:

  ```text
  node tests/js/task_runtime.test.cjs src/torq_cli/data/fleet/task.js
  task runtime contract checks passed
  ```

- Used isolated temporary fixture harnesses for cases that were initially
  missing from the repository tests:
  - A Stop issued while the checker was held at a barrier, followed by a late
    successful checker result, ended `cancelled` with `task_cancelled`; it did
    not produce `candidate_ready`.
  - An unconfirmed Stop ended `termination_unknown`; a new Start was blocked
    both in the same service and after reconstructing the service on the same
    state root.
  - Flipping one byte of the encrypted candidate artifact changed a cached
    ready task to `untrusted` with `artifact_hash_mismatch`.

## Findings resolved during review

The implementation now rejects task-store field injection, preserves draft
revision monotonicity through delete/recreate, fsyncs the durable reservation,
blocks unknown ownership before another reservation, and validates the provider
runtime as a fourth disjoint boundary. It rejects non-finite and duplicate-key
JSON, configured provider symlinks, unsafe candidate inventory, and a stale
queued Start after a draft edit. Candidate readiness is withheld unless the
manifest is sealed and fully verified; it decrypts and cross-checks accepted
input, raw provider output, candidate operations, checker output, helper hash,
candidate bytes, and current source identity.

## Final gates

- Final full repository suite reported by Root: **1042 passed, 13 skipped** in
  103.89 seconds.
- Final wheel/source asset parity SHA-256:
  `ef547cd476261eec58c5dc7b4a95db5edc4176edd0378035f08b541ae5afe10f`.
- Root's rebuilt-wheel browser flow reloaded a generating task into a verified
  candidate, reviewed and started a second task without a page reload, then
  stopped it. It observed three owned launches total, no source change,
  delete/reload/recreate draft revision 4, desktop/mobile no-overflow,
  dark mode, 200% CSS zoom, focus, and reduced-motion checks. Owned servers
  were stopped afterward.
- After the late browser findings, independently ran:

  ```text
  node tests/js/task_resume.test.cjs src/torq_cli/data/fleet/task.js
  node tests/js/task_runtime.test.cjs src/torq_cli/data/fleet/task.js
  node E:\tmp\g2a-p2-task-slow-load.cjs
  ```

  All passed. The temporary slow-load DOM harness delays both capabilities and
  an existing-draft read beyond the autosave threshold, verifies controls stay
  disabled until revision 5 loads, and verifies the first subsequent autosave
  uses revision 5.

## Limits

No live vendor request, credential validation, or paid smoke was performed.
`structural-v1` parses Python, strict JSON, and UTF-8 text; it does not execute
generated code or run unit/integration tests. The result is an isolated
candidate only. P3 primary-tree review, acceptance, and application remain out
of scope.
