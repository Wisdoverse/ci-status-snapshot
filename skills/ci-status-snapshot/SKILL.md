---
name: ci-status-snapshot
description: Token-efficient GitLab CI, GitHub Checks, MR, and PR status handling with one-shot snapshots or a silent local watcher that notifies the agent only when state changes. Use when the user asks whether CI passed, wants an active goal to continue through CI/review waits, asks to merge after checks, wants provider auto-merge, or corrects model-driven polling. Also use proactively, without being asked, whenever you are about to wait on a PR, MR or pipeline you created or pushed to (checks, review or merge), or are about to sleep, re-run a status command, or otherwise poll for CI.
---

# CI Status Snapshot

`$SKILL_DIR` means this skill's install directory — the one holding this `SKILL.md`. Installed as a plugin it is the host's plugin cache directory, which Claude Code and Codex report when the skill loads; copied manually it is `~/.claude/skills/ci-status-snapshot` or `~/.codex/skills/ci-status-snapshot`. Substitute it or export it once; never hardcode an install layout.

This file is the always-loaded contract. Two playbooks stay unloaded until their trigger fires: `$SKILL_DIR/references/gitlab-triage.md` (failed/stuck GitLab jobs) and `$SKILL_DIR/references/merge-flow.md` (merge and auto-merge delegation).

## Goal

Use compact one-shot snapshots for immediate questions. When an active goal depends on pending CI or review, arm the bundled local watcher in an attached deferred tool call — it polls without model turns and emits one compact event when state changes — and let provider-side auto-merge own the merge itself. Efficiency here means token budget control: fewer CLI calls, narrower reads, fewer chat turns.

## Agent/Watcher Contract

The authoritative rule list. Everything below is binding.

- Hard rule: the agent never polls remote CI in model turns. No repeated provider queries, `--watch` commands that stream progress into chat, scheduled goal turns or continuations, subagents, reminders, terminal streams, status narration, or manual "check again" cycles. The local watcher owns polling and emits one exit event; collecting that existing tool's result follows the runtime rules below.
- Choose one one-shot helper per decision point: snapshot for status, delegate for GitLab merge intent. Do not chain both over unchanged state. A mutation response with the needed fields already counts as fresh evidence. Start at most one watcher per repository + PR/MR + head; retain its tool handle across context handoffs.
- Treat `WAIT` and `delegated_auto_merge` as watcher-arm outcomes when an active Goal depends on later state. Prefer provider-side auto-merge over local polling whenever possible.
- Run only `scripts/ci_state_watch.py` for local waiting. Keep it attached to a tool invocation with a supported result-delivery mechanism; do not use `nohup`, `&`, detached terminal sessions, or polling subagents.
- Pass `--expected-head` with the full SHA from the handoff snapshot (`head_sha`, or delegate `sha`). The first read emits immediately if that head changed even when it is still `WAIT`, or if the state is already `ACTION`/`DONE`. Without a prior snapshot, the argument is optional and the first `WAIT` becomes the baseline.
- The watcher stays silent until a decision-relevant fingerprint changes — conclusion, head, target branch, PR/MR state, review gate, draft, or the GitLab head pipeline appearing, starting or being replaced — or until auto-merge is cancelled. Enabling auto-merge, per-job progress, a pipeline moving between not-yet-started states and transient merge-gate churn do not wake the model. A pipeline finishing wakes only through the conclusion (failure as `ACTION`, a mergeable success as `DONE`); a green pipeline still waiting on approval stays silent, and the approval wakes it through the merge gate. It also exits after three consecutive errors or timeout; these are watcher failures, not CI failures.
- Inspect every tool return before waiting: a completed event must be handled immediately using the table below, even when the exit code is `0`; only a still-running handle needs result collection. Never substitute a timed sleep for collecting or handling the result.
- Treat `event.current` as the fresh snapshot; do not immediately query the same state again. Re-read only when an exact-head mutation such as merge requires it. Each watcher exits after its event: retire the completed handle, and confirm the previous watcher exited before starting a replacement. Reuse a still-running watcher for the same target and head across interruptions.
- Re-check remote state only after an authorized state change (push, job retry/cancellation, or auto-merge enablement), on a watcher event, or when the user explicitly asks for a fresh snapshot. Reuse sufficient mutation/event fields; otherwise take one bounded read, not a new polling loop.
- Never end, complete, or block an unfinished Goal merely because CI/review is `WAIT`. Continue independent work, then collect the attached watcher's result through the supported runtime mechanism. If that mechanism is unavailable, report the monitoring limitation and preserve the unfinished target/head; do not claim an armed watcher or promise automatic resumption.
- When the user says polling is wasting tokens, stop model-driven polling and move the wait into the silent local watcher.

## Silent Local Watch

```bash
python3 "$SKILL_DIR/scripts/ci_state_watch.py" \
  --provider github \
  --selector <pr-number-or-url> \
  --expected-head <full-sha-from-snapshot> \
  --interval-seconds 30
```

Use `--provider gitlab` for an MR, from that repository's checkout. `--selector` is required: without it a checkout change could retarget the watch. Omitting `--timeout-seconds` means a finite **7200 seconds**, not an infinite wait; only explicit `0` disables the backstop, and requires guaranteed runtime cleanup. Keep the existing 30-second cadence unless there is measured reason to change it.

Watcher exit codes: `0` = one decision-relevant change (`event: change`), `2` = three consecutive snapshot errors (`event: error`), `3` = timeout backstop hit (`event: timeout`). Exactly one JSON event is printed in every case.

Choose the result-delivery mechanism from the actual runtime's tool contract before launching:

- If attached background execution explicitly delivers completion notifications, use it and consume that notification once. Claude Code's `Bash` with `run_in_background: true` is an option when the tool exposes that contract. Do not assume every host or background process can wake the agent.
- If execution instead returns a session/cell handle that requires retrieval, retain it and use the documented result tool (for example, `wait` or `write_stdin`) on that same handle. Prefer bounded blocking retrieval over rapid empty reads; where orchestration is available, keep result collection inside one attached invocation. This collects the running watcher, not a new remote snapshot. Never call a result tool after its completion has been collected.
- Report "watcher armed" only after launch returns a live handle with a supported delivery/retrieval path. A launch that already returned an event needs handling, not arming. Promise automatic notification only when the runtime supports it. Avoid foreground calls whose execution cap is shorter than the intended watch.

### Handle the exit event

Parse the JSON before choosing the next action. Exit `0` means an event was emitted, not that CI passed or the requested merge happened.

| Event | Next action |
| --- | --- |
| `change`, `current.conclusion: ACTION` | Triage the supplied failure/blocker now. Do not sleep or re-arm against the same unresolved `ACTION`. After an authorized fix/retry, use the resulting state to continue. |
| `change`, `current.conclusion: WAIT` | Act on the changed head, target, gate or delegation state as needed. If the task still depends on later state, arm one replacement watcher using the resulting head; do not re-enable auto-merge after someone explicitly cancelled it without authorization. |
| `change`, `current.conclusion: DONE` | Check the requested outcome. An open mergeable PR/MR still needs the authorized merge; closed-unmerged does not satisfy a merge request. Report CI separately from merge state. |
| `error`, or missing/invalid event output | Treat this as a watcher failure, not a CI failure. Use `watch` or the retained handle's target; `last_snapshot`, if present, is stale context. Diagnose once, then re-arm only if resolved and still needed; otherwise report the monitoring blocker. Never loop retries or silently leave unfinished work without an event source. |
| `timeout` | Monitoring ended; `last_snapshot` is stale and does not prove CI failed. Take one bounded diagnostic read. Continue with a finite watch only when expected duration/progress and the task's remaining wait budget justify it; otherwise report the elapsed-limit or stuck-work blocker. Another `WAIT` alone is not resolution, and timeout must not become an indefinite restart loop. |

## Snapshot First

Prefer the bundled snapshot helper when a one-shot status is enough:

```bash
python3 "$SKILL_DIR/scripts/ci_status_snapshot.py"
```

Use `--provider github` or `--provider gitlab` when auto-detection is wrong. Use `--selector <number|url|branch>` to check a specific PR/MR.

Use the bundled GitLab auto-merge delegation helper when the user asks to inspect MR merge/CI delegation state:

```bash
python3 "$SKILL_DIR/scripts/ci_merge_delegate.py" \
  --provider gitlab \
  --selector <mr-iid-or-url> \
  --json
```

Pass `--project <id-or-path>` when the project cannot be inferred from the git remote. The helper never waits: with CI running and server-side auto-merge enabled it emits `delegated_auto_merge` and exits. Add `--enable` to turn GitLab auto-merge on for the MR's current head — at most one guarded PUT, confirmed by one re-read; read `$SKILL_DIR/references/merge-flow.md` before using it.

Delegate exit codes: `0` = nothing to do now, `2` = terminal pipeline state, triage the failed jobs, `3` = action required (with `--enable` also `merged_before_ci`, and `enable_refused`/`enable_not_confirmed`, which name a `reason`), `4` = `api_error`. Exit `0` does not mean merged — always read `result`, and treat a `failed_jobs_error` field as "job list unavailable", never as zero failed jobs.

When the user asks to submit, merge, "merge when CI passes", or to delegate the merge, read `$SKILL_DIR/references/merge-flow.md` first: merge flow, delegation-helper contract, per-exit-code result names, compact `glab api ... | jq` MR field snapshot, GitLab host/legacy caveats.

If the helper cannot run, take one manual snapshot:

- GitHub: `gh pr view --json number,title,state,url,headRefName,headRefOid,baseRefName,isDraft,reviewDecision,mergeStateStatus,autoMergeRequest,statusCheckRollup`
- GitHub checks only: `gh pr checks --watch=false` or `gh pr checks <selector>` once.
- GitLab: `glab mr view --output json` once, or use `glab api` with `jq` to print only the fields needed for the decision.
- GitLab pipeline only: `glab pipeline list --ref <branch> --per-page 5` or `glab pipeline view <id>` once.

If CLI flags differ on the host, run the relevant `--help` command once and adapt. Do not spend turns rediscovering the same flags.

## Classify

Return one of three outcomes:

- `ACTION`: failed or canceled CI, merge conflict, required manual job, rejected review, branch needs rebase/update, or a concrete blocker that can be fixed now.
- `WAIT`: CI is running/pending/queued or has not appeared yet, the PR/MR is still a draft, review or approval is required, merge-when-pipeline-succeeds/auto-merge is enabled, or there is no actionable failure yet. Arm the local watcher when an active Goal depends on later state.
- `DONE`: merged, closed intentionally, or all checks are green and no obvious remote blocker remains. The helpers require positive merge evidence for that last case — GitHub `mergeStateStatus` `CLEAN`/`HAS_HOOKS`, GitLab `detailed_merge_status: mergeable`. A green pipeline alone is never `DONE`; a still-running pipeline stays `WAIT` even when the GitLab gate already reports `mergeable` (CI is not a required merge check there); and an unrecognized provider state is `WAIT`.

Missing CI is `WAIT`, not `DONE`: right after a push GitLab can report `mergeable` before it creates the head pipeline (reason `no pipeline observed`), and GitHub can report `CLEAN` before a single check run registers (`no checks reported yet`). The watcher follows that state until CI appears. Pass `--allow-no-pipeline` to the snapshot, watcher and delegate only for a project that runs no CI for this PR/MR; an identified pipeline with an empty or unrecognized status stays `WAIT` regardless.

`DONE` is the MR/PR decision, not blanket CI or delivery acceptance: merged/closed takes precedence in the classifier. Always report `ci` separately; merged with failed/pending CI does not mean validation passed, and closed-unmerged does not satisfy a merge request. Cleanup still requires exact merged-head and clean-worktree proof.

For `ACTION`, fetch only the failed job logs needed for the next fix. Summarize the failing lines; do not paste full logs unless the user asks. Before triaging a failed, canceled, or manual GitLab pipeline — or a job that looks stuck — read `$SKILL_DIR/references/gitlab-triage.md` (fetch recipes, infrastructure-vs-code-defect rule, never-auto-retry-lint/test rule, stuck-runner flow).

For `WAIT`, arm the watcher instead of taking another snapshot. If the user requested merge, first enable merge-when-pipeline-succeeds or auto-merge when policy and permissions allow (GitLab: `ci_merge_delegate.py --enable`).

For `DONE`, report the result and avoid extra remote calls unless the requested merge still needs to be performed under `$SKILL_DIR/references/merge-flow.md`.

## Token Rules

Fetch and reporting discipline, on top of the contract above.

- Fetch narrowly: structured JSON fields over full command output, only the fields the decision needs, and the shortest useful trace tail. Never fetch logs for successful, pending, or running jobs; skipped jobs have no useful trace — fetch metadata only.
- If a pipeline reaches failed, canceled, or manual — or GitLab reports it skipped while the merge gate is `ci_must_pass` (or a legacy server reports no `detailed_merge_status` at all) — stop status checks and switch to failed-job triage. A skipped pipeline under any other gate (`mergeable`, `not_approved`, …) is not a failure. GitHub check-level skipped/neutral conclusions are non-blocking and count as done.
- Prefer local validation and focused fixes over waiting for remote reruns.
- When many MRs are open, process one mergeable MR at a time in dependency order. Do not refresh every MR after every merge unless the base branch changed in a way that can affect them.
- Keep final reports compact: current conclusion, status evidence, and the next useful action.
- Read a `references/` file only when its trigger condition is met, and only the one that applies.

## Limitations

Provider auto-detection matches the literal strings `github`/`gitlab` in the `origin` URL, so self-hosted hosts (`git.example.com`) need an explicit `--provider`. GitLab host-routing and legacy-server caveats are in `$SKILL_DIR/references/merge-flow.md`.

## Response Shape

Use this shape for terse operator updates:

```text
Conclusion: WAIT (watcher armed; Goal active)
Current state: PR #123 is open; checks are running; review is still required.
Action: auto-merge is enabled; a local script is polling silently without model turns.
Next: consume the watcher's single exit event through the confirmed runtime delivery/retrieval path.
```

Match the user's language in the final answer. For Chinese users, use `结论`, `当前状态`, `处理`, and `下一步`.
