# Dashboard P3 G2A review

## Verdict

**APPROVE — bounded P3 review, refinement, acceptance, explicit Apply, and
recoverable per-file application scope.**

I reviewed the final shared working-tree implementation against the approved P3
contract. The source transaction remains constrained to evidence-derived UTF-8
create/replace operations and does not add arbitrary command execution,
generated-code execution, project test execution, credential changes, or Git
operations.

This approval covers the application authority boundaries reviewed below. Root
owns the separate final wheel, real-browser, and complete-repository gates;
this receipt does not claim those checks.

## Independent review evidence

- Read the P3 contract and G1R review. Reviewed the immutable task resolver,
  review/application coordinator and mutable-store boundaries, task plan/start
  validation, receipt lifecycle validator, anchored primary transaction and
  marker paths, Fleet HTTP session/replay routing, and task UI request identity
  and eligibility logic.
- Ran:

  ```text
  python -m pytest tests/test_candidate_review.py \
    tests/test_candidate_decision_evidence.py \
    tests/test_fleet_ui_runtime.py tests/test_fleet_ui.py \
    -q -p no:cacheprovider
  16 passed

  node tests/js/task_runtime.test.cjs src/torq_cli/data/fleet/task.js
  task runtime contract checks passed

  git diff --check
  ```

## Findings resolved before approval

- Continuation v2 plans now reject a stale draft server-side; disabling Start in
  the browser is no longer the only protection.
- Continuation replay reconstructs the exact intended draft from the durable
  request and independently verified applied candidate, so a process death
  after the draft write is retryable without trusting cached result data.
  The pre-save active-context write was removed: a concurrent ordinary draft
  now wins its CAS conflict without acquiring continuation lineage.
- The immutable child resolver independently validates the sealed Apply terminal
  *and* its sealed acceptance artifact, rather than relying on mutable review
  state or a generic signed Apply record.
- Rejected and rolled-back applications remain historical outcomes but permit a
  fresh explicit Apply after source freshness checks. Applied and
  recovery-required outcomes still block it. The UI derives retry request
  identity from the prior outcome and renders the corresponding remedy.
- Accepted request replay remains limited to exact request bodies and the
  rotated-session replay routes; it does not widen chat or other mutations.
- The rollback-stage protocol uses distinct signed rollback temps and binds
  stage/restored events to the original operation, preserving identity through
  a crash before or after rollback rename.

## Boundaries and limitations

The primary lease coordinates TORQ installations for one OS user and host. It
anchors roots and parents, rejects observable source drift, and fails closed on
unknown marker, identity, journal, artifact, or staged-file state. It is not a
lock over other editors or tools. The contract and README now require external
writers, including autosave and formatters, to abstain during Apply/recovery;
a write racing the final content/identity check and atomic rename is outside the
portable guarantee. Observed or pre-existing third-party states remain
recovery-blocking and are not overwritten.

No live provider, user-project mutation outside isolated fixtures, wheel smoke,
or browser run was performed by this reviewer.

## Darwin native metadata follow-up — 2026-09-17

**APPROVE — bounded macOS metadata-adapter follow-up.** This supplements the
prior approval, which did not include a Darwin kernel run.

The POSIX transaction now uses Darwin's descriptor-bound `flistxattr` ABI,
instead of Python's unavailable path/API surface. It rejects every xattr or ACL
enumeration failure, including unavailable-feature errors and a null ACL handle;
it therefore never treats unknown metadata as empty. A present ACL entry rejects
the target. The empty extended-ACL result is the Darwin `acl_get_entry` result
`-1` with `EINVAL`; this matches Apple's documented Darwin result convention
(`0` returns an entry, `-1` otherwise). Nonzero BSD `st_flags` also reject.

The snapshot rechecks xattrs/ACLs and BSD flags after the held-descriptor read,
so the evidence snapshot does not accept a metadata transition during that read.
The existing anchored-root, no-follow, identity and permission-policy checks
remain unchanged.

I ran `python -m pytest tests/test_primary_transaction.py -p
no:cacheprovider -ra` (**3 passed**), `python -m mypy
src/torq_cli/safety/primary_transaction.py` (clean), and `git diff --check`.
Those checks exercise the mocked Darwin ABI and static typing on this host; they
are not a Darwin kernel verification. macOS CI remains the required platform
gate before publishing this follow-up.

### Correction: normal absence of a Darwin extended ACL

The preceding null-ACL sentence was too strict. Apple Libc's
[`acl_file.c`](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_file.c)
implements `acl_get_fd_np` by initializing `acl` to null and returning it after
calling `filesec_get_property(FILESEC_ACL, ...)`. It deliberately discards that
call's result. Apple's
[`filesec.c`](https://github.com/apple-oss-distributions/Libc/blob/main/gen/filesec.c)
defines an absent `FILESEC_ACL` property as `-1` with `errno = ENOENT`.
Consequently, a normal macOS plain file yields a null ACL pointer with `ENOENT`.

The corrected, narrowly approved rule resets `errno` immediately before
`acl_get_fd_np` and treats **only** null plus `ENOENT` as no extended ACL. A
non-null ACL is enumerated and rejected if it has an entry; all other null/error
results remain `task_apply_metadata_unreadable`. This preserves the fail-closed
policy for unknown metadata while allowing ordinary files. The follow-up mock
must model null-plus-`ENOENT` for the plain-file case. Native macOS CI remains
the final platform gate for this correction.
