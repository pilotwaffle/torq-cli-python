# PRD — TORQ Fleet UI

Status: **deferred — do not build.** Written 2026-07-24.
Operator hold as of 2026-07-24: no UI work begins until the two outstanding
backend phases are complete. This document is a specification of record, not a
build authorization. Revisit §9 build order only when the hold lifts.
Design reference: Palette B "Console Gold" mock
(`torq-fleet-console.html`, https://claude.ai/code/artifact/ad59925e-6d3b-45f7-85bb-f40f7889f1aa).
Depends on: [`architecture/plan-entitlement-accounting.md`](architecture/plan-entitlement-accounting.md).

## 1. Problem

TORQ runs a fixed six-lane governed pipeline (`g1d → g1r → builder → g2a`, plus
the conditional `refine_bug` / `refine_ui` repair lanes). The existing harness
surfaces that as six live panes plus a watcher tab. Three things are wrong with
it:

1. **It demands attention proportional to the number of lanes.** Six streams is
   six streams whether or not anything needs a human.
2. **It shows transcripts, not decisions.** The thing an operator actually needs
   — which lane is blocked, on what, and what unblocks it — is buried in scroll.
3. **It cannot be left.** Close the harness and you lose the run's state.

The pipeline should run in the background. The operator should be told when
something needs them, and otherwise be able to work elsewhere.

## 2. The design thesis

**The board reads the sealed receipt chain, not six output streams.**

Every governed transition already writes a receipt: `run_planned`,
`stage_started`, `stage_completed`, `stage_blocked`, `repair_routed`,
`run_decision`. That chain is hash-linked, signed, and complete by construction —
which means it is a strictly better UI data source than tailing subprocess
stdout. It is ordered, it is verifiable, it survives a closed window, and it is
the same artifact the audit view reads.

This is what makes the six panes unnecessary rather than merely hidden. The UI is
not a quieter harness; it is a different data source.

**Consequence to hold onto:** if a state is not in the receipt chain, the UI
cannot show it honestly. Every proposed indicator in this document must name the
receipt field it derives from, or be cut.

## 3. Users and scope

**Primary user:** the operator running a governed change on their own machine,
with all six lanes on paid subscriptions.

**In scope:** the run board (fleet view), the orchestrator card, per-lane detail,
the value ledger, the quota meter, the input dock, and the persistent background
indicator.

**Out of scope for v1:** multi-user, remote runs, run history search, the
post-hoc audit view (that is TORQ Console, which already exists), and mobile
layouts.

## 4. Screen inventory

TORQ has three surfaces. This PRD covers the third.

| Surface | Question it answers | Status |
|---|---|---|
| Studio | "What do I want to run?" | designed |
| **Fleet** | **"What is running, and does it need me?"** | **this document** |
| Console | "What happened, and does it verify?" | exists |

## 5. Functional requirements

### 5.1 Orchestrator card (lead brain)

The run's single summary object, at the top of the board.

| Element | Source |
|---|---|
| Decomposition — the six lane-bound subtasks | `run_planned.planned_roles` + profile bindings |
| Live tallies — sealed / running / waiting / queued | count of `stage_completed`, `stage_started` without a matching completion, `stage_blocked`, planned-but-unstarted |
| Success criteria checklist | profile contract per role (`_CONTRACTS` in `live_provider.py`) |
| "Waiting on" strip | roles with an open `stage_blocked` or `awaiting_approval` |

**FR-1.** The orchestrator must not report a run finished while any success
criterion is unmet. The gate is the `run_decision` receipt, not the absence of
running lanes.

**FR-2.** Tallies must reconcile to the chain. If the UI's count disagrees with
a recount from `receipts.jsonl`, the UI is wrong and must say so rather than
render a plausible number.

### 5.2 Fleet rows

Six collapsed rows, one per lane, each with a status stripe.

| State | Stripe | Derived from |
|---|---|---|
| sealed | sage | `stage_completed` |
| running | gold, pulsing | `stage_started` with no completion |
| needs you | burnt orange | `stage_blocked`, or `awaiting_approval` |
| queued | grey | planned, no `stage_started` |
| refused | red | `run_decision.status == "blocked"` |

**FR-3.** A row shows a one-line peek — the lane's most recent transition and
its sequence number — without expanding.

**FR-4.** Expanding a row happens **in place**. It never navigates away, because
navigating away from a running board is the failure mode being designed out.

**FR-5.** *Attach* hands the operator one lane's session while the other five
keep running. Detaching returns to the board with no state lost.

**FR-6.** Rows are ordered by receipt sequence, not by lane name. The order the
run actually took is information.

### 5.3 Blocked lanes

**FR-7.** A blocked lane renders from its `stage_blocked` receipt and must
display `provider_dispatch: false` as an explicit assertion — "no request was
sent" — not merely as an absence.

This is the highest-value single element on the board. A refusal that looks like
a failure gets retried; a refusal that shows it refused *before egress* gets
understood.

**FR-8.** Blocked lanes surface the `reason` string verbatim
(`plan_window_exceeded:refine_bug`, `budget_preflight_blocked:g2a`,
`entitlement_unknown:builder`) alongside a plain-language gloss. The raw reason
is the thing that is greppable in the chain; the gloss is the thing that is
readable. Show both.

### 5.4 Value ledger

Three headline figures and a per-lane table.

| Headline | Meaning | Source |
|---|---|---|
| Metered API cost | what this run would have cost at list rates | Σ `metered_usd` |
| Your cost | what it actually cost | Σ `billed_usd` |
| Leverage | monthly metered ÷ monthly plan fees | derived; see FR-11 |

**FR-9.** `metered_usd` is computed as sealed token counts × a pinned, versioned
rate table. It must be **exactly reproducible** by replaying the chain against
that table. This is the requirement that makes the savings figure defensible
rather than marketing, and it is non-negotiable.

**FR-10.** The per-lane table shows input and output tokens **separately**, with
the rate applied to each. A combined total cannot be priced. (This is why
`orchestrator.py` had to be fixed first — see §8.)

**FR-11.** Plan fees are operator-entered configuration, not run evidence. The
leverage figure must be visually distinguished from receipt-backed numbers and
must never appear inside the evidence export.

**FR-12.** An unpriced provider/model pair renders as "unpriced", never as
`$0.00`. A silent zero understates the counterfactual in the flattering
direction, which is precisely the direction that destroys trust in the number.

**FR-13.** Runs sealed before the token split landed cannot be priced. They are
excluded from the ledger with a stated reason, never imputed.

### 5.5 Quota meter

**FR-14.** Meters are per **entitlement account**, not per provider. Providers
sharing a subscription share one bar. (DeepSeek currently runs under the Qwen
plan; showing two bars at 21% and 30% when the pooled figure is 51% would be a
lie the UI told on its own.)

**FR-15.** Each meter labels its provenance separately for the two numbers:
`used` is receipt-derived, `limit` is operator-declared. They must not be
rendered as equally authoritative.

**FR-16.** An account within 10% of its window shows a hot state, with the
reset time.

### 5.6 Input dock

The persistent bar at the bottom of the board. It is an **input**, not a status
strip alone.

**FR-17.** The dock accepts text, images, and documents mid-run.

**FR-18.** Input routes to the **lead brain** by default, which re-plans and
forwards. An explicit override routes directly to one lane.

**FR-19.** Injected context passes the redaction registry (`PatternRegistry`)
before dispatch and is sealed as a `context_injected` receipt. Evidence
completeness is the point: a run whose inputs are partly unrecorded cannot be
audited, and the operator adding context mid-run is exactly the case where
that would silently happen.

**FR-20.** The dock's collapsed state shows: live dot, six pips, metered/actual
cost, elapsed time, and a "N needs you" button that jumps to the first blocked
lane.

### 5.7 Background persistence

**FR-21.** A mini fleet monitor (six bars + run state) is present on **every**
screen, not just the board.

**FR-22.** Closing the board does not stop the run. Reopening reconstructs full
state from the chain — no in-memory session required.

**FR-23.** When a lane transitions to "needs you" while the operator is
elsewhere, notify once. Do not notify per receipt.

## 6. Visual direction

Palette B, Console Gold — single-theme by deliberate choice. `data-theme="light"`
and `data-theme="dark"` resolve identically so a viewer toggle cannot break the
brand.

```
--ground #09090A   --rail #0D0D0F    --panel #131315   --panel-2 #1A1A1D
--line   #2A2823   --ink  #EDE8DC    --ink-dim #A49B8A --ink-faint #6F685B
--accent #D4AF6E   --good #7FA97F    --warn #E08C3C    --crit #C4453E
```

Type: serif display (`ui-serif, Georgia, "Iowan Old Style", Palatino`) for the
wordmark and run title; system sans for everything else; letterspaced uppercase
for section labels; `tabular-nums` everywhere digits align.

**Semantic rule (load-bearing):** gold is the brand *and* the running state.
Semantic colors never borrow the gold — "needs you" is burnt orange, warmer and
more saturated, deliberately not gold-adjacent. This is what keeps gold reading
as TORQ rather than as a status.

## 7. Data contract

The UI reads `receipts.jsonl` and `terminal-manifest.json` from the run root. It
does not read provider stdout.

**FR-24.** The UI runs `verify_receipt_store` on load and on every refresh, and
renders the result. A board displaying a chain that does not verify must say so
prominently — a `tampered` or `incomplete` chain invalidates every number above
it, including the cost figures.

**FR-25.** The UI is read-only against the chain. It never writes receipts
directly; injected context goes through the orchestrator, which seals it.

## 8. Backend dependencies

| Item | Needed for | Status |
|---|---|---|
| Split token counts in receipts | FR-10, FR-9 | **landed 2026-07-24** |
| `stage_blocked` receipt + sealed refusal | FR-7, FR-8 | **landed 2026-07-24** |
| Pinned rate table (`list_prices.v1.yaml`) | FR-9, FR-12 | implemented on backend feature branch; merge pending |
| `EntitlementLedger` + account map | FR-14, FR-15, FR-16 | implemented on backend feature branch; merge pending |
| `billed_usd` / `metered_usd` / `settlement` fields | FR-9, ledger headlines | implemented on backend feature branch; merge pending |
| `context_injected` receipt | FR-19 | not started |
| DeepSeek/Qwen routing resolution | FR-14 correctness | **open question** |

**The value ledger cannot be built before the rate table and receipt fields
land.** Everything else in this PRD can be built against the chain as it exists
today.

## 8a. Deployment topology

**The fleet board ships with the CLI as a local surface. It is not an
independently deployable web application.** Two properties of the evidence layer
force this:

1. It reads `receipts.jsonl` *while the run is writing it*, from a run root
   whose signing identity is ACL-restricted to the current OS user
   (`signing_file_permissions_are_restricted`).
2. Artifact bodies are XOR-streamed with the run signing key
   (`receipts.py:543`). Rendering a lane's actual output requires the private
   key, which never leaves the machine.

A remote surface *can* verify an **exported** bundle when handed the `.pub`
trust anchor out of band — `run_governed_live.py:117-125` already does exactly
this, and that capability is TORQ Console. What a remote surface cannot do is
show a live board or decrypt artifacts.

So: `torq fleet` serves the board locally against the run root. A separate
`torq-cli-ui` repository is warranted only if a hosted multi-user product is
wanted, which requires a different trust model first — export-and-upload,
artifact-body omission, or key escrow — each with security tradeoffs that must
be specified before, not after, any repository split.

## 9. Build order

1. **Board skeleton + fleet rows** — reads the chain, renders states, expands in
   place. Buildable now.
2. **Orchestrator card + blocked-lane detail** — the highest-value surface, and
   its backing receipts now exist.
3. **Dock as status** — pips, elapsed, "needs you" jump.
4. **Quota meter** — after `EntitlementLedger`.
5. **Value ledger** — after the rate table and receipt fields.
6. **Dock as input** — after `context_injected`.

Ship 1–3 against real runs before building 4–6. The board is useful without the
cost surface; the cost surface is not trustworthy without its backend.

## 10. Acceptance criteria

- A six-lane governed run completes with the board never showing a transcript
  pane, and the operator interrupted exactly once — at the `awaiting_approval`
  gate.
- Closing and reopening the board mid-run reconstructs identical state.
- A run blocked at `g2a` shows the lane in "needs you" with
  `provider_dispatch: false` displayed, and the chain verifies.
- Every number in the value ledger is reproducible by replaying the chain
  against its pinned rate table. A spot-check by hand matches to the cent.
- Tampering with one byte of `receipts.jsonl` causes the board to refuse to
  render the ledger.

## 11. Open questions

1. ~~**DeepSeek routing.**~~ Settled 2026-07-24: the lane bills to the Alibaba
   Token Plan in fact, and the transport now says so — it resolves
   `QWEN_TOKEN_PLAN_API_KEY` against the Token Plan host rather than
   `api.deepseek.com`. FR-14 is unblocked.
2. **The `g2a` lane's settlement.** As built it uses a platform API key and is
   genuinely metered, regardless of the ChatGPT subscription tier. Does it move
   to a subscription-authenticated CLI, or stay the one metered lane?
3. **Quota limits.** Cannot be observed across the `claude` subprocess boundary.
   Operator-declared for now — is that acceptable for v1, or does FR-16 need
   provider-reported limits to be worth showing?
