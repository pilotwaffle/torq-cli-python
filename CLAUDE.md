# CLAUDE.md — TORQ CLI

## 1. Repo Context

Address the operator as **King Flowers**.

TORQ CLI (`torq-cli` v0.2.0) is a **governed multi-provider agent runner and evidence-backed Fleet control surface**. Standalone Python package, `requires-python >=3.11,<3.14`, console entrypoint `torq = torq_cli.interfaces.cli:main`.

This repo is **not** TORQ Console and **not** TORQCLAW. It is its own lane with its own governance.

> **Staleness check:** verify this context against `pyproject.toml`, `MEMORY.md`, recent commits, and `docs/architecture/` before starting work. If this file disagrees with the repo, stop and report the conflict.

### Instruction precedence

1. Direct operator instruction in the current session
2. This file
3. Global `E:\.claude\CLAUDE.md`
4. General Claude Code defaults

Global rules win where they are **more restrictive** on safety, secrets, destructive actions, untracked files, or owner-gated actions.

### Boundary

- Do not touch `E:\TORQ-CONSOLE`. The recorded Foundation Slice scope explicitly excludes TORQ Console access.
- Do not touch `E:\TORQCLAW`.
- Do not switch into the TORQ V5/V6 Claude harness mode.
- Do not modify the canonical Fable-led global configuration from inside this repo.

---

## 2. Governing Harness

**TORQ CLI is governed by the OpenAI/Codex role chain, NOT the Claude V5/V6 harness.**

| Role | Model | Recorded scope |
| --- | --- | --- |
| **Terra** — G1D / orchestrator / authority | GPT-5.6 Terra High | Scoping, routing, reconciliation |
| **Sol** — G1R and G2A | GPT-5.6 Sol High | Isolated design review; isolated final audit |
| **Luna** — Builder | GPT-5.6 Luna High | Bounded implementation only |
| **Verifier** | GPT-5.5 Thinking High | Independent verification → `READY_FOR_G2A` / `RETURN_TO_BUILDER` |
| **Memory Writer** | GPT-5.5 Thinking High | `MEMORY.md` appends after G2A approval only |

A Claude Code session **MUST NOT** assume the Fable / Opus 5 / Sonnet 5 / Opus 4.8 role map in this repo. That map governs TORQ Console and TORQCLAW; it has no authority here.

The full evidence chain — session IDs, rollout paths, task turns, and per-gate verdicts — is recorded in `MEMORY.md`. Canonical session records live under the operator's local Codex session store (`~/.codex/sessions/`), which is outside this repository and not distributed with it.

### What a Claude session may do here

Unless the operator explicitly states otherwise, a Claude session in this repo is a **bounded worker under direct operator instruction**, not a gate holder:

- Read, analyze, explain, search, and report — always permitted.
- Bounded implementation — only when the operator scopes it explicitly.
- **Never** issue a G1R or G2A verdict, mark a PRD task approved, append gate approvals to `MEMORY.md`, or claim a Codex gate outcome.
- If work would normally require a gate verdict, stop and report that the gate belongs to the Codex chain.

### Authority rules

- Builder output is not approval.
- Offline evidence gathering may establish facts but **never authorizes protected capability** (the recorded controlling invariant).
- G2A verdict controls final pass/fail.
- REJECT cycles are preserved in history; a later APPROVE supersedes them for current state but does not erase them.
- Operator controls commit, push, merge, deploy, release, branch protection, provider configuration, credentials, billing, and static fixture refresh. **None of these have been performed by any agent role.**

### What counts as evidence

Evidence means exact command output with exit status, test counts, SHA-256 hashes, diffs, or preserved artifact paths. Narrative summary is not evidence.

This repo holds preserved integrity hashes (policy, registry, manifest, prompt provenance, role map, v5 config) in `MEMORY.md`. **Do not modify a governed artifact without recomputing and reporting its hash.**

---

## 3. Key Files

| Path | Purpose |
| --- | --- |
| `src/torq_cli/interfaces/` | CLI entrypoint (`cli:main`) |
| `src/torq_cli/core/` | Core resolution and gate sequencing |
| `src/torq_cli/domain/` | Domain model |
| `src/torq_cli/application/` | Application services |
| `src/torq_cli/adapters/` | Provider/runtime adapters |
| `src/torq_cli/connectors/` | External connectors |
| `src/torq_cli/safety/` | Safety and containment |
| `src/torq_cli/data/` | Registry, config, and policy resources |
| `src/torq_cli/testing/` | Test support |
| `tests/` | Full suite (`conftest.py`, `fixtures/`, `js/`) |
| `scripts/` | Governed runners — see below |
| `docs/architecture/` | ADRs, contracts, specs, phase status |
| `MEMORY.md` | **Governance record** — gate verdicts, hashes, residuals |
| `SPEC.md` | Specification |

Governed scripts in `scripts/`: `run_named_mutants.py` · `run_governed_live.py` · `run_live_smoke.py` · `run_native_credential_evidence.py` · `run_linux_ownership_evidence.py` · `verify_owned_process_stress.py` · `audit_extraction.py` · `release_build.py` · `check_links.py`

Key architecture docs: `config-profile-spec.md` · `context-injection-contract.md` · `credential-storage-requirements.md` · `fleet-backend-contract.md` · `fleet-run-contracts.md` · `phase1-status.md` · `foundation-task-status.md` · `adr-2026-07-26-{linux,macos}-chat-containment.md`

---

## 4. Mandatory Rules

### Governance

- `MEMORY.md` is the governance record. **Do not append gate approvals, verdicts, or completion claims to it from a Claude session.** Memory Writer is a Codex role gated on G2A approval.
- Do not mark PRD tasks complete. Status taxonomy lives in `MEMORY.md`; most tasks (T-02, T-03, T-05 through T-36) remain incomplete, dependency-blocked, external, or operator-gated.
- Do not claim Git provenance. The recorded Foundation Slice was built without `.git` evidence.
- Preserved hashes are load-bearing. Changing a governed artifact invalidates the recorded chain — recompute and report.

### Scope discipline

- The recorded Foundation Slice is a **standalone offline** Python CLI slice. Its scope excludes provider calls, credential resolution, network actions, subprocess/provider/operator actions, persistence, sandbox execution, receipts, primary apply, commit, push, merge, deployment, release, and oracle refresh.
- Make the smallest correct change; touch only files the task requires.
- A file outside the task's allowlist is a scope deviation — report it rather than absorbing it silently. (A prior `tests/test_findings.py` deviation is recorded and disclosed in `MEMORY.md`.)
- Keep docs-only changes separate from runtime behavior changes.

### Capability boundaries

- Current production code provides only exact `credref_[0-9a-f]{32}` syntax validation and raw-secret-field rejection. There is **no** credential backend, cryptography, secret input, provider call, migration, persistence API, production unlock, or runtime credential capability. Do not describe any of these as implemented.
- `runtime_effective=false` / `runtime_state=offline_unattested` is the recorded protocol state.
- Do not implement protected capability on the basis of offline evidence alone.

---

## 5. Test Commands

Verify against `pyproject.toml` before running.

```bash
python -m pytest tests/                    # full suite
python -m pytest tests/test_findings.py    # narrowest relevant first
ruff check .                               # lint
mypy src/                                  # type check
python scripts/run_named_mutants.py        # named mutants (12/12 expected)
python -m build                            # build
```

**Last recorded verification state** (from `MEMORY.md`, T-06A): 256 collected · 252 passed · 4 skipped · Ruff pass · mypy pass over 16 sources · mutants 12/12 · isolated offline build, wheel smoke, clean installed-wheel, and adversarial probes pass.

> Test counts rise over time. **Rising counts can mask a genuine deletion** — compare against the recorded baseline rather than assuming growth means health.

Native POSIX/macOS/Linux execution has **not** been performed locally; POSIX behavior was simulated. Do not claim native three-OS verification.

---

## 6. Security

- No secrets, API keys, credentials, `.env` values, or provider keys in code, logs, commits, `MEMORY.md`, or agent state.
- Credential handling is requirements-only. Do not introduce real credential material.
- Do not add network calls to the offline slice.
- Do not run destructive commands unless explicitly approved.
- Do not delete, move, overwrite, clean, or modify untracked operator files, `tmp/`, `dist-release-*`, or unknown local artifacts. **Report them only.**
- If the working tree has user-owned changes, stop and report before editing related files.
- Never remove or weaken a test to make a build pass.
- Do not claim a command ran unless it actually ran; do not claim tests passed without exact output.
- Do not commit, push, merge, deploy, release, change branch protection, configure providers, touch billing, or refresh static fixtures without explicit operator approval. These are operator-controlled and none have been agent-performed.

**Residual risks on record:** wheel and pip-cache hashes prove local byte identity only, not signed upstream provenance. Local administrators, same-user malware, process memory, swap, core dumps, backups, rollback, sync, and lost-passphrase risks remain outside current scope.

---

## 7. Verification Requirements

Before reporting completion:

- Run the narrowest relevant test first, then broaden.
- Report exact counts (collected / passed / skipped / failed), not paraphrase.
- Confirm Ruff and mypy status explicitly.
- Review the diff for accidental edits and out-of-allowlist files.
- Recompute and report any governed artifact hash you touched.
- State plainly what could **not** be verified — native OS execution, CI, branch protection, provider/runtime credentials, and live prompt/model/session behavior are all currently unattested.

Never set a task to complete on intent, assumption, or unverified claim.

---

## 8. State and Memory

- `MEMORY.md` is the single governance record — append-only, Codex Memory-Writer role, post-G2A only.
- There is no `.claude/agents/` in this repo, and no Claude agent-state contract. Do not invent one.
- `.claude/settings.local.json` and `.claude/skills/` exist and are local configuration.
- Keep any operator-requested notes concise: date, change, tests, verdict source, next action.
- Do not store secrets, raw logs, or one-time payloads.

---

## 9. Required Output Format

Lead with the result. Then:

1. Outcome
2. Files changed
3. Tests/checks run
4. What passed
5. What failed or could not be verified
6. Evidence used
7. Risks or limitations
8. Recommended next step
9. Whether owner approval is needed before commit / push / release, **and whether a Codex gate verdict is required**

Never bury failures. Never say something is done if it is only partially done. Never claim a gate outcome this session did not receive.
