# What people like about Antigravity and the Codex app, and what TORQ should take from it

Status: research and recommendations, not implemented. Written 2026-09-17. Scope: TORQ CLI in E:/Torq-CLI at origin/main cb93b4c.

Method: a read-only survey of the repository as it ships today, then agent-reach research (Exa through mcporter, Reddit through OpenCLI, vendor documentation reads). The community sample is purposive and qualitative. Praise is reported as what the poster said on the date shown, not as a measured result. Vendor documentation confirms a feature exists; it is not evidence that users value it.

## What TORQ ships today

Facts from source, with paths.

- **Entry.** The dashboard opens on the Fleet view. The New task view is hidden unless `torq fleet --serve` is started with `--task-project ID=PATH`, `--task-provider`, `--task-model`, and for Claude `--task-claude-bin` (`interfaces/fleet_http.py:607-638`, `interfaces/cli.py:387-607`). There is no way to open a project from the browser.
- **Build.** Start building dispatches exactly one provider request with tools disabled: `--tools "" --permission-mode plan --no-session-persistence` for Claude, a bridge script for the others (`adapters/candidate_provider.py:98-129`). The provider returns JSON create and replace operations, which TORQ writes into an isolated candidate directory, never the project (`application/candidate_tasks.py:534-675`, `safety/task_workspace.py`).
- **Checks.** The only check is `structural-v1`: Python files must parse, JSON must load, Markdown and text must be UTF-8. Nothing is imported, executed, or tested (`checkers/structural.py`).
- **Review.** Plan, Changes, and Checks views; Request a correction takes free text and spawns a child candidate against the same base; Accept records a signed decision and changes nothing on disk (`application/candidate_review.py:164-257, 659-724`).
- **Apply.** The one step that writes to the project: anchored atomic create and replace, journaled, with rollback evidence and a recovery-required block after interruption. Bounded to 32 files, 64 KiB per file, 2 MiB total, UTF-8 only, no delete, rename, or directory creation, no shell, no tests, no git (`candidate_review.py:726-1071`, `docs/plans/2026-09-16-dashboard-p3-contract.md:20, 84`).
- **Evidence.** Every step is a hash-chained, signed, encrypted receipt verifiable offline with `torq evidence verify`. The anchor is same-volume, so it resists outsiders but is not tamper-evident against a privileged insider, and provenance is a local session, not a named person (`docs/security/threat-model.md:57-126`).
- **Alerts.** Fleet already offers browser notifications for newly open operator actions (`data/fleet/fleet.js:773-789`).
- **Platform.** Windows is the production target with Job Object containment (`adapters/windows_job.py`). Linux containment is experimental and macOS has none (`ROADMAP.md:6-17`).

## What people say they like

### Antigravity

| Source and date | What was praised | TORQ today |
|---|---|---|
| [Replicating Antigravity's "brain + plan" workflow](https://www.reddit.com/r/google_antigravity/comments/1rtfwog/replicating_antigravitys_brain_plan_workflow_in/), 2026-03-14 | "The fact I can comment directly on the artifacts it generates, basically doing code reviews on the implementation plans or the output code. This single feature is what keeps me from switching." | Correction is a free-text box, not line-anchored. |
| [Honest review of Antigravity vs Cursor vs Claude Code vs Copilot](https://www.reddit.com/r/google_antigravity/comments/1q1tx8j/this_is_my_honest_review_of_antigravity_vs_cursor/), 2026-01-02 | Planning mode "knows my code base well"; a reply: "Antigravity = actual engineering." | Plan is a hashed snapshot of the input paths the user typed, not a codebase-aware plan. |
| [AG vs alternatives](https://www.reddit.com/r/google_antigravity/comments/1tglffj/ag_vs_alternatives_what_does_it_actually_do_well/), 2026-05-18 | Inline editing and pair programming; cross-file context. | No editing surface; no context beyond declared input paths. |
| [Artifact review docs](https://www.antigravity.google/docs/artifact-review/), vendor, undated | Planning mode versus Fast mode; review policy "always ask" versus "always proceed"; the agent pauses before any local file is written. | Always plan, always review, always isolated. The pause-before-write guarantee is stronger than Antigravity's. |
| [Artifacts docs](https://www.antigravity.google/docs/artifacts/), vendor, undated | Browser recordings and diffs as deliverables, reviewed at milestones rather than watching every tool call. | Changes and Checks exist; there is no verification artifact beyond parse results. |

Counterevidence in the same threads: throttling, "servers busy", weekly limits on the Pro plan, and sharp disagreement about model quality ([Antigravity vs Cursor vs Claude Code](https://www.reddit.com/r/google_antigravity/comments/1sg2vip/antigravity_vs_cursor_vs_claude_code/), 2026-04-08). None of the praise is about the model; it is about the plan-and-review loop.

### Codex app

| Source and date | What was praised | TORQ today |
|---|---|---|
| [Anyone else really enjoying the Codex app?](https://www.reddit.com/r/codex/comments/1r6x43w/anyone_else_really_enjoying_the_codex_app/), 2026-02-17 | "Just enough functionality in its UI"; blends text and code "like Jupyter"; works across two or three conversations at once. | Fleet view leads with governance telemetry; one task at a time per project in practice. |
| [Codex App Review](https://www.reddit.com/r/codex/comments/1qvc1qf/codex_app_review/), 2026-02-04 | Projects in one place; completion notifications "I really needed"; wants all limits shown consistently. | Projects come from CLI flags; alerts exist for operator actions, not task completion. |
| [Awesome Agents review](https://awesomeagents.ai/reviews/review-openai-codex-app/), 2026-02-27 | Diff review "closer to reviewing a pull request": comment on lines, stage or revert chunks, commit in-app. Mid-task steering. Automations with a review queue. | Whole-candidate accept only; one-shot build with no steering; no automations. |
| [Automations, triggers and the review queue](https://codex.danielvaughan.com/2026/04/08/codex-desktop-automations/), 2026-04-08 | Worktree isolation per thread; Triage inbox where runs with nothing to report auto-archive; sandbox modes read-only, workspace-write, full access; admin-enforced requirements file. Windows support arrived 2026-03-04. | Candidate directories are already per-task isolation. No inbox. Policy is fixed rather than selectable. Windows is already the primary target. |
| [OpenAI automations docs](https://developers.openai.com/codex/app/automations), vendor, undated | Unattended runs land findings in Triage; skills define the action. | `torq demo` and `torq run` are scriptable but nothing schedules them or collects results. |

Counterevidence: usage limits are "the single most common complaint", cloud execution is "a non-starter for many enterprises", and the app was macOS-only for its first month. These are the gaps TORQ's local-first, bring-your-own-provider, Windows-native design already answers.

## What the praise has in common

Across both products, the loved features are the same four things, and none of them is the model:

1. A plan the user can read and mark up before anything is written.
2. A review of changes that feels like a pull request: per line, per hunk, with the evidence beside it.
3. Isolation that makes parallel work safe.
4. An inbox that says what needs a human, and stays quiet otherwise.

TORQ already has stronger guarantees on 1 and 3 than either product. It is behind on 2 and 4, and behind both on the one thing neither community praises but everyone assumes: the agent can actually run the code.

## Recommendations, ranked by leverage

Effort labels are relative and are my opinion. No calendar estimate is asserted.

### 1. Run the project's real checks inside the candidate

Fact: the only check today is a parse. Opinion: this is the largest credibility gap. Reviewers of Codex and Cursor value screenshots, logs, and CI results attached to the change; TORQ's evidence chain is the ideal place to hold a test command, its exit code, and its captured output, yet it holds none.

Do: add a `checks-v1` stage that runs a per-project allow-listed command (for example `pytest -q`) inside the candidate directory under the existing Windows Job Object containment, with a time and output cap, and records command, exit code, and truncated output as evidence. Show it in the Checks view as command and exit code, never a badge. Keep it off by default until the project opts in. Effort: medium. Risk: the containment must hold for arbitrary test code; the Linux and macOS gaps in the roadmap mean this is Windows-only at first, and the UI must say so.

### 2. Line-anchored comments on Plan and Changes

Fact: the single most-cited Antigravity feature is commenting on the plan or diff, and TORQ's correction path is free text. Do: let the user attach a comment to a plan line or a diff hunk; assemble the comments into the correction prompt; record them in the review evidence with their anchors. The child-candidate mechanism already exists, so this is mostly UI plus prompt assembly. Effort: small to medium.

### 3. Open a project from the browser and make New task the default when one exists

Fact: reaching the New task view requires four CLI flags, and the plan's own P0 finding about separate Open project and New task is still not delivered. Do: persist task projects and provider settings in the config profile, add an Open project action that validates the path server-side against the approved roots, and land on New task when at least one project is configured. Keep Fleet one click away. Effort: small to medium. This is the cheapest change with the largest first-impression effect.

### 4. A cross-project inbox

Fact: Fleet alerts fire for operator actions, and tasks with nothing to do still sit in the list. Do: one inbox across projects with three rows that need a person: Needs review, Ready to apply, Recovery required. Completed tasks with nothing pending auto-archive out of the inbox, matching the Codex Triage behaviour that reviewers single out. Wire completion and needs-review to the existing notification path. Effort: small.

### 5. Widen Apply within policy, not without it

Fact: Apply refuses directory creation, delete, rename, and anything over 32 files or 64 KiB per file. Opinion: ordinary features will hit these limits within a week of real use, and the fallback is the user doing it by hand, which defeats the evidence chain. Do: add directory creation and delete as journaled, rollback-capable operations; make the size limits a per-project policy with the current values as defaults; surface the policy in the plan view so the user sees the ceiling before building. Effort: medium. Rename can wait.

### 6. Selectable execution policy per task

Fact: Antigravity offers Planning versus Fast and an always-ask versus always-proceed review policy; Reddit's plan-mode thread complains about ceremony on small requests. Do: keep isolation and evidence mandatory, but let a task skip the plan-review pause and go straight to candidate-ready for small scoped goals. The policy choice is itself recorded in evidence. Effort: small.

### 7. Parallel tasks per project with drift detection

Fact: candidates are already isolated per task id, which is the worktree property Codex users praise. What is missing is the UI and the conflict rule. Do: allow several tasks in flight per project; when a candidate's recorded base no longer matches the project, show a "base drifted" state before Apply and offer a rebuild. Effort: medium.

### 8. Steering during a build

Fact: a build is one provider request, so there is nothing to steer. Codex reviewers call mid-task steering the thing that changed the delegation model. Do: this depends on the live runtime rather than the candidate provider, and on a staged generation contract. Defer until 1 through 4 are in, then design it as a P4 with its own contract. Effort: large.

### 9. Scheduled candidate builds with results in the inbox

Fact: `torq demo` and `torq run` are scriptable. Codex automations are the feature reviewers call "quietly changes how you work". Opinion: unattended runs are the strongest case for an evidence chain, so this fits TORQ better than it fits Codex. Do: a schedule that starts a candidate build from a saved goal and drops the result in the inbox; nothing is applied unattended. Effort: medium, after 4.

### 10. Say what TORQ is, in the words the competition's critics use

The most common complaints about Codex and Antigravity are usage limits, cloud execution, platform exclusivity, and not knowing what the agent did. TORQ is local-first, bring-your-own-provider, Windows-native, and offline-verifiable. The README should lead with those four sentences. Effort: trivial.

## What would change these recommendations

- If the Windows Job Object containment cannot be trusted with arbitrary test code, recommendation 1 becomes a design task before it is a build task.
- If moderated sessions with target users show they do not read plans, recommendation 2 drops below 4.
- If the project is meant to stay an operations viewer rather than a build tool, recommendations 5 through 9 do not apply.

## Not verified here

No provider was called, no live task was run, and no browser session was tested during this review. The tests the repository's own P3 verification cites were not re-run for this document.
