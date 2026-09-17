# PR 80 G2A gate receipt

Reviewed the frozen implementation against baseline `a6716dcf2c585ac3a2e19d4317a56af6ba3ba741` and the approved bounded P0/P1 contract in `2026-09-16-dashboard-g1d-build-contract.md`.

## Judgment

**APPROVE — bounded P0/P1 only.** This receipt approves the friendly Workspace entry, authenticated read-only metadata, root/trust diagnostics, truthful discussion capability controls, session-scoped drafts, and retained Fleet view. It does not approve P2/P3 task creation, file editing, command execution, diff/check review, acceptance/application, or build-tool parity.

## Trust and capability review

- `application/workspace.py` classifies roots without creating keys, journals, or directories. It gives an opaque stable root identifier, rejects symlink/reparse roots and ancestors, bounds collection discovery to immediate `run-*` children, and does not select a collection member automatically.
- The `fleet --serve --chat-provider` CLI path performs root classification and verified snapshot trust checks before `FileRunKeyStore`, `ChatEvidenceJournal`, or provider construction. The new Windows-focused regression covers missing, collection, linked, and untrusted roots.
- `GET /api/v1/workspace` remains session-authenticated and no-store. It exposes the opaque workspace identity, safe run IDs, root/trust state, provider-derived attachment limits, and explicit non-execution capability flags without emitting the configured path, secrets, or receipt bodies.
- Direct chat submission now rechecks a fresh root shape and verified snapshot before dispatch, downgrades a closed or abandoned session, and refuses unavailable runtime or active-turn submissions. Cancellation ownership remains unchanged. The regression tests cover POST-before-GET closed, tampered, unavailable, and active states.
- The UI source stores drafts only in `sessionStorage` under the opaque workspace identity; it preserves in-flight edits, keeps the composer editable when discussion is unavailable, and retains Fleet as the default view. The requested execution capability stays false.

## Independent validation

```text
git diff --check a6716dcf2c585ac3a2e19d4317a56af6ba3ba741
python -m pytest -q tests/test_workspace.py tests/test_chat_http.py tests/test_chat_cli.py tests/test_fleet_ui.py tests/test_chat_ui_runtime.py -p no:cacheprovider --basetemp E:\tmp\torq-g2a-final-targeted
31 passed
```

The Builder also reported clean focused 32/32, workspace/chat/fleet regression selection, Ruff, mypy over 82 source files, and wheel build. Browser fixture checks and screenshots are recorded separately in `2026-09-16-dashboard-browser-verification.md`.

## Limits

No live provider, credential, paid request, or execution engine was validated. This is a local review gate receipt, not a production run certificate.
