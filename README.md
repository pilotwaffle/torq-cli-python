[![CI](https://github.com/pilotwaffle/torq-cli-python/actions/workflows/ci.yml/badge.svg)](https://github.com/pilotwaffle/torq-cli-python/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11--3.13-blue.svg)](pyproject.toml)

# TORQ CLI

**A governed agent runner: every run is attested, dry-run by default, and backed by verifiable evidence.**

TORQ executes multi-stage agent plans (design → review → build → adversarial audit) inside a governed boundary. It validates the machine and role profile before any provider is touched, refuses live execution unless both the operator and the policy opt in, never lets an agent commit, push, or merge, and writes a tamper-evident receipt chain for every run.

## See the dashboard in action

**[Watch the 92-second dashboard demo](https://github.com/pilotwaffle/torq-cli-python/releases/download/dashboard-demo-2026-09-17/torq-dashboard-demo-2026-09-17.mp4)** — plan a task, review changes and checks, request a correction, accept, apply, and continue.

[![TORQ dashboard showing a reviewed and applied candidate](https://github.com/pilotwaffle/torq-cli-python/releases/download/dashboard-demo-2026-09-17/torq-dashboard-demo-poster.png)](https://github.com/pilotwaffle/torq-cli-python/releases/download/dashboard-demo-2026-09-17/torq-dashboard-demo-2026-09-17.mp4)

Recorded September 17, 2026 with an **offline test provider**. The dashboard, structural checks, signed receipts, and file application are real; no live AI service is called. [Demo details and verification report](https://github.com/pilotwaffle/torq-cli-python/releases/tag/dashboard-demo-2026-09-17).

## Why TORQ

| | TORQ | Typical agent CLI |
|---|---|---|
| Default mode | Dry-run plan, zero provider calls | Live execution |
| Going live | Requires `--allow-live` **and** `--policy-allow-live` | One flag, if that |
| Your worktree | Untouched until an audited, tree-pinned proposal is approved | Agent edits freely |
| Evidence | Per-run receipt chain; `torq evidence verify` | None |
| Credentials | Opaque handles in the OS keychain; attended entry only | Plaintext env files |
| Provider containment | OS-owned processes (Windows production; Linux/macOS fail closed) | Whatever the provider does |

## Quickstart

Requires Python 3.11–3.13.

```bash
pip install torq-cli          # or: pip install git+https://github.com/pilotwaffle/torq-cli-python
torq demo --goal "Add input validation to the login form" --run
torq evidence verify --run-root ./torq-demo-runs/<run-id>
```

`torq demo` scaffolds the identity and attestation documents, runs a real four-stage dry-run (G1D → G1R → Builder → G2A), and leaves a verifiable run directory behind. No provider is contacted and nothing outside the run root is written. The full install matrix is in [docs/install.md](docs/install.md).

## What it looks like

```console
$ torq --version
torq 0.2.0

$ torq demo --goal "Add input validation to the login form" --run
{"status": "scaffolded", "inputs_dir": "torq-demo-runs/demo-inputs", ...}
{"status": "dry_run_complete", "report": {"mode": "dry_run", "verdict": "dry_run_complete",
  "planned_roles": ["g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"],
  "attested": true, "receipts": "torq-demo-runs/run-0d04c989291241d58830a964ce578fc9", ...}}

$ torq evidence verify --run-root torq-demo-runs/run-0d04c989291241d58830a964ce578fc9
{"finding": null, "status": "verified"}
```

<!-- TODO: record a 30–60s asciinema of `torq demo --run` + `torq evidence verify` and embed it here -->

## Command surface

| Command | What it does |
|---|---|
| `torq demo` | Zero-config dry-run trial: scaffolds identity + attestation, optionally executes. |
| `torq run` | The governed orchestration boundary. Dry-run by default; live needs `--allow-live` and `--policy-allow-live`. |
| `torq evidence verify --run-root DIR` | Verify a run's receipt chain and artifacts. |
| `torq fleet --run-root DIR --serve` | Evidence-backed Fleet control surface with attended chat. |
| `torq fleet --serve --task-project ID=DIR ...` | Open New task for bounded candidate generation and structural checks. |
| `torq setup` / `torq auth` | Interactive config; credentials stored as opaque handles in the OS keychain or an attended encrypted file vault. |
| `torq status` / `torq profile validate` / `torq harness inspect` | Attest the machine, the role profile, and the live harness before a run. |
| `torq config import-v5-*` | Import normalized or raw Console V5 configuration (read-only projection). |
| `torq trust readiness` | Report production-trust gaps (signing identity, receipt anchor). |

### Workspace dashboard

`torq fleet --run-root DIR --serve` opens Fleet by default. Choose **Workspace** in
the header for a calmer, conversation-first view; the choice is saved locally.
`DIR` must be one individual TORQ run, not its parent collection. If a collection
is selected, Workspace lists safe relative run IDs and shows the exact relaunch
form without choosing a run automatically.

Run discussion is opt-in and remains narrower than a build agent:

```powershell
torq fleet --run-root .\torq-demo-runs\run-0123 --serve `
  --chat-provider claude --chat-model MODEL
```

Workspace can discuss verified run history when the configured provider and
session permit it. Discussion does not create or apply a change. Unsent text is stored only in browser `sessionStorage`, scoped to an
opaque identity for the selected root; **Clear draft** removes that scoped text.
Provider keys, attachments, and filesystem paths are never stored there.

### New task

New task is a separate opt-in launch and does not require an existing run directory:

```powershell
torq fleet --serve --task-project app=C:\work\app `
  --task-provider claude --task-model MODEL `
  --task-claude-bin C:\Users\you\.local\bin\claude.exe
```

TORQ saves the goal and exact relative file scope under an owner-only application
state directory. Start authorizes one tools-off provider request. TORQ writes the
returned UTF-8 contents to a separate candidate, then runs the fixed
`structural-v1` check for Python syntax, strict JSON, and UTF-8 text. The policy
accepts at most 32 files, 64 KiB per file, and 2 MiB total. It does not run unit
tests or execute generated code. Building a candidate leaves the configured
project unchanged.

Open **Review candidate** to inspect its Plan, exact Changes, and signed Checks.
Request a correction to build a separate child candidate, or **Accept reviewed
candidate** to record approval without changing files. **Apply to project** is a
separate action that writes only those approved contents. After application,
**Continue after apply** opens a fresh task draft from the current source.
History remains readable after the project changes.

Application supports UTF-8 file creation and replacement in existing directories.
It verifies source freshness and file identity, preserves supported permissions,
and rejects unsafe links or unsupported metadata. An interrupted application
blocks new work on that project until verified recovery completes; ambiguous or
externally changed files remain blocked rather than being overwritten. These
actions do not commit, push, merge, or run project tests.

Pause other editors, autosave, formatters, and tools writing this project during
Apply or recovery. TORQ serializes its own installations and rejects observed
file changes, but cannot prevent an external write racing the final file rename.

## Safety model

- Agents never commit, push, or merge. The worktree stays unchanged until an audited, tree-pinned proposal receives explicit approval.
- Dry-run is the default. Provider processes only run under OS-enforced containment, and only on platforms where that containment is production-grade.
- Configuration stores `credref_<hex>` handles, never secrets. Secret entry is attended and no-echo; permissive secret files are refused, not repaired.
- Fail closed: invalid config, credentials, regional routing, or missing transport binaries stop the run before a run directory is created.

Full threat model: [SECURITY.md](SECURITY.md) and [docs/security/threat-model.md](docs/security/threat-model.md).

## Platform support

| | Dry-run + evidence | Live provider containment |
|---|---|---|
| Windows | ✅ | ✅ production (Job Objects) |
| Linux | ✅ | Fails closed; systemd/cgroup-v2 adapter is experimental |
| macOS | ✅ | Fails closed pending a signed, notarized containment product |

## Status

Local signing and the same-volume manifest anchor are tamper-resistant, not production-trust hardened — `torq trust readiness` reports the gap explicitly. See [ROADMAP.md](ROADMAP.md) for what closes it.

## Contributing and license

[CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) · Apache-2.0
