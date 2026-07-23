# TORQ CLI Foundation Slice — Implementation Plan

Date: 2026-07-17  
Authority: Terra G1D packet `019f730e-3f6f-75f1-a5d7-b4615c466930`  
Gate 1: Sol G1R `019f7345-ded0-7971-af5d-1b733b862af6`, `APPROVE`

## Execution rules

- GPT-5.6 Luna High is the Builder.
- Work only in Terra's allowlisted paths under `E:\Torq-CLI`.
- Use test-driven development: add a failing focused test, observe the failure, implement the smallest conforming behavior, then rerun the focused test.
- Do not commit, push, merge, deploy, release, refresh oracle data, read upstream worktrees, or perform any operator-controlled action.
- Stop and return to Gate 1 if an exact approved contract cannot be implemented without redesign.

## Build sequence

1. Create packaging metadata and the finding/result-envelope domain types.
2. Add exact registry package data and twelve prompt identity resources; implement closed registry validation.
3. Add the three exact JSON oracle fixtures and exact YAML manifest; implement fixture-only integrity and compatibility validation.
4. Implement closed config validation, raw-secret rejection, opaque credential-reference syntax, connector matching, and required-role checks.
5. Implement immutable staged resolution, offline status, JSON result serialization, and deterministic exit behavior.
6. Add hermetic production guards and tests that prove prohibited APIs and paths cannot be reached.
7. Implement the exact twelve-mutant harness and require `named_mutants: 12/12 killed`.
8. Add extraction/config/credential/task-status documentation and the four-job Python 3.11 CI definition.
9. Build the wheel and run clean-environment installed-command smoke tests.
10. Produce a Builder evidence packet with changed files, red/green TDD evidence, commands, actual results, limitations, and risks.

## Required verification commands

```powershell
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m mypy src
python -m pytest -q
python scripts/run_named_mutants.py
python -m build
python scripts/wheel_smoke.py dist
```

Passing local commands do not prove the four declared CI jobs or branch protection. Those remain external/operator-gated evidence.
