# Dashboard bounded P0/P1 builder handoff

Date: 2026-09-16
Branch: `feat/dashboard-workspace-pr80`
Baseline: `a6716dcf2c585ac3a2e19d4317a56af6ba3ba741`

## Delivered scope

This increment delivers the bounded P0/P1 contract: an opt-in Workspace view for
discussing an existing verified run, truthful authenticated workspace metadata,
safe root and collection classification, provider-driven attachment capability,
fresh submit eligibility checks, and browser-session drafts scoped to an opaque
workspace identity. Fleet remains the initial view and keeps its existing
operational controls.

The CLI now classifies and verifies the selected root before constructing the key
store, chat evidence journal, or provider. The HTTP submit route independently
rechecks root trust, lifecycle writability, chat projection availability, and
active-turn state before forwarding a message. Cancellation remains available
when the session permits it even if sending is unavailable.

The Workspace UI uses the real composer and transcript. It keeps draft editing
available while Send is gated, preserves distinct drafts per workspace for the
current browser tab, avoids unsupported event-stream retries, exposes actionable
setup recovery, and keeps provider/auth/execution claims fail-closed.

## Changed files

- `src/torq_cli/application/workspace.py`: bounded root discovery, link/reparse
  rejection, verified-run classification, safe run identifiers, and opaque stable
  workspace identity.
- `src/torq_cli/interfaces/cli.py`: root/trust preflight before identity, evidence,
  or provider construction while retaining existing model/platform diagnostics.
- `src/torq_cli/interfaces/fleet_http.py`: authenticated `/api/v1/workspace`
  metadata and fresh direct-submit eligibility enforcement.
- `src/torq_cli/data/fleet/index.html`, `chat.css`, and `chat.js`: opt-in Workspace,
  responsive presentation, capability-driven controls, recovery actions, and
  scoped resilient drafts.
- `tests/test_workspace.py`, `tests/test_chat_cli.py`, `tests/test_chat_http.py`,
  `tests/test_fleet_ui.py`, and `tests/js/chat_runtime.test.cjs`: root, trust,
  mutation-order, HTTP gate, capability, navigation, draft, and in-flight
  regressions.
- `README.md` and `docs/plans/2026-09-16-dashboard-ux-plan.md`: launch guidance
  and corrected actual phase scope.

## Verification

- `python -m pytest -p no:cacheprovider tests -k "workspace or chat or fleet"`
  — **186 passed, 841 deselected in 50.17s**. This run used normal Windows temp
  access because the restricted sandbox cannot create pytest temporary folders.
- `python -m ruff check src/torq_cli/application/workspace.py src/torq_cli/interfaces/cli.py src/torq_cli/interfaces/fleet_http.py tests/test_workspace.py tests/test_chat_cli.py tests/test_chat_http.py tests/test_fleet_ui.py`
  — **passed**.
- `python -m mypy --cache-dir tmp/mypy-dashboard-review src` — **passed, no
  issues in 82 source files** (independent G1D run).
- `node tests\js\chat_runtime.test.cjs src\torq_cli\data\fleet\chat.js` —
  **passed**.
- `node --check src\torq_cli\data\fleet\chat.js` and `git diff --check` —
  **passed**.
- `uv build --wheel --out-dir tmp\dist-dashboard` — **passed** and produced
  `E:\Torq-CLI\tmp\dist-dashboard\torq_cli-0.2.0-py3-none-any.whl`
  (274,206 bytes).

G1D independently checked the source UI at desktop, 320 px,
200% CSS zoom, dark theme, storage failure, authentication failure, and workspace
identity changes. A separate desktop/mobile smoke check of the installed wheel returned authenticated workspace metadata,
restored the tab draft, kept Send disabled for a missing root, and included the
final responsive assets. The detailed observations and screenshot paths are in
`docs/plans/2026-09-16-dashboard-browser-verification.md`. G2A independently
approved the frozen implementation after 31 checks.

## Deliberate limits

- Workspace discusses an existing verified run. It cannot start a task, edit
  files, run commands, present a governed diff, or accept/apply changes.
- Live dispatch still forbids tools and files, and the governed change
  transaction remains design-only. P2 and P3 are not delivered.
- Provider authentication is not probed by metadata. No paid provider was called,
  no credential was changed, and no live run was started.
- Draft persistence is limited to the current browser tab. Attachment binaries
  are not persisted with drafts.
- A collection root requires the user to relaunch against one selected run; the
  server does not silently switch roots.
- Nothing was pushed, merged, deployed, or committed by the builder.
