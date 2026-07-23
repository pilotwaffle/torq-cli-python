# TORQ CLI Memory - Foundation Slice

Last updated: 2026-07-23

## Status Taxonomy

- Foundation Slice: Completed and verified.
- Independently approved work recorded in this memory: T-01 Foundation Slice,
  T-04 credential-storage requirements documentation, T-06A normalized import,
  and T-06B config-parser hardening.
- T-06C and the full T-06 config contract are implemented and locally verified;
  no new independent gate approval is recorded for T-06C.
- Other later PRD tasks and phases remain proposed, incomplete, blocked, or
  operator-gated as recorded below.
- Operator-controlled actions remain unperformed unless separately recorded elsewhere by the operator.

## Objective

Build the Foundation portion of `C:\Users\asdasd\Downloads\torq-cli-prd-execution-plan-r5-2026-07-17.md` inside `E:\Torq-CLI` under session-scoped OpenAI routing, without modifying the canonical Fable-led global configuration.

This repository initially had no `MEMORY.md`. This file was created only after final Sol G2A approval of the Foundation Slice.

## Controlling Invariant

Offline evidence gathering may establish facts but never authorizes protected capability. Foundation Slice resolution must execute actual registry-first sequential prerequisites against one retained registry/config evidence chain, stop at the earliest failed stage, verify raw fixture integrity before parse/materialization, and return stable immutable findings and snapshots without protected capability execution.

## Scope And Prohibited Boundaries

The approved Foundation Slice is a standalone offline Python CLI/package slice in `E:\Torq-CLI`. The scope excludes provider calls, credential resolution, upstream import or access, Git or `.git` access, `E:\TORQ-CONSOLE` access, network actions, subprocess/provider/operator actions, persistence, sandbox execution, receipts, primary apply, commit, push, merge, deployment, release, oracle refresh, branch-protection changes, billing, and later PRD phases.

Canonical Fable-led global config was not to be modified. Luna placement remained session-provisional under the operator contract; no separate Luna acceptance is recorded beyond the actual gate evidence listed below.

## Canonical Role, Model, And Thread Evidence

- Terra G1D/orchestrator: GPT-5.6 Terra High, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\17\rollout-2026-07-17T21-29-01-019f730e-3f6f-75f1-a5d7-b4615c466930.jsonl`.
- Sol G1R: GPT-5.6 Sol High isolated review, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\17\rollout-2026-07-17T22-29-46-019f7345-ded0-7971-af5d-1b733b862af6.jsonl`, task turn `019f7345-e5d4-7dd1-8d51-25a6744b15e1`.
- Luna Builder: GPT-5.6 Luna High Builder, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\17\rollout-2026-07-17T22-36-31-019f734c-0c0f-7391-8c4d-81aa021a8297.jsonl`.
- Final independent verifier: GPT-5.5 Thinking High, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T03-56-26-019f7470-f227-7442-a7a6-d74904300234.jsonl`, task turn `019f7470-f9af-73d3-9d96-25b2ff574030`.
- Final Sol G2A: GPT-5.6 Sol High isolated final G2A, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T04-04-26-019f7478-433e-74c1-8f96-5f7d30ad2ed9.jsonl`, task turn `019f7478-4b38-7590-86e6-4b41b48260e2`.
- Memory Writer: fresh GPT-5.5 Thinking High, canonical record `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T04-17-50-019f7484-8843-7a51-a123-945447fd0f89.jsonl`.

## Gate Verdicts

- G1R verdict: APPROVE for bounded Foundation Slice implementation.
- G2A final verdict: APPROVE for Foundation Slice only.
- Intermediate rejected G2A threads actually invoked: `019f7384-d911-7912-8d78-247fc2377b82`, `019f73c5-5e8a-7af3-b902-46135ea77d86`, `019f73e6-0376-7140-8acc-af97e557b411`, `019f7410-5e70-7760-9790-6afa55ea38a2`, `019f7430-984b-7b30-baaa-70b6d3444d7c`, `019f7453-2a26-71c3-8ca7-b0fbf6b73901`.
- Those REJECT cycles drove bounded Terra/Luna corrections. Final G2A approval supersedes them for current state, but their history is not erased.

## Implemented Artifact Summary

- Standalone offline Python CLI Foundation Slice.
- Two canonical profiles: `torq-v5-6-live` and `torq-v5-repo-compat`.
- Registry-first actual sequential gates with retained evidence: `registry_read`, `registry_validate`, `config_read`, `config_validate`, `profile_resolve`, `eligibility`, and oracle validation as applicable.
- Hermetic native path handling with protected-root denial and identity-preserving accepted paths.
- Safe immutable envelopes and snapshots with non-disclosing findings.
- Closed config and registry schemas.
- Exact prompt, policy, oracle, role-map, manifest, and v5 config integrity.
- Packaging, CI declaration files, README, and architecture docs included as artifacts of the Foundation Slice.

## Current Verification

Final verifier and final G2A records preserve this current verification state:

- Full suite: 216 collected; 212 passed; 4 skipped.
- `tests/test_findings.py`: 43 passed.
- Ruff: clean.
- Mypy: clean across 14 source files.
- Named mutants: 12/12 killed.
- Build: passed.
- Wheel smoke: passed.
- Native POSIX/macOS/Linux execution was not performed; POSIX behavior was simulated.

## Preserved Hashes

- Policy: `fbeb3e1597a8dde9b4e5c1b5b049605bd3ed767c468a3f75b2e362c3e2f153ca`.
- Registry: `e6c378aa5104b871baf6c6404f13d1373a5ec7cbf486283a0a28dab73da87128`.
- Manifest: `f4255aadddfbe1d3de47d51145c2ea4e8e610dcc1284d017a9abd80d22f68e4b`.
- Prompt provenance: `798c9ba82859da69eb6d38c5b16a47dd9891be48f4310136421c36e6b8e6382b`.
- Role map: `7348b7c15bfece61ecacdeb71a860ad4221425044d39e705cf0ec7b28b4d2cf0`.
- v5 config: `52110d4522b883ee16b313a612441871e802710e75bb754bb5d9707492f27698`.

## Procedural Scope Deviation

Luna made a procedural scope deviation: `tests/test_findings.py` was outside the initial sixth allowlist. Terra prospectively reconciled the narrow public-path monkeypatch change targeting `resolve_module.validate_config_shape` through public `resolve_text`, with necessary import cleanup. Final G2A treated the issue as historically violated and currently nonblocking. Exact historical diff proof remains unavailable without Git or a baseline and is not claimed here.

## Known Limitations And Residuals

- No local `.git` evidence was available or used; no Git provenance is claimed.
- Parent repository dirtiness was unrelated and not interpreted as proof of this slice.
- Native POSIX/macOS/Linux execution was not performed.
- External four-job CI execution and branch protection are not attested.
- Provider/runtime credentials, grants, routing effectiveness, live prompt/model/session behavior, and provider execution are unattested.
- Credential backend, sandbox, receipts, persistence, primary apply, deployment/release, T-02, and later PRD work remain unimplemented or operator-gated.
- Static fixture refresh remains operator-gated.

## Remaining Operator Actions

The following remain operator actions and were not performed by the Foundation Slice builder, verifiers, G2A, or this Memory Writer record: commit, push, merge, deploy, release, branch protection, provider configuration, credentials, billing, and static fixture refresh.

## Post-G2A T-04 Memory Update

This section records the post-G2A memory state for T-04 only. It does not mark Phase 1 complete and does not advance the full PRD. The pre-memory strict 109-file repository inventory applies to the repository state before this authorized `MEMORY.md` update; this update intentionally changes only `MEMORY.md`, so earlier full-repository byte-equality and aggregate inventory digests must be interpreted as pre-memory evidence.

### T-04 Status

- T-04 status: completed and verified as requirements-only credential-storage documentation.
- T-01 status remains complete and Foundation-approved.
- T-02, T-03, and T-05 through T-36 remain incomplete, dependency-blocked, external, or operator-gated.
- Current production code still provides only exact `credref_[0-9a-f]{32}` syntax validation and raw-secret-field rejection. No credential backend, cryptography, secret input, provider call, migration, persistence API, production unlock, or runtime credential capability is implemented by T-04.
- `docs/architecture/phase1-status.md` remains an authority/status document preserving Phase 1 incompleteness and the requirements-only T-04 boundary.

### Actual Model, Thread, And Gate Roles

- Terra T-04 authority/orchestrator: GPT-5.6 Terra High, session `019f730e-3f6f-75f1-a5d7-b4615c466930`, path `C:\Users\asdasd\.codex\sessions\2026\07\17\rollout-2026-07-17T21-29-01-019f730e-3f6f-75f1-a5d7-b4615c466930.jsonl`, last task_complete turn `019f7552-36f8-7a32-b9ac-d4d5fa07bb4b`.
- Sol G1R: GPT-5.6 Sol High, session `019f748c-ff76-7df1-b689-e34bd6053f27`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T04-27-05-019f748c-ff76-7df1-b689-e34bd6053f27.jsonl`, last task_complete turn `019f74b6-f6c5-76c1-9b5e-17594bfb126a`, verdict APPROVE for bounded external verification wheelhouse staging.
- Luna Builder: GPT-5.6 Luna High, session `019f734c-0c0f-7391-8c4d-81aa021a8297`, path `C:\Users\asdasd\.codex\sessions\2026\07\17\rollout-2026-07-17T22-36-31-019f734c-0c0f-7391-8c4d-81aa021a8297.jsonl`, last task_complete turn `019f7553-fb83-7e13-8089-6ff1873d992d`, final builder packet completed the v4 external-only matrix correction and issued no approval decision.
- Verifier 1: GPT-5.5, session `019f74cc-3cf4-7af0-aa0e-45bb79c230d6`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T05-36-09-019f74cc-3cf4-7af0-aa0e-45bb79c230d6.jsonl`, last task_complete turn `019f74cc-44c3-7312-8bee-a577fe4cfda3`, recommendation RETURN_TO_BUILDER.
- Verifier 2: GPT-5.5, session `019f74e0-a668-7973-9f20-57a3f1e3a231`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T05-58-27-019f74e0-a668-7973-9f20-57a3f1e3a231.jsonl`, last task_complete turn `019f74f3-9d17-72f2-b62b-fbcabe52d04e`, recommendation READY_FOR_G2A after resumed abnormal runner disclosure.
- G2A 1: GPT-5.6 Sol High, session `019f74f9-8656-72e2-b8fa-acf36bc20b60`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T06-25-37-019f74f9-8656-72e2-b8fa-acf36bc20b60.jsonl`, last task_complete turn `019f74f9-8e70-7e53-a1ad-253b3b9a3f43`, verdict REJECT.
- Verifier 3: GPT-5.5, session `019f7525-3082-79d0-81eb-6a54da13a0eb`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T07-13-19-019f7525-3082-79d0-81eb-6a54da13a0eb.jsonl`, last task_complete turn `019f7525-3380-7ea1-8b32-4c4719f973d3`, recommendation RETURN_TO_BUILDER.
- Verifier 4: GPT-5.5, session `019f7539-e7f8-7df1-b1a8-e5522c2e098e`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T07-35-57-019f7539-e7f8-7df1-b1a8-e5522c2e098e.jsonl`, last task_complete turn `019f7539-ea1e-7761-b940-75832592d4ac`, recommendation READY_FOR_G2A.
- G2A 2: GPT-5.6 Sol High, session `019f7541-a658-7cc2-b7a5-08f6e9667581`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T07-44-24-019f7541-a658-7cc2-b7a5-08f6e9667581.jsonl`, last task_complete turn `019f7550-d999-7851-807e-2f0e23e2d407`, verdict REJECT.
- Verifier 5: GPT-5.5, session `019f7559-85ef-7d51-a46a-52d4088491fb`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T08-10-29-019f7559-85ef-7d51-a46a-52d4088491fb.jsonl`, last task_complete turn `019f7559-8d8c-7512-90b7-f33547aacbb3`, recommendation READY_FOR_G2A.
- Final G2A: GPT-5.6 Sol High, session `019f755f-b8bd-7b50-883a-dc3774ba013c`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T08-17-15-019f755f-b8bd-7b50-883a-dc3774ba013c.jsonl`, last task_complete turn `019f755f-c0b3-7430-ac82-80fa9672480b`, verdict APPROVE for T-04 Gate 2 only.
- Memory Writer: GPT-5.5 Thinking High, session `019f756d-329d-7923-ab24-02777e8a1618`, path `C:\Users\asdasd\.codex\sessions\2026\07\18\rollout-2026-07-18T08-31-58-019f756d-329d-7923-ab24-02777e8a1618.jsonl`, initial completed turn `019f756d-3ad3-7131-981b-f297f3361464`; this thread is distinct from all verifier and G2A sessions above.

### Implementation Documents And Evidence

- Repository implementation documents:
  - `docs/architecture/credential-storage-requirements.md`, SHA-256 `54acf4b1e32c3b79c174fcc9311c81361a3dc8b374851c54034493015c0b4ad1`.
  - `docs/architecture/phase1-status.md`, SHA-256 `b1a0ab31278a39838eb202016d818ce74d1bf5cdba5761cf65d4f7c64ecced48`.
- External wheelhouse evidence path: `C:\Users\asdasd\torq-cli-t04-evidence-35b78d31083e42ef97d5ed7b4e7c726a\offline-wheelhouse-v1`.
- External reconciliation evidence paths:
  - `C:\Users\asdasd\torq-cli-t04-evidence-35b78d31083e42ef97d5ed7b4e7c726a\reconciliation-v2`.
  - `C:\Users\asdasd\torq-cli-t04-evidence-35b78d31083e42ef97d5ed7b4e7c726a\reconciliation-v3`.
  - `C:\Users\asdasd\torq-cli-t04-evidence-35b78d31083e42ef97d5ed7b4e7c726a\reconciliation-v4`.
- v4 external-only correction created exactly five authorized files: `matrix-v2-digest-defect-v4.json`, `architecture-to-document-matrix-v4.json`, `matrix-v4-validation.json`, `v3-repository-equality-v4.json`, and `command-results-v4.jsonl`.
- v4 matrix evidence: v2 matrix preserved at 15,363 bytes, SHA-256 `22972d3b10284dfd997498d18b8985c4a63fc59691ac0cd7cab366b45e1c01bb`; v4 matrix 15,364 bytes, SHA-256 `4fe5a9cedeaea502a08a4aa0f984602447c1cf83406d17baad19090f5fa7bd54`; v4 differs from v2 by exactly one inserted ASCII `1` byte at offset 220.
- Strict v3 inventory evidence: 109 records, canonical digest `4a1543750b805cc57ca6f91888b6bd87e4dae16af71c19ebb134625e428b48bb`, byte-identical pre/final and matching fresh v4 equality proof before this memory update.
- Copy isolation evidence: `copy-local-runner.py` used `-S`, absent `PYTHONPATH`, no site processing, original-source rejection, copy-local module/resource paths, and copy-only pytest collection.

### Final Command And Gate Evidence

Final independent verifier and final G2A evidence record:

- Ruff: pass, `All checks passed!`.
- Mypy: pass, no issues in 14 source files.
- Pytest: 216 total, 212 passed, 4 skipped.
- Findings count: 39.
- Named mutants: 12/12 killed.
- Build: pass, with build-only scoped `PYTHONPATH` to the staged `wheel` archive and removal afterward.
- Wheel smoke: pass, with `PYTHONPATH` absent before smoke, `PIP_NO_INDEX=1`, and `PIP_FIND_LINKS` pointed only at the staged wheelhouse.
- Wheelhouse exact set: `wheel-0.47.0-py3-none-any.whl`, `pyyaml-6.0.3-cp313-cp313-win_amd64.whl`, and `wheelhouse-manifest-v1.json`.
- Wheel source cache bodies matched staged wheels byte-for-byte; wheel metadata, tags, native-extension expectations, and all hashed RECORD entries validated.
- Final G2A read-only gate check exited `0` with matrix `52/52`, inventory `109/109`, tests evidence `216/212/4`, copy isolation true, Foundation hashes true, and v4/document/phase hashes matched.

### Preserved Foundation Hashes

- Policy: `fbeb3e1597a8dde9b4e5c1b5b049605bd3ed767c468a3f75b2e362c3e2f153ca`.
- Registry: `e6c378aa5104b871baf6c6404f13d1373a5ec7cbf486283a0a28dab73da87128`.
- Manifest: `f4255aadddfbe1d3de47d51145c2ea4e8e610dcc1284d017a9abd80d22f68e4b`.
- Prompt provenance: `798c9ba82859da69eb6d38c5b16a47dd9891be48f4310136421c36e6b8e6382b`.
- Role map: `7348b7c15bfece61ecacdeb71a860ad4221425044d39e705cf0ec7b28b4d2cf0`.
- v5 config: `52110d4522b883ee16b313a612441871e802710e75bb754bb5d9707492f27698`.

### Limitations And Residual Risks

- Wheel and pip-cache hashes prove local byte identity only, not signed upstream provenance or immunity from prior cache poisoning.
- No real credential backend, credential material handling, provider validity, production unlock, migration, deployment, release, or runtime credential capability has been demonstrated.
- Local administrators, same-user malware, process memory, swap, core dumps, backups, rollback, synchronization, and lost-passphrase risks remain outside T-04.
- Native three-OS clean-machine CI, branch protection, signing, tag, release, provider configuration, credentials, and billing remain outside this completed T-04 requirements-only slice.
- Historical malformed v2 inventories and truncated v2 matrix are preserved as defect evidence and superseded by v3/v4 evidence; they are not current baselines.
- Luna's abbreviated v2 matrix hash suffix was narratively wrong while artifact hashes were correct; final G2A treated this as nonblocking.
- Historical `tests/test_findings.py` Foundation scope deviation remains disclosed and previously reconciled as nonblocking.

### Remaining Operator-Controlled Actions

The following remain unperformed unless separately recorded by the operator: commit, push, merge, deploy, release, branch protection, provider configuration, credential creation/rotation/revocation/migration, billing, static fixture refresh, and any production unlock.

## Post-G2A T-06A Memory Update

T-06A normalized V5 import is completed and verified. Final G2A verdict is APPROVE for T06A only. Full T06, Phase 1, and the remaining PRD remain incomplete. This record makes no provider, credential, persistence, migration, apply, runtime-effective, deploy, release, CI, branch-protection, or Git-provenance authorization or claim.

Actual role threads: Terra GPT-5.6 Terra High `019f7638-2883-70e3-9225-62a09deee039`; Sol G1R GPT-5.6 Sol High `019f757c-1a87-7080-a2ba-c299daf607d9` APPROVE; Luna GPT-5.6 Luna High `019f734c-0c0f-7391-8c4d-81aa021a8297`; verifier GPT-5.5 `019f7690-2f1a-76f0-b664-ede1c3345b90` READY_FOR_G2A; fresh Sol G2A GPT-5.6 Sol High `019f76a3-b2bb-7d21-b16b-e2e03f78f3e4` APPROVE. A separate OpenAI GPT-5.5 Memory Writer stage performed this append; its thread ID is not recorded here because it was not provided.

Verification: 256 collected; 252 passed, 4 skipped; Ruff pass; mypy pass over 16 sources; mutants 12/12; isolated offline build, wheel smoke, clean installed-wheel, and adversarial probes pass.

Protocol: stdout JSON envelope; embedded `canonical_target_config_utf8` is 1,029 UTF-8 bytes with one final LF and SHA-256 `63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e`; `runtime_effective=false`; `runtime_state=offline_unattested`.

Anchors: G2A report SHA-256 `b01c5dbc1ede38ee2cddd70850da237c77d91c0f13bba1fde2fd85325084f6c6`; verifier manifest SHA-256 `3d6aaee53845e9b9f611768b898cdc64199dcbaba37e33beb4b370b4b469c5e4`; verifier packet SHA-256 `1e3feb688a04ae2b5e040f21a000d0fc392680b8866b882f399756a0c7d1dcc5`.

Governed hashes: `import_v5_config.py` `43917cee061ea774c94327f2a3120d64d48a3aa7ab6e4fcd011b762321263f5c`; `cli.py` `98ad30d509f3bf5c1d71ab1a9e7c010e35cad8e1603e7c83a0b02062f88750da`; `hermetic.py` `1b38b7cc1880c398a7292da03e4d767401763e082c420f2b4b45a617245eca2f`; `test_cli.py` `a8adb6e560638193f745b4ce53617bdbb63869c9d52d0b971fcb843790fe09e7`; `test_hermetic.py` `91699c86c4caa0d9bd7e1072f5a180892f888eba1f85235e734a9c5c6d0197fa`.

Scope: canonical inventory 54/54; serializer correction changed only `cli.py` and `test_cli.py` relative to baseline; Foundation/T04 resources are byte-equal.

Authorized dependency: one HTTPS-only no-install `packaging==26.0` acquisition into offline staging, wheel SHA-256 `b36f1fef9334a5588b4166f8bcd26a14e521f2b55e6b9de3aaa80d3ff7a37529`; it was not copied into the repo or T04 wheelhouse and was not system installed.

Residuals at original T06A approval time included absent native macOS/Linux local execution and absent branch-protection/Git-provenance attestation; post-publication T06A/T06B exact-SHA hosted CI and branch/PR facts are recorded in the T-06B section below. Archive containers vary by metadata/compression while payloads match; G2A did not rerun write-producing suites or directly read two archive binaries.

## Post-G2A T-06B Memory Update

T-06A and T-06B are completed and verified, including post-publication correction commits `28a00589e73f7511912b180883838bbfd680c85c`, `8887bf586660f792ab68dbdf5fcee8808db769f0`, and final verified head `993efbebc08c6fdb8668c7d74d7f1d819a3027f1`. At that T-06B checkpoint, T06C, the remainder of T06, Phase 1, and the full PRD remained incomplete/in progress; the later T-06C update below supersedes only the T06C/T06 implementation status. T06B is the bounded config-parser hardening/config-file-specification slice; it enforces byte-bounded, single-document, node-preflighted YAML before construction and retains opaque known-parser/unexpected-error result behavior.

Pre-publication Gate record: final G1 design packet `terra-g1d-final-v2.md` SHA-256 `b81f7478f95c99a5ac449ed70d20d9c2892c950fb74bbd986ce4bdb0e25ec039`; final Sol G1R verdict `APPROVE`, report SHA-256 `d977b8f4b57cad4432969fcc721394d33e30d51e7fc31d631aef32388934a67c`; final Sol G2A verdict `APPROVE`, report SHA-256 `008cfce4699e94de8fba7bbe80fedfcb0e97ed3bbdaa548b43e24151b825439d`.

Relevant actual roles/threads: Terra G1D GPT-5.6 Terra High `019f7638-2883-70e3-9225-62a09deee039`; Sol G1R records remain preserved, including T06B final G1R GPT-5.6 Sol High `019f76e8-b27a-7132-8b6f-4e95a7a9e121`; Luna Builder GPT-5.6 Luna High `019f734c-0c0f-7391-8c4d-81aa021a8297`; pre-publication final verifier GPT-5.5 Thinking `019f77d2-d717-7672-9d6d-450c7e89dfb7` `READY_FOR_G2A`; pre-publication final Sol G2A GPT-5.6 Sol High `019f77dd-6362-7a73-a710-dab273c5777f` `APPROVE`; prior Memory Writer OpenAI GPT-5.5 Thinking `019f77ed-b5d6-7692-b0fd-012c42dfde74`; genuine post-publication verifier GPT-5.5 Thinking `019f7874-9b3f-7d62-a0c4-291553fa00ea` `READY_FOR_G2A`; post-publication G2A GPT-5.6 Sol High `019f7882-64a8-7d90-94ac-cfdacb1fb2cd` `APPROVE`.

Gate-2 history: Sol G2A `019f778a-6b0d-7b10-94a9-774a9ee62d2b` `REJECT` for bounded B1/B2 correction; Sol G2A `019f77b8-d83f-79c3-829a-a457e9a7f108` `REJECT` for two durable-test gaps; pre-publication final Sol G2A `019f77dd-6362-7a73-a710-dab273c5777f` `APPROVE`; post-publication Sol G2A `019f7882-64a8-7d90-94ac-cfdacb1fb2cd` `APPROVE`.

Pre-publication independent verifier packet SHA-256 `314fd667716b135c49caae8313c398dc7fd3f8c1f65749a6d9d466aa00cfdfb9`; verifier run-manifest SHA-256 `8d3fde3c502bf28224cb1bc56b6485d63ccf8c514e7df78e8fc6a12780e20c3e`; final Luna test-only packet SHA-256 `7962c1072256d5af2c99a3a30a083b191b912f5850b19ec5469caba338cf150e`.

Post-publication repository publication state: branch `agent/t06a-t06b-verified` was pushed; draft PR #1 exists at `https://github.com/pilotwaffle/Torq-CLI/pull/1`; `main` remains unchanged at `be676e987163baa2b06ce9df49ba6c1110149c0c`; no merge, deploy, or release occurred.

Post-publication exact-SHA CI: push run `29671988290` passed 4/4; pull request run `29671989395` passed 4/4. Windows, macOS, Linux, and headless Linux each passed the workflow sequence: Ruff, mypy, full pytest, named mutants, build, and wheel smoke.

Post-publication final independent evidence at `993efbebc08c6fdb8668c7d74d7f1d819a3027f1`: focused pytest `96 passed`; full pytest `360 passed, 4 skipped`, `364 collected`; Ruff pass; mypy pass for 16 sources; named mutants `14/14` in default and explicit-root modes; offline build, wheel smoke, and archive/resource audit passed.

Final anchors: `tests/test_config_schema.py` SHA-256 `ecadc6b941b5efc2bdf05dae36426d1c0572a0c83a70feeea671ebc4fbbfd42d`; exact T06A normalized target remains 1,029 bytes, final LF, SHA-256 `63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e`.

Limitations/residuals at the T-06B checkpoint: GPT-5.5 did not run native macOS/Linux locally; exact-SHA Actions did. Wheel-container hashes may vary by checkout/build environment while governed bytes and behavior match. Node 20 deprecation annotations are non-blocking future maintenance. Luna's accidental read-only `MEMORY.md` output made no change and did not contaminate independent verification. Raw Console YAML and without-manual-translation proof remained operator-gated T06C at that checkpoint; the later local T-06C update below supersedes that implementation-status statement. Symbolic defaults remain documentation-only with no runtime discovery/native three-OS evidence; credential references prove syntax only; provider runtime, credentials, persistence/migration execution, concurrency, deployment/release, and production runtime remain unverified. Existing local schema/eligibility metadata disclosure remains outside the fixed parser-envelope scope.

Operator gates remain preserved for merge, deployment, release, provider configuration, credentials, billing, irreversible/destructive actions, and any broader T06/T06C/Phase 1/PRD work.

## T-06C Local Implementation Update

T-06C raw Console V5 YAML import is implemented and locally verified as a
read-only continuation of T-06A/T-06B. This update records implementation and
local evidence only. No independent G1/G2A approval, new hosted CI run, Git
commit, push, merge, deployment, release, provider configuration, credential
capability, persistence, migration, or runtime-effectiveness claim is made.

The new command is `torq config import-v5-console --config ABSOLUTE_PATH`. It
uses the existing protected, bounded native reader and a dedicated
sequence-aware hardened YAML parser. The parser accepts the sequences required
by raw Console V5 data while enforcing strict UTF-8, no BOM, a 65,536-byte
maximum, one mapping document, a 1,024-event maximum, depth 8, unique
NFC-normalized string keys, standard tags only, and no explicit tags, anchors,
aliases, unquoted merge keys, or recursively normalized secret-shaped keys.
The canonical TORQ-CLI v1 config parser remains unchanged and continues to
reject sequences.

The actual 3,120-byte
`E:\TORQ-CONSOLE\.torq\v5\config.yaml` was checked read-only against the new
domain parser on 2026-07-23: `parse_finding=None` and
`mapping_finding=None` against the authenticated normalized oracle. Production
CLI access to protected upstream roots remains denied; the command operates on
an explicitly supplied safe absolute path and performs no path discovery.

Successful import emits the existing fixed 1,029-byte canonical target with
SHA-256 `63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e`,
profile `torq-v5-repo-compat` version `1.0.0`,
`runtime_effective=false`, and `runtime_state=offline_unattested`. It does not
write files, copy wrapper names or endpoints into the target, resolve
credentials, call providers, or execute agents.

Fresh local verification before this memory append:

- pytest: 380 collected; 376 passed, 4 skipped;
- Ruff: `All checks passed!`;
- mypy: no issues in 18 source files;
- named mutants: 14/14 killed;
- isolated sdist and wheel build: passed;
- sdist fixture-presence audit: passed;
- clean installed-wheel smoke: passed;
- installed `import-v5-console` smoke: exit 0, command
  `config_import_v5_console`, status `ok`, canonical SHA matched.

Governed T-06C hashes at this checkpoint:

- `src/torq_cli/domain/v5_console_import.py`:
  `366266c9f734f2b85cca405f3ca48b1db40879ff15dce1072e722f7da837eac7`;
- `src/torq_cli/application/import_v5_console.py`:
  `72a9f9ded34a882bcda1ab2edfd3c2144f031d115588c681a3228fffd1461d65`;
- `src/torq_cli/interfaces/cli.py`:
  `37cfa12763fb8f3c4e0366272bc3e768dec13df09e76e66d8e8e7ac950565573`;
- `tests/test_v5_console_import.py`:
  `4513b38923b827fd65f229fd5ada53c28d2517c5e2febafa7799be0367c1e681`;
- sanitized Console fixture:
  `aa0a15f8d761a8eafacf681a3ff61f3efd190937a46f54708aecb94b60811371`;
- `MANIFEST.in`:
  `7739a2ad1e1102169e87c97dd42dbc8623e85e3797941ffe528e863e2d5069a3`;
- `docs/architecture/t06-console-import-spec.md`:
  `81f1b023bcece88048fb5b57e7ca574b6af635fc9bde1e88875a03c53f739db7`.

Repository-control limitation: `E:\Torq-CLI` is untracked inside the unrelated
`E:\` Git repository on `master`, so no safe standalone commit or diff exists
for this local checkpoint. All cleanup-producing verification ran under
`E:\Torq-CLI\tmp`; no file outside a temporary folder was deleted. T-02,
T-03, T-05, T-07 completion, the Phase 1 gate, T-08 through T-36, and all
operator-controlled actions remain incomplete, separately gated, or
unverified as applicable.
