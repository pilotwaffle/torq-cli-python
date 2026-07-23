# T-02 Conductor/MMH Extraction-Viability Audit

Date: 2026-07-23  
Source inspected: `E:\TORQ-CONSOLE`  
Method: read-only static inventory plus execution of the existing MMH resume
fixtures. This audit authorizes no upstream modification.

## Verdict summary

| Subsystem | Verdict | Evidence | Scope |
| --- | --- | --- | ---: |
| `engine` | REUSE | `torq_mmh/router/engine.py` (291 lines); Phase 0A tests cover normalization, retry, budget, and resume | 0 |
| `adapters` | WRAP | `torq_mmh/router/adapters.py` (195 lines), direct `httpx` provider calls and config coupling | 49 estimated wrapper lines |
| `telemetry` | REBUILD | `torq_mmh/router/telemetry.py` (84 lines), SQLite lifecycle bound to MMH config | 84 estimated rebuild lines |
| `redaction` | REUSE | `torq_mmh/router/redaction.py` (69 lines), pure regex contract with fail-closed tests | 0 |
| `graph_compiler` | REUSE | `torq_console/conductor/compile.py` (237 lines), deterministic and writer-free | 0 |
| `policy_gates` | REUSE | `torq_console/conductor/policy.py` (151 lines), pure versioned evaluation contract | 0 |
| `receipts` | WRAP | `torq_console/conductor/receipt_emitter.py` (474 lines); pure projection is separable, persistence imports Supabase preflight | 118 estimated wrapper lines |
| `resume` | WRAP | `torq_mmh/router/engine.py` plus `test_phase0a.py`; hash gating is proven but depends on telemetry storage | 146 estimated wrapper lines |

Total quantified Phase 2 budget: **397 estimated rebuild/wrapper lines**. This
is a sizing floor for the bounded contracts, not an estimate for every test,
connector, CLI, or safety control added by the PRD.

## Resume exercise

Executed from `E:\TORQ-CONSOLE`:

```text
python -m pytest torq_mmh\tests\test_phase0a.py::test_resume_after_failure_no_rebill torq_mmh\tests\test_phase0a.py::test_resume_config_mismatch -q
```

Observed result: **2 passed**, one event-loop deprecation warning, and zero
non-loopback network resolutions. `test_resume_after_failure_no_rebill`
creates a run, injects a failed stage, resumes by run ID, and proves completed
stages are not re-billed. The companion test mutates prompt/config identity and
proves resume fails closed on hash mismatch. Verdict: retain the resume
contract behind a rebuilt standalone metadata store.

## Coupling and persistence findings

- Conductor compiler, validator, policy, observe model, trace, snapshot, and
  recompile logic are predominantly pure Python and suitable for extraction.
- Conductor writer modules and persistence preflight read Supabase environment
  variables and call Supabase HTTP surfaces. They are not standalone-core
  dependencies and must remain behind injected persistence adapters.
- `harness_bridge.py` imports Console supervisor status I/O and therefore needs
  a port/adapter boundary rather than direct extraction.
- MMH adapters use `httpx` and MMH config directly. The normalized behavior is
  reusable; provider transport/auth configuration must be injected.
- MMH telemetry owns SQLite creation and access through MMH configuration.
  Rebuilding the small store contract is safer than importing its global
  lifecycle.

## Test coverage evidence

- Conductor has focused tests for compile, validate, policy, execute, observe,
  recompile, receipts, persistence, harness bridging, dry runs, cycles, and the
  drive loop.
- MMH `test_phase0a.py` covers adapter normalization, redaction, budget guards,
  deadlock behavior, resume, and hash mismatch.
- Phase 2 must retain shared fixtures and add a seeded divergence test; current
  coverage proves contracts exist but does not itself prove extracted parity.

## Dependency and license hygiene

The inspected repository declares MIT in `LICENSE`, `README.md`, and
`pyproject.toml`. MMH requirements are FastAPI, Uvicorn, HTTPX, PyYAML,
Pydantic, and pytest. The standalone core needs only PyYAML for existing
configuration plus stdlib contracts initially; HTTPX and provider SDKs belong
in optional connector extras. Extracted files must retain applicable MIT
notices. A conflicting `package.json` ISC field exists elsewhere in the
monorepo, so release metadata must use the Python project/standalone license
source rather than infer from unrelated Node metadata.

## Phase 2 extraction decision

Proceed in Python. Reuse pure graph, policy, engine-contract, and redaction
logic; wrap provider, Console status, and receipt persistence seams; rebuild
the 84-line telemetry store contract with explicit schema/version ownership.
No Supabase, Railway, environment discovery, or Console import may occur at
standalone-core import time.
