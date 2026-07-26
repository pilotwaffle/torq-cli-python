# Torq-CLI evidence layer — gotchas, in full

The one-line rules live in `SKILL.md`. This file carries the detail — the
reproductions, the war-stories, the exact commands — for the gotcha you're
actually hitting. Every one cost real time or a real defect in the session this
skill was written from.

## 1. Check `origin/main` BEFORE building anything substantial

The single most expensive mistake of the session: an entire Release 3 command
lifecycle was built on a branch off an **old base**, while `origin/main` had
*already merged a complete, different implementation of the same contract*
(`bd73c69` "Complete Release 3 governed controls #33"). The result was two
competing designs and a "compare and recommend" instead of a clean PR.

Before writing code that implements a contract or feature:
```bash
git fetch origin
git log --oneline "$(git merge-base HEAD origin/main)..origin/main"   # what main gained since you branched
git show origin/main:src/torq_cli/domain/run_evidence.py | grep -c "<the symbol you're about to add>"
```
If `main` already has it, you are hardening or extending, not building. Rebase or
target `main`, don't build a parallel.

## 2. `PYTHONPATH` / worktree trap — tests silently run the WRONG tree

The editable install's `.pth` points at **another worktree** (a "Sol"/fleet-ui
tree), not the directory you're editing. Running `pytest` bare imports `torq_cli`
from that other tree, so your changes appear to have no effect (or the wrong one).

Always run with an explicit, forward-slashed PYTHONPATH pointing at the tree you
are editing:
```bash
PYTHONPATH="E:/Torq-CLI/src" python -m pytest tests -q
PYTHONPATH="E:/Torq-CLI/src" python -m mypy src
```
Backslashes silently fail on this Windows setup — use forward slashes. When you
create a worktree to work off `origin/main`, point PYTHONPATH at *that* worktree's
`src` (e.g. `E:/r3-prose-fix/src`), and run its own `tests`, not the repo's.

## 3. Harden the VERIFIER, not just the producer

A "fix" that only tightens the ingress/producer path (`inject_context`,
`inject_artifact`) is not a fix. The receipt store is the portable trust boundary:
a receipt written by any other path — a buggy or compromised writer, a future
route — must still be refused. In the session, operator prose (a 103 KB
`action_opened.summary`, a `media_type: "text/<megabyte>"`, undeclared keys) sailed
into a **signed receipt that `verify_receipt_store` reported as `verified`**,
because only the producer bounded those fields, not `validate_receipt_payload`.

Put the guard in `run_evidence.py`. Bound values with a **key allowlist + per-key
value schema + closed vocabularies**, not a denylist of one field name (renaming
the key defeats a denylist). Add a universal oversize floor for the transitions a
per-transition allowlist doesn't cover.

## 4. Producer/verifier must agree, or you wedge the run

If the verifier is *stricter* than the producer, the producer emits a payload the
verifier refuses at `append`, raising a raw `ValueError` mid-run instead of a
governed refusal — a wedge. Session example: a `_MEDIA_TYPE` regex that rejected
`text/plain; charset=utf-8`, which the producer happily accepts. **Capture the real
producer payloads first** (monkeypatch `ReceiptChain.append` and print keys/values
across every scenario — accept, reject, artifact, replan, blocked, interrupted,
abandoned) and make the verifier accept exactly what the producer emits, no more,
no less. `command_rejected` is special: it *records the input it refused*, so its
echoed fields (`media_type`, `source_name`, `target_role`) must be bounded by
LENGTH only, never held to accepted-grade validity — otherwise a rejection *for* a
malformed input can't itself be written.

## 5. Windows-specific traps

- **Receipt paths must be POSIX.** `str(path.relative_to(root))` yields backslashes
  on Windows, so a store written on Windows won't verify on Linux, and a strict
  artifact-path regex will reject your own receipts. Emit `path.as_posix()`.
- **Huge pytest params overflow the env block.** A parametrized test whose value is
  ~100 KB of prose blows the 32 KB `PYTEST_CURRENT_TEST` environment-variable limit
  (`ValueError: the environment variable is longer than 32767 characters`) — it's a
  harness crash, not a real failure. Use a small string-key `case` param that looks
  up the big value inside the test body.
- **`git checkout main` may fail** with `'main' is already used by worktree at ...`
  because another worktree owns the `main` branch. Do NOT "work around" it with
  `git reset --hard origin/main` — you are on some other branch or detached HEAD,
  and a hard reset silently discards any uncommitted work in the current worktree.
  Instead add a fresh worktree for the ref you want
  (`git worktree add ../verify origin/main`) or check it out detached in a scratch
  dir; reset only a throwaway tree you are certain is clean.

## 6. Every guard needs a reproduced exploit AND a named mutant

A fix without proof isn't finished. The bar used all session:
1. **Reproduce the exploit** against the baseline first (a runnable `python -c`
   that shows the store verifies with the bad data), then show it's refused after.
2. **Negative test** for the new finding string.
3. **Named mutant** in `scripts/run_named_mutants.py` that reverts the guard to
   `pass`/`False`/`if False:` and MUST be killed — this proves the guard is
   load-bearing, not decorative. Anchor mutants on the **smallest unique line**;
   multi-line before-strings break on any reformat, and `_apply` fails loudly with
   "transformation occurrence was not exactly one" if the anchor isn't unique.

## 7. Nested mappings and lists are prose channels too

`_extra_keys(payload, ALLOWED)` only checks top-level keys. Inner keys of nested
mappings (`effective_attempt`, `earliest_eligible_attempt`, `extraction`) and the
length of lists (`redactions`, `dispatched_roles`) are separate channels — a 100 KB
string rode in an inner key and 50k valid tokens accumulated in a list, both
verifying clean. Give nested mappings an exact key set (`set(m) != {...}`) and cap
list lengths.

## 8. Authority matrix and evidence_basis

- A new governed transition needs a `TransitionRule` row keyed on
  `(writer_role, transition)`, or `append` raises `receipt_writer_unauthorized`.
- `evidence_basis` default is derived per-transition in `receipts.py`. If you add a
  transition whose declared basis is `derived`/`submitted`, add it to that derivation
  set or it defaults to `observed` and collides.
- Changing an existing authority row (e.g. moving `context_injected` from
  `operator_gateway/submitted` to `orchestrator/derived`) makes already-sealed runs
  that contain it read as `tampered`. Check whether the transition shipped in a
  released tag (`git grep <transition> v0.1.0 -- src/torq_cli`) before you change its
  row; if it did, you need a schema-version bump or a legacy-tolerant path.

## 9. Enforce resolution at the terminal decision, not only at seal

A store is never obliged to `seal()`. A guarantee like "every accepted command must
resolve" enforced only under `sealed=True` is optional — a terminal `run_decision`
already makes further appends impossible. Enforce it the moment `terminal_decision`
becomes true inside the receipt loop, and again at seal.
