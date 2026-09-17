# G1D independent browser verification

Scope: bounded P0/P1 dashboard on implementation branch `feat/dashboard-workspace-pr80`. Local Python Fleet server at 127.0.0.1:8878 with a missing run root, authenticated through its real bootstrap exchange. No provider or live build was invoked. Playwright was used because agent-browser CLI was not installed.

## Baseline

The original page led with operational telemetry, disabled the input, and repeatedly requested unavailable chat SSE. At a 320px viewport the composer began around y=1614px. Screenshots are local working artifacts `tmp/dashboard-before.png` and `tmp/dashboard-before-mobile.png`.

## Checks observed after implementation

- New browser state defaults to Fleet, with the actual legacy composer and transcript still present.
- Workspace opt-in survives reload. Switching views retains the same draft and moves the real composer back to its Fleet slot correctly.
- With a missing run root, input is editable, Send is disabled, metadata explains the missing folder, attachments are unavailable, and execution/start capabilities are false.
- Typed draft survives reload with a truthful restored-for-this-tab status. Clear draft remains cleared after reload.
- No `/api/v1/chat/events` request is made when the configured runtime is unavailable.
- At 320px viewport width, document scroll width is 320px. The first implementation places next-action guidance around y=179 and the composer around y=500; final styling is checked separately below.
- Authenticated workspace metadata contains an opaque workspace identifier, reason/remedy, root kind and capabilities rather than the raw filesystem path.

## Fault-injected UI checks

These use browser-local synthetic metadata/transport responses. They demonstrate frontend behavior, not authenticated provider execution.

- Changed the response identity from root A to root B: B opened with an empty draft. Entered B draft, restored A identity, and A's original text returned. No cross-root carryover.
- Deferred synthetic Send acceptance, delivered a stale no-active-turn snapshot, typed a newer draft, and attempted Send again. Exactly one POST reached the synthetic transport; the newer revision remained after acceptance and input stayed editable.
- Forced browser sessionStorage writes to throw QuotaExceededError. Draft stayed in memory and status changed to Held in this tab only; no false persistent-save claim.
- Returned HTTP 401 for workspace refresh. Draft stayed intact, Send remained disabled, and the UI explained the disconnect. Restoring the response recovered metadata. The injected HTTP failure generated the expected browser network error, not a JavaScript exception.

## Final packaged verification

Final visual refinements and Terra's backend eligibility findings were closed. G2A independently approved the bounded change after 31 targeted tests.

Installed `tmp/dist-dashboard/torq_cli-0.2.0-py3-none-any.whl` without dependencies into `tmp/dashboard-wheel-install` and launched Python with isolated import resolution. The printed module path confirmed the server used that installed package. Authenticated browser smoke at 127.0.0.1:8879 confirmed Workspace and its draft survive reload, the missing-root input remains editable, Send remains disabled, execution capability is false, and the verbose evidence caption is hidden. At 320px, document width equals viewport width. Final packaged screenshots: `tmp/dashboard-wheel-mobile.png` and `tmp/dashboard-wheel-desktop.png`.

Additional source UI checks covered dark theme, reduced motion, and CSS zoom at 200 percent without horizontal overflow. CSS zoom was used, not browser toolbar zoom. The primary setup action opens the setup details. No live provider, file editing, command execution, governed apply, or P2/P3 completion is claimed by these checks.
