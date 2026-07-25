# Plan entitlement accounting

Status: **C1-C7 foundations are on protected main; C8/C9 and the Rev 5.5
reconciliation/rollback lifecycle are Release 2 build-order step 9.** Reconciled
2026-07-25 against `docs/prd-fleet-ui.md` Rev 5.5.

Most provider lanes run on paid plans rather than metered API keys; the OpenAI
HTTP transport remains metered as built. This makes the run mixed-settlement:
plan windows constrain subscription lanes while dollars constrain metered
lanes. This document specifies the accounting contract for both.

The user-facing goal is a defensible answer to "what would this have cost on
metered API?" — but the number is worthless unless it recomputes from sealed
receipts. Everything below exists to make that property true.

## The live path, as actually built

`LiveStageDispatcher` (`adapters/live_provider.py`) has two distinct transports,
and they sit on opposite sides of this problem:

| Lane provider | Transport | Settlement |
|---|---|---|
| anthropic, deepseek, moonshot, zai, qwen | `subprocess` → `claude` CLI (`_claude_compatible`, L146-199) | **Plan-covered** — runs under the CLI's own subscription auth |
| openai | `urllib.request` → `api.openai.com/v1/responses` with `Bearer` (`_openai`, L201-262) | **Metered** — a real API key, real dollars |

So the run is **already mixed-settlement**, and nothing in the receipt says so.
`budget_usd` is applied uniformly to all six.

This is not a hypothetical to design for. It is the current architecture.

### Entitlement is per account, not per provider

Operator-reported as of 2026-07-24: DeepSeek now runs under the **Qwen**
subscription. That breaks the assumption that one provider maps to one plan.
Two lanes drawing on one subscription share **one window**, so a ledger keyed by
provider would show `deepseek 21%` and `qwen 30%` when the true pooled figure is
`51%` — and would let a run pass a preflight it should have failed.

Windows are therefore keyed by **entitlement account**, with an explicit
provider → account map in operator config:

```yaml
entitlement_accounts:
  qwen-max:      {providers: [qwen, deepseek]}
  anthropic-max: {providers: [anthropic]}
  moonshot-max:  {providers: [moonshot]}
  zai-max:       {providers: [zai]}
```

The map is `operator_declared` and must be stated, not inferred. A provider with
no account entry resolves to `entitlement_unknown` and fails closed.

**Resolved 2026-07-24.** The credential layer used to disagree with this
arrangement — `_PROVIDER_KEYS["deepseek"]` read `DEEPSEEK_API_KEY` and
`_CLAUDE_COMPAT["deepseek"]` pointed at `api.deepseek.com`, so the entitlement
map claimed a settlement the transport did not use. The transport now matches:
both lanes resolve `QWEN_TOKEN_PLAN_API_KEY` and the Token Plan
Anthropic-compatible host, and `DEEPSEEK_API_KEY` is not consulted even as a
fallback, because falling back to it would silently reclassify the lane as
metered. See `docs/external-credential-source.md`.

### The OpenAI lane is metered regardless of subscription tier

Operator-reported: GPT is on an OpenAI max subscription. That does **not** make
this lane plan-covered as built. `_openai` sends a `Bearer` token from
`OPENAI_API_KEY` to `api.openai.com/v1/responses` — the developer platform,
which bills per token against a separate prepaid balance. A ChatGPT
Pro/Max subscription covers the chat product, not platform API calls.

The already-sealed run at `docs/evidence/t33-governed-live-2026-07-24/` shows
`g2a` / `openai` / `gpt-5.5` dispatching for real, so that lane has already
spent money.

`g2a` becomes plan-covered only if its transport changes to a subscription-
authenticated CLI, the way the other five lanes work. Until then it is the one
lane where `metered_usd` and `billed_usd` are the same number, and where
`_preflight_cost` is doing genuine work.

## What is broken today

### F1 — Ceilings gate a resource plan lanes do not consume, and the resource they do consume is ungoverned

`scripts/run_governed_live.py:61-66` configures one flat ceiling across all six
roles:

```python
cost_ceiling_usd_by_role={role: args.role_ceiling_usd for role in _ROLES}   # default 0.10
```

For the five `claude`-CLI lanes that figure is fiction — no dollars are spent,
so `budget_preflight_blocked` can only ever fire on an imaginary quantity.
Meanwhile the quantity those lanes really draw down — the subscription's rolling
call/token window — has **no preflight, no receipt field, and no ceiling at
all.** A governed run can exhaust the Kimi weekly window and TORQ will neither
notice nor record it.

Only `g2a`/openai is genuinely metered, and there the existing check is correct
and should be kept. Note `max_output_tokens: 256` (L216) already bounds that
lane hard.

*(For completeness: `torq run --live` via `cli.py:206` never reaches the
preflight at all — `RunController.start:69-70` raises `live_dispatcher_required`
first, because the default `GovernedOrchestrator()` has no dispatcher. The
runner script is the only live path today. That is by design, not a defect.)*

### F2 — Input and output tokens are summed and discarded

The adapter gets this right. Both transports populate the three counts
separately (`live_provider.py:194-197` and `257-260`).

`orchestrator.py:268-271` then throws the distinction away:

```python
tokens = sum(int(response.usage.get(name, 0)) for name in (
    "prompt_tokens", "completion_tokens", "reasoning_tokens"
))
usage = {"tokens": tokens}
```

Output tokens cost roughly **5× input** on every provider in the profile
(Opus: $15 vs $75 per Mtok). A single total cannot be priced.

The loss is confined to these four lines, so the fix is cheap and needs no
upstream change. But it is load-bearing: **the value ledger cannot be
backfilled for any run sealed before this lands.** Fix F2 before building any
UI that shows cost.

### F3 — `cost_usd` records the ceiling, never the actual

`_preflight_cost` returns the configured ceiling, and `_dispatch` writes that
value into the receipt as `cost_usd` with `cost_basis: "configured_worst_case"`
(`orchestrator.py:234, 276, 288-289`). The label is honest, but the receipt
never states what the stage actually consumed — and for plan lanes the true
billed figure is `$0.00`, which no receipt says.

### F4 — A preflight block writes no receipt

`_dispatch` calls `_preflight_cost` at L234 and only appends `stage_started` at
L237. When the preflight raises, `OrchestrationBlocked` propagates with **zero
receipts written for that stage**.

This is the same failure class as the `RunController.start` silent-receipt bug:
a refusal that leaves no trace is indistinguishable from a stage that never
existed. The fleet UI shows a `stage_blocked` receipt; it does not exist today.

## The model

Two axes are currently collapsed into one `cost_usd`. Separate them:

| Axis | Field | Meaning | Source |
|---|---|---|---|
| What it actually cost | `billed_usd` | Money that left the account | Settlement regime |
| What it would have cost metered | `metered_usd` | Counterfactual at list rates | Sealed token counts × pinned rate table |

`settlement` names the regime: `plan_covered`, `metered`, or `unknown`.

**`metered_usd` is a counterfactual, not a credit.** Replaying a sealed chain
against its pinned rate table must reproduce the number exactly. That property
is what makes the savings figure auditable rather than marketing.

## Changes

### C1 — Split the token record

```python
usage = {
    "input_tokens":     int(response.usage.get("prompt_tokens", 0)),
    "output_tokens":    int(response.usage.get("completion_tokens", 0)),
    "reasoning_tokens": int(response.usage.get("reasoning_tokens", 0)),
    "tokens": total,   # retained: summarize_usage and the e2e fixture read it
}
```

Keep `tokens` so `safety/usage.py:24` and `application/e2e.py:63-67` keep
working. Treat it as derived, never as the pricing input.

Reasoning tokens bill at the output rate on the OpenAI path; price them with
`output_usd_per_mtok`, and never fold them into `input_tokens`.

### C2 — Pinned rate table

New `src/torq_cli/data/list_prices.v1.yaml`, carrying the provenance discipline
the surface matrix had to learn:

```yaml
rate_table_version: list-prices.2026-07.v3
observed_at: 2026-07-24
source: vendor_published_pricing
machine_generated: false
receipt_backed: false
rates:
  anthropic:
    claude-opus-4-8:  {input_usd_per_mtok: 15.00, output_usd_per_mtok: 75.00}
  deepseek:
    deepseek-v4-pro:  {input_usd_per_mtok: 0.55,  output_usd_per_mtok: 2.19}
```

`machine_generated: false` because a human transcribes these from vendor pricing
pages. Never present them otherwise.

Every priced receipt records `rate_table_version`. Sealed receipts must not
restate when the table changes — a run priced under `v3` stays priced under
`v3`. Reprice only by emitting a new derived report naming both versions.

An unknown provider/model pair is **not** priced as zero. It sets
`metered_usd: null` with `pricing_status: "rate_unknown"`, and the run summary
reports the gap. Silent zeros would understate the counterfactual in the
flattering direction.

### C3 — Entitlement ledger

```python
@dataclass(frozen=True)
class PlanWindow:
    account: str         # entitlement account, NOT provider — lanes share plans
    providers: tuple[str, ...]
    settlement: str      # "plan_covered" | "metered" | "unknown"
    used: int
    limit: int
    resets_at: str       # ISO-8601 Z
    used_source: str     # "receipt_derived" | "provider_reported"
    limit_source: str    # "operator_declared" | "provider_reported"

class EntitlementLedger(Protocol):
    def window(self, provider: str) -> PlanWindow: ...
    def reserve(self, provider: str, *, calls: int) -> None: ...
    def reconcile(self, provider: str, *, calls: int) -> None: ...
```

`window(provider)` resolves through the account map, so two lanes on one
subscription return the *same* window object and draw down the same counter.
`reserve` and `reconcile` take the provider for caller convenience and apply the
change at the account.

Injected like `StageDispatcher` — the orchestrator owns no transport.

`reserve` before dispatch, `reconcile` after, so a projection that proves wrong
corrects the ledger instead of silently overrunning.

**Split the provenance of the two numbers.** TORQ issues every dispatch itself,
so `used` can be counted from sealed `stage_completed` receipts within the
window — that is genuinely machine-observed (`receipt_derived`). `limit` cannot
be observed through a subprocess boundary and must start as
`operator_declared`. Labelling them separately is what keeps this honest.

### C4 — `_preflight_entitlement`, the sibling to `_preflight_cost`

```python
def _preflight_entitlement(self, role, binding, usage_rows) -> StageBudget:
    window = self.entitlement.window(binding.provider_id)

    if window.settlement == "unknown":
        raise OrchestrationBlocked(f"entitlement_unknown:{role}")

    if window.settlement == "metered":
        ceiling = self._preflight_cost(role, usage_rows)      # unchanged path
        return StageBudget(settlement="metered", ceiling_usd=ceiling)

    projected = self.projected_calls_by_role.get(role, 1)
    if window.used + projected > window.limit:
        raise OrchestrationBlocked(f"plan_window_exceeded:{role}")
    self.entitlement.reserve(binding.provider_id, calls=projected)
    return StageBudget(settlement="plan_covered", ceiling_usd=0.0)
```

The decisive point: **`_preflight_cost` must not run for plan-covered lanes.**
It raises `cost_ceiling_required` whenever a role has no configured ceiling —
which is the correct state for a subscription lane. Routing around it is what
lets the runner script stop fabricating ceilings for the five CLI lanes while
keeping the real check on `g2a`.

`unknown` fails closed. If the ledger cannot establish a provider's regime it
must say so rather than assume coverage; assuming would recreate the
`provider_surfaces.v1.yaml` problem, where a hand-entered guess became
indistinguishable from an observed fact.

### C5 — Write the blocked receipt before raising

```python
try:
    budget = self._preflight_entitlement(role, binding, usage_rows)
except OrchestrationBlocked as exc:
    chain.append("stage_blocked", {
        "role": role,
        "provider": binding.provider_id,
        "model": binding.model_id,
        "reason": str(exc),
        "basis": "plan_entitlement",
        "window": {"used": window.used, "limit": window.limit,
                   "resets_at": window.resets_at,
                   "used_source": window.used_source,
                   "limit_source": window.limit_source},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "billed_usd": 0.0,
        "provider_dispatch": False,
    })
    raise
```

`provider_dispatch: False` is the load-bearing assertion — it proves no egress
occurred, which is what makes a refusal auditable rather than merely reported.

### C6 — Receipt shape

`stage_completed` gains:

```json
{
  "usage":       {"input_tokens": 184200, "output_tokens": 12480, "reasoning_tokens": 0},
  "billed_usd":  0.0,
  "settlement":  "plan_covered",
  "metered_usd": 3.6993,
  "rate_table_version": "list-prices.2026-07.v3",
  "cost_basis":  "sealed_token_counts",
  "entitlement": {"used": 41, "limit": 200, "used_source": "receipt_derived",
                  "limit_source": "operator_declared"}
}
```

`cost_basis` becomes `sealed_token_counts` where actuals exist. Retain
`configured_worst_case` only on blocked receipts and the metered preflight,
where a worst-case figure genuinely is the basis. Keep `cost_usd` as an alias
of `billed_usd` for one release so `summarize_usage` and existing verifiers do
not break, then retire it.

### C7 — `summarize_usage` settlement split

Add alongside the existing `budget` block, which keeps meaning metered lanes
only:

```python
"settlement": {
    "billed_usd": 0.04,
    "metered_equivalent_usd": 7.9834,
    "plan_covered_roles": ["g1d", "g1r", "builder", "refine_ui"],
    "metered_roles": ["g2a"],
    "unpriced_roles": [],
    "rate_table_version": "list-prices.2026-07.v3",
}
```

The mixed case is the normal case: `budget` constrains only `g2a`, while
`metered_equivalent_usd` covers every priced lane.

### C8 — Dispatch registry (PRD AR-4a, AR-4c)

Everything above measures consumption from run evidence. That is sound only
while the set of runs is itself known. It is not: a run's usage and the only
proof the run existed live in the same directory, so deleting it removes
numerator and denominator together and coverage stays at 100 percent. The ledger
reads freed quota where an attacker read evidence.

A root-level, append-only registry outside every run directory closes this. The
broker owns it, enrolls a run at creation before its first dispatch, and signs
each entry:

```python
@dataclass(frozen=True)
class RegistryEntry:
    run_id: str
    account: str            # entitlement account, matching PlanWindow.account
    root_key_id: str        # trust anchor the run's certificate chains to
    window_resets_at: str   # ISO-8601 Z, the rolling window it draws from
    enrolled_at: str        # ISO-8601 Z, before first dispatch
    entry_hash: str         # over the canonical entry plus prior entry_hash
```

`entry_hash` chains the journal. The broker anchors the current head and
monotonic entry count outside the mutable journal, beside the trust anchor and
under the same file protections. Verification recomputes both values and
compares them to the anchor; signatures alone detect modification but not
deletion or restoration of an older, still-validly-signed journal.

Resolution of a registry entry against the evidence tree:

| Entry resolves to | In active denominator | Reservation |
|---|---|---|
| verified run | yes | actual, from sealed receipts |
| `missing` | yes | conservative |
| `deleted` | yes | conservative |
| `unverifiable` | yes | conservative |
| `expired` | **no** | released |

`expired` leaves the active-window denominator and releases its reservation.
Keeping it counted would contradict the reservation-expiry rule in C3 and
ratchet coverage permanently downward as runs age out, until every dispatch
refused.

A registry that is absent, whose recomputed head does not match the anchor, or
whose head is an ancestor of the anchored head does **not** reduce to low
coverage. It fails closed as `registry_rollback_detected`. A rolled-back
registry is an attack on the denominator itself, and treating it as ordinary
missing coverage would let it be reconciled away by the same path that clears
benign gaps. Recovery uses a distinct broker-signed re-anchor record and
quarantines the affected account window at its configured limit before a new
head is accepted. Ordinary reconciliation cannot clear rollback.

### C9 — Coverage gating on `_preflight_entitlement` (PRD AR-4b)

C4 fails closed on `settlement == "unknown"`. It must also fail closed on
incomplete coverage, for the same reason and one step earlier:

```python
def _preflight_entitlement(self, role, binding, usage_rows) -> StageBudget:
    coverage = self.entitlement.coverage(binding.provider_id)
    if coverage.status == "registry_rollback_detected":
        raise OrchestrationBlocked(f"registry_rollback_detected:{role}")
    if coverage.verified < coverage.total:
        raise OrchestrationBlocked(f"entitlement_coverage_incomplete:{role}")
    window = self.entitlement.window(binding.provider_id)
    ...
```

Excluding unverifiable runs is correct for display and dangerous for
enforcement. Display continues to show the partial figure against its
denominator; enforcement refuses. The asymmetry is deliberate — a reader
tolerating an incomplete number loses accuracy, an enforcer tolerating one
grants quota that tampering created.

Coverage spans the active root plus every `trusted_legacy` root. Runs under a
`distrusted_compromised` root stay in the active denominator and are excluded
from the verified numerator. Routine rotation therefore preserves 100 percent
coverage; compromise does not.

### C10 — Durable reservation and reconciliation history (PRD AR-5, AR-6)

Reservations are journaled by entitlement account and registry entry. They
begin at transport attempt, remain conservative after uncertain failure, and
expire only with the rolling window or an explicit reconciliation. The
reconciliation journal is broker-signed, hash-chained, and rollback-anchored.
Each record names actor, time, source, old/new reservation, and affected entry
IDs. Reconciliation may release reserved capacity; it cannot remove a missing,
deleted, unverifiable, or distrusted entry from the coverage denominator.

## State vocabulary

Additions: `plan_covered`, `plan_window_exceeded`, `entitlement_unknown`,
`rate_unknown`, `entitlement_coverage_incomplete`, `registry_rollback_detected`.

## Evidence-source boundary

Quota limits cannot be observed across the `claude` subprocess boundary — the
CLI's JSON payload carries `modelUsage` and token counts, not rate-limit
headers. Two phases, and the labels must never blur:

- **Phase A (now).** `used` counted from sealed receipts (`receipt_derived`);
  `limit` from operator config (`operator_declared`).
- **Phase B.** Where a transport exposes rate-limit headers — the OpenAI
  `urllib` path can read them directly — promote that provider's fields to
  `provider_reported`.

Never label Phase A limits as provider-reported. That is the exact mistake that
made the PR #3 surface results unverifiable.

## Order of work

1. ~~**F2 / C1** — split token counts.~~ **Landed 2026-07-24.**
   `_usage_record` in `orchestrator.py`; split carried through
   `summarize_usage`. `tokens` retained as derived.
2. ~~**F4 / C5** — blocked receipts.~~ **Landed 2026-07-24.** `stage_blocked`
   before the raise, terminal `run_decision {status: "blocked"}`, and
   `RunController.start` seals the refusal so it verifies.
3. ~~**Provider routing.**~~ **Landed 2026-07-24.** The DeepSeek lane bills to
   the Alibaba Token Plan, and `_PROVIDER_KEYS` / `_CLAUDE_COMPAT` now say so:
   `QWEN_TOKEN_PLAN_API_KEY` against the Token Plan host, with no
   `DEEPSEEK_API_KEY` fallback. `QWEN_TOKEN_PLAN_BASE_URL` overrides the region
   for both plan lanes; the native keychain declares none and keeps the default.
4. ~~**C2.**~~ **Implemented 2026-07-24.** The packaged, versioned rate table
   records its source and produces explicit `rate_unknown` results for models
   without a pinned rate.
5. ~~**C3 / C4.**~~ **Implemented 2026-07-24.** Account-keyed entitlement
   windows share Qwen/DeepSeek consumption, reserve/reconcile calls, and bypass
   dollar ceilings only for `plan_covered` lanes.
6. ~~**F3 / C6 / C7.**~~ **Implemented 2026-07-24.** Completed-stage receipts
   seal settlement, billed and metered values, pricing status, rate-table
   version, and entitlement provenance; summaries preserve the split.

7. **C8 / C9 / C10** — dispatch registry, head/count anchor, trust-set coverage
   gating, durable reservations, reconciliation, and rollback recovery. This is
   the next phase. The evidence broker prerequisite is now on protected main;
   until this phase lands, the C3 coverage figure remains advisory because it
   cannot detect a deleted run.

1 and 2 were defect fixes that stood on their own merits and landed before the
next governed live run. 3 was the correctness precondition for the ledger and is
now met. Items 4-6 are on protected main. Item 7 is Release 2 build-order step 9
and is the backend contract the Fleet accounting surface consumes.

**Runs sealed before item 1 landed are permanently unpriceable.** That includes
`docs/evidence/t33-governed-live-2026-07-24/`, whose four `stage_completed`
receipts carry only `{"tokens": N}`. They cannot be backfilled; the value ledger
must exclude them rather than impute a split.

## Tests

- A plan-covered lane with **no** configured cost ceiling dispatches
  successfully. *(Today raises `cost_ceiling_required` — the exact regression
  the fleet UI surfaced.)*
- A lane at its window limit raises `plan_window_exceeded`, writes a
  `stage_blocked` receipt with `provider_dispatch: False`, and calls no
  dispatcher. Assert against a spy dispatcher, not just the exception.
- `g2a`/openai still honours `budget_preflight_blocked` unchanged.
- `settlement: "unknown"` fails closed with `entitlement_unknown`.
- **Replay property:** recomputing `metered_usd` from a sealed chain plus its
  pinned rate table reproduces the sealed value exactly. This is the test that
  makes the savings figure defensible.
- Bumping the rate table does not change any already-sealed receipt.
- An unpriced provider/model yields `pricing_status: "rate_unknown"`, not `0.0`.
- Mixed run — five plan lanes plus `g2a`: `budget` applies only to `g2a`;
  `metered_equivalent_usd` covers all six.
- Reasoning tokens are priced at the output rate, not the input rate.
- Reserve/reconcile: an over-projection returns headroom to the window.
- **Shared account:** two providers mapped to one entitlement account draw down
  a single window. Dispatching on `qwen` reduces the headroom `deepseek` sees.
- A provider absent from the account map raises `entitlement_unknown` rather
  than defaulting to a private window.

Registry tests (C8/C9), each of which fails today because no registry exists:

- **Deleted run is detected.** Enroll two runs, dispatch on both, delete one
  run directory entirely. Coverage reports 1 of 2, not 2 of 2, and preflight
  raises `entitlement_coverage_incomplete`. This is the test the registry exists
  for; without it the deletion reads as freed quota.
- **Rollback is not reduced coverage.** Restore an older, still-validly-signed
  registry journal. Verification raises `registry_rollback_detected`, *not* a
  coverage shortfall, and operator reconciliation is the only path forward.
- **Truncation is caught by the anchor.** Drop the last journal entry. The
  recomputed head is an ancestor of the anchored head; same failure.
- **Enrollment precedes dispatch.** A run that dispatches without a registry
  entry is refused, so the registry cannot be outrun by a racing writer.
- **Expiry releases, it does not accumulate.** Age a run past its window. It
  leaves the active denominator, its reservation is released, and coverage
  returns to 100 percent rather than ratcheting down.

## Not in scope

Plan fees themselves. The UI shows "July leverage 3.8×" against a monthly
subscription total, which is operator-entered configuration, not run evidence.
It belongs in `torq setup` and must never be presented as receipt-backed.
