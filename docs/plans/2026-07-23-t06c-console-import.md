# T-06C Raw Console V5 Import Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

**Goal:** Add a read-only CLI command that converts raw TORQ Console V5 YAML into the canonical `torq-v5-repo-compat` v1 configuration without manual translation.

**Architecture:** Parse raw Console YAML through a dedicated sequence-aware hardened parser derived from the T-06B policy, normalize its six governed agent lanes to the authenticated T-06A oracle shape, and reuse the existing fixed registry-authoritative projection. Keep a separate CLI command so normalized JSON and raw YAML retain unambiguous contracts; leave the canonical config parser unchanged.

**Tech Stack:** Python 3.11+, PyYAML, argparse, pytest, Ruff, mypy, setuptools/build.

---

### Task 1: Lock the raw Console contract with failing domain tests

**Files:**
- Create: `tests/fixtures/torq-console-v5-config.yaml`
- Create: `tests/test_v5_console_import.py`
- Create: `src/torq_cli/domain/v5_console_import.py`

**Step 1: Add a sanitized fixture**

Copy the structural shape of the observed raw Console V5 file without credentials. Preserve the six agent records and their `model`, `cli`, `prompt`, and optional `endpoint` fields so it proves import without manual translation.

**Step 2: Write the first failing tests**

Test `parse_console_config(raw)` for:

```python
finding, normalized = parse_console_config(FIXTURE.read_bytes())
assert finding is None
assert normalized == json.loads(NORMALIZED_REFERENCE.read_text(encoding="utf-8"))
```

Also test role-order independence, missing/duplicate/unknown roles, mismatched governed fields, secret-key rejection, malformed YAML, BOM, invalid UTF-8, oversize input, duplicate keys, aliases, tags, merges, depth, and multiple documents.

**Step 3: Run tests and verify RED**

Run: `python -m pytest tests/test_v5_console_import.py -q`

Expected: collection/import failure because `torq_cli.domain.v5_console_import` does not exist.

**Step 4: Implement the minimal domain parser**

Create `parse_console_config(payload: bytes) -> tuple[str | None, Mapping[str, Any] | None]`. Implement a dedicated sequence-aware preflight with the T-06B byte, UTF-8, document, event, depth, tag, anchor, alias, merge, and duplicate-key constraints. Validate a closed raw root, `version == 5`, exactly six governed agent mappings, and the fields required to produce the existing normalized oracle representation. Reject secret-shaped keys recursively before returning any source-derived data. Do not change `parse_config_bytes`.

**Step 5: Run tests and verify GREEN**

Run: `python -m pytest tests/test_v5_console_import.py -q`

Expected: all domain tests pass.

**Checkpoint:** Review only the fixture, new test, and new domain module. Do not commit because `E:\Torq-CLI` is not a standalone Git worktree.

### Task 2: Add the application boundary through TDD

**Files:**
- Create: `src/torq_cli/application/import_v5_console.py`
- Modify: `tests/test_v5_console_import.py`

**Step 1: Write failing application tests**

Exercise `import_v5_console_path(path)` and require:

- registry and oracle validation precede input access;
- protected/unreadable paths return stable findings;
- valid raw YAML emits the same 1,029 canonical bytes and SHA-256 as T-06A;
- source schema is `torq-console-v5-config-v5`;
- runtime remains `offline_unattested`;
- projection failure and unexpected failure are closed and non-disclosing.

**Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_v5_console_import.py -q`

Expected: failures because the application module is absent.

**Step 3: Implement the minimal boundary**

Mirror the registry-first ordering in `application/import_v5_config.py`, call the raw parser, validate its normalized mapping against the authenticated oracle, reuse `target_config()` and `validate_projection()`, and return immutable envelopes with a distinct command ID.

**Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_v5_console_import.py -q`

Expected: all application tests pass.

### Task 3: Expose the command through TDD

**Files:**
- Modify: `src/torq_cli/interfaces/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_v5_console_import.py`

**Step 1: Write failing CLI tests**

Call:

```python
main(["config", "import-v5-console", "--config", str(path)])
```

Assert compact JSON, exit 0 on success, exit 2 for invalid input, exit 3 for protected paths, exit 5 for internal errors, and rejection of every `--output` form before I/O.

**Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_cli.py tests/test_v5_console_import.py -q`

Expected: argparse rejects `import-v5-console`.

**Step 3: Implement the minimal CLI routing**

Add the subparser and dispatch to `application.import_v5_console`. Keep `import-v5-normalized` behavior unchanged and make output rejection command-specific.

**Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_cli.py tests/test_v5_console_import.py -q`

Expected: all focused CLI/import tests pass.

### Task 4: Update governed documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/t06-normalized-import-spec.md`
- Modify: `docs/architecture/t06-config-file-spec.md`
- Create: `docs/architecture/t06-console-import-spec.md`
- Modify: `docs/architecture/phase1-status.md`
- Modify: `MEMORY.md` only after verification

**Step 1: Document the command and boundaries**

Record the raw YAML contract, deterministic projection, parser constraints, no-write/no-secret boundary, and the distinction from normalized JSON import.

**Step 2: Correct stale status statements**

Mark T-04 reviewed and T-06A/B/C complete only if final verification supports those claims. Keep T-02, T-03, T-05, the broader Phase 1 gate, and later PRD work incomplete.

**Step 3: Review documentation assertions**

Run: `rg -n "T-06|T06|import-v5|Phase 1|credential" README.md docs MEMORY.md`

Expected: no claim of provider, credential, persistence, deployment, release, merge, or full Phase 1 completion.

### Task 5: Full verification and handoff

**Files:**
- Review all changed files.

**Step 1: Run focused tests**

Run: `python -m pytest tests/test_v5_console_import.py tests/test_cli.py tests/test_config_schema.py -q`

Expected: zero failures.

**Step 2: Run full tests**

Run: `python -m pytest -q`

Expected: zero failures.

**Step 3: Run static checks**

Run: `python -m ruff check .`

Run: `python -m mypy src`

Expected: both exit 0.

**Step 4: Run mutation checks**

Set `TORQ_T06B_MUTANT_ROOT` to a validated temporary directory under `E:\Torq-CLI\.tmp-verification`, then run `python scripts/run_named_mutants.py`.

Expected: all named mutants killed.

**Step 5: Build and smoke-test**

Run: `python -m build`

Run: `python scripts/wheel_smoke.py`

Expected: both exit 0.

**Step 6: Review scope**

Run: `git diff --stat -- E:/Torq-CLI` if a safe tracked boundary becomes available; otherwise enumerate modified files and compare them with this plan.

Expected: only T-06C implementation, tests, fixture, and governed documentation changed.

**Step 7: Append memory**

Only after all commands pass, append exact test counts and residual limitations to `MEMORY.md`. Do not claim hosted CI, native external execution, branch protection, merge, deployment, or release unless separately observed.
