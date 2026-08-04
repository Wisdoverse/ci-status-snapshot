---
name: ci-status-snapshot
description: Token-efficient GitLab CI, GitHub Checks, MR, and PR status handling with one-shot snapshots or a silent local watcher that notifies the agent only when state changes. Use when the user asks whether CI passed, wants an active goal to continue through CI/review waits, asks to merge after checks, wants provider auto-merge, or corrects model-driven polling.
---

# CI Status Snapshot

## Goal

Use compact one-shot snapshots for immediate questions. When an active goal depends on pending CI or review, start the bundled local watcher in an attached deferred tool call. The script performs the polling without model turns and emits one compact event when state changes.

High efficiency means token efficiency: no repeated assistant turns, scheduled continuations, status narration, or agent/subagent polling. Provider auto-merge can own the merge; the local watcher owns notification when continued work depends on observing the result.

Hard rule: the agent never runs the polling loop. Do not use repeated tool calls, `--watch` commands that stream progress into chat, scheduled goal turns, subagents, or manual "check again" cycles. A deterministic local script may poll while the model is idle, provided it stays silent and the attached tool runtime sends exactly one completion notification.

Interpret `WAIT` as "this CI-dependent step is paused." It is not a terminal Goal result and is not a reason to mark a Goal complete or blocked. Continue independent Goal work; if none remains, keep the Goal active and let the watcher notification resume it.

## Agent/Watcher Contract

- Use each one-shot helper once per decision point. Start at most one watcher per PR/MR head; it exits on the first decision-relevant change.
- Treat `WAIT` and `delegated_auto_merge` as watcher-arm outcomes when an active Goal depends on later state.
- Run only `scripts/ci_state_watch.py` for local waiting. Keep it attached to a deferred tool call; do not use `nohup`, `&`, detached terminal sessions, or polling subagents.
- The watcher treats its first `WAIT` read as the known baseline, emits nothing while the decision-relevant fingerprint (conclusion, head SHA, PR/MR state, review decision on GitHub — GitLab approvals surface via the merge gate — draft flag, merge-gate state, auto-merge flag) is unchanged, and exits with one JSON event after a material change, three consecutive errors, or timeout. If the first read is already `ACTION`/`DONE` (state moved between snapshot and arm), it emits immediately with `"initial": true` instead of hanging on a terminal baseline.
- After the tool yields control, do not call wait/poll methods to inspect it. Continue useful work and consume the runtime's completion notification when it arrives.
- Treat `event.current` in that notification as the fresh snapshot; do not immediately query the same state again. Re-read only when an exact-head mutation such as merge requires it.
- Never end, complete, or block an unfinished Goal merely because CI/review is `WAIT`.
- For failed/canceled/manual CI, stop status checking and switch to triage: fetch only the failed job metadata and the shortest useful trace tail.
- For possible stuck runner jobs, take one bounded diagnostic snapshot only when the current snapshot or user report already indicates a long-running anomaly. Retry only a clearly infra-stuck job at most once, then take one final compact snapshot and stop.

## Silent Local Watch

Use the bundled watcher after a snapshot is `WAIT` and later state matters to the active Goal:

```bash
python3 ~/.codex/skills/ci-status-snapshot/scripts/ci_state_watch.py \
  --provider github \
  --selector <pr-number-or-url> \
  --interval-seconds 30
```

(On Claude Code hosts the same files are reachable at `~/.claude/skills/ci-status-snapshot/` — it is a symlink to the `~/.codex` install.)

Use `--provider gitlab` for an MR. The default timeout is 7200 seconds as an orphan-process backstop; pass `--timeout-seconds 0` only when the surrounding runtime guarantees cleanup. The script takes its first `WAIT` read as a silent baseline and prints one JSON event only when the decision-relevant fingerprint changes; a first read that is already `ACTION`/`DONE` emits immediately.

Launch it through one deferred/attached tool invocation whose runtime can deliver a completion notification:

- Codex: one attached tool invocation that waits internally on the process and calls the runtime's notification primitive once when it exits.
- Claude Code: `Bash` with `run_in_background: true` — the process keeps running across turns and re-invokes the agent once when it exits. Do not run it as a foreground Bash call: the foreground 10-minute cap kills a long wait and wastes an error-handling turn.

In either runtime it is not acceptable to resume the model periodically to call `wait`, `write_stdin`, or another snapshot command.

## Snapshot First

Prefer the bundled snapshot helper when a one-shot status is enough:

```bash
python3 ~/.codex/skills/ci-status-snapshot/scripts/ci_status_snapshot.py
```

Use `--provider github` or `--provider gitlab` when auto-detection is wrong. Use `--selector <number|url|branch>` to check a specific PR/MR.

Use the bundled GitLab auto-merge delegation helper when the user asks to inspect MR merge/CI delegation state:

```bash
python3 ~/.codex/skills/ci-status-snapshot/scripts/ci_merge_delegate.py \
  --provider gitlab \
  --selector <mr-iid-or-url> \
  --json
```

Pass `--project <id-or-path>` when the script cannot infer the GitLab project from the current git remote. This helper never waits: if CI is still running and server-side auto-merge is enabled, it emits `delegated_auto_merge` and exits.

If the helper cannot run, take one manual snapshot:

- GitHub: `gh pr view --json number,title,state,url,headRefName,headRefOid,baseRefName,isDraft,reviewDecision,mergeStateStatus,autoMergeRequest,statusCheckRollup`
- GitHub checks only: `gh pr checks --watch=false` or `gh pr checks <selector>` once.
- GitLab: `glab mr view --output json` once, or use `glab api` with `jq` to print only the fields needed for the decision.
- GitLab pipeline only: `glab pipeline list --ref <branch> --per-page 5` or `glab pipeline view <id>` once.

If CLI flags differ on the host, run the relevant `--help` command once and adapt. Do not spend turns rediscovering the same flags.

For GitLab MRs, prefer a compact field snapshot when reporting to the user:

```bash
glab api "projects/<project_id>/merge_requests/<iid>" \
  | jq -r '[
      "state="+.state,
      "sha="+.sha,
      "auto_merge="+(.merge_when_pipeline_succeeds|tostring),
      "merge_status="+.detailed_merge_status,
      "pipeline_id="+(.head_pipeline.id|tostring),
      "pipeline_status="+.head_pipeline.status,
      "url="+.web_url
    ] | .[]'
```

## Classify

Return one of three outcomes:

- `ACTION`: failed or canceled CI, merge conflict, required manual job, rejected review, branch needs rebase/update, or a concrete blocker that can be fixed now.
- `WAIT`: CI is running/pending/queued, review or approval is required, merge-when-pipeline-succeeds/auto-merge is enabled, or there is no actionable failure yet. Arm the local watcher when an active Goal depends on later state.
- `DONE`: merged, closed intentionally, or all checks are green and no obvious remote blocker remains.

For `ACTION`, fetch only the failed job logs needed for the next fix. Summarize the failing lines; do not paste full logs unless the user asks.

For `WAIT`, do not run another agent snapshot. If the user requested merge, enable merge-when-pipeline-succeeds or auto-merge when policy and permissions allow, then arm the local watcher for notification.

For `DONE`, report the result and avoid extra remote calls. Exception: a GitHub `DONE` with reason `no checks reported` taken within ~60s of a push may be the check-registration race (Actions has not registered its check runs yet) — wait 60 seconds and take exactly one fresh snapshot before merging.

## Low-Token Merge Flow

When the user asks to submit, merge, or "merge when CI passes", do the remote workflow without chat polling:

1. Take one compact snapshot before acting.
2. If CI is failed or canceled, fetch only failed/canceled jobs and the shortest useful trace tail:

```bash
glab api "projects/<project_id>/pipelines/<pipeline_id>/jobs?per_page=100" \
  | jq -r '.[] | select(.status=="failed" or .status=="canceled") | [.id,.name,.stage,.status,.failure_reason,.web_url] | @tsv'

glab api "projects/<project_id>/jobs/<job_id>/trace" | tail -n 160
```

3. Fix locally, run the narrow gate that proves the fix, and run the formatter/linter gate that failed remotely.
4. If the MR should stay as one clean commit, amend the existing commit and push with `git push --force-with-lease` only to the branch you just amended.
5. After any push, take exactly one fresh snapshot because the branch SHA changed. Re-enable auto-merge/merge-when-pipeline-succeeds when policy and permissions allow, then arm one local watcher if the snapshot is `WAIT`.
6. If GitLab reports the MR as merged while the MR head pipeline is still pending/running, do not treat the merge as CI success. Report the split state from the snapshot instead of waiting: merged is done for Git state, CI is still pending for validation state.
7. After a successful merge, clean the local feature worktree and branch promptly: fast-forward the root `DEV` checkout, remove the feature worktree, delete the local branch, fetch/prune remotes, and run `git worktree prune`.
8. If the user asks to ensure the MR merges, enable server-side auto-merge for the current head SHA, run `scripts/ci_merge_delegate.py` once to confirm delegation or an immediate blocker, and arm the local watcher while the Goal remains unfinished.

Do not wait for a running pipeline in model turns. Server-side auto-merge preserves merge intent; the local watcher supplies the event that resumes the Goal.

## Delegation Snapshot

Use the bundled GitLab auto-merge delegation helper to confirm that merge is delegated to GitLab auto-merge or to surface an immediate terminal blocker:

```bash
python3 ~/.codex/skills/ci-status-snapshot/scripts/ci_merge_delegate.py \
  --provider gitlab \
  --selector <mr-iid-or-url> \
  --json
```

The helper must:

- Prefer non-blocking delegation after auto-merge is enabled: emit `delegated_auto_merge` and exit while CI is merely running/pending.
- Print only compact JSON/text results. Do not print progress updates or state-change streams.
- Exit after one snapshot unless it found an immediate terminal state.
- Fetch failed job details only after a terminal failed/canceled/manual pipeline state, or a skipped pipeline whose merge gate still requires a pipeline; a skipped pipeline on a mergeable MR is not a failure.
- Never be left running when sending the final answer.
- If the helper cannot run, use the compact `glab api ... | jq` one-shot snapshot from the Snapshot First section. Do not replace the helper with a hand-written polling loop.

If the failed job trace shows the runner failed before project scripts ran, classify it as CI infrastructure rather than a code defect. Common examples include Docker executor preparation timeouts, `Failed to remove network for build`, source checkout timeouts in `get_sources`, or service container startup timeouts before `script`. Retry only the failed job with the provider API at most once, then take one compact MR snapshot. If the replacement job is pending/running and auto-merge is enabled, report delegated/pending and stop. If the same infrastructure class repeats, stop and report the runner/checkout blocker instead of changing code.

Never auto-retry a failed `lint`/`test`/`gofmt`/typecheck job (any in-`script` quality gate). A gate that ran and reported `path/file.go:line:col: msg (linter)` findings or `--- FAIL:` is a code defect, not a transient — blind-retrying it just burns pipelines and hides the real failure. Read the trace and triage: reproduce locally with the SAME pinned tool version the CI uses (e.g. install `golangci-lint@${GOLANGCI_LINT_VERSION}` from the CI config and run the exact `golangci-lint run ...` command, or `go test` the failing package), fix the finding, and only then re-push. The one allowed auto-retry is reserved for the pre-`script` runner/infra classes above and for clearly tool-provisioning transients inside the job (the linter binary or `go mod download` failing to fetch over the network — i.e. a download/timeout error, not a `path:line:col` finding). A snapshot that finds failed lint/test stops and surfaces the finding; it does not retry or loop.

If the current snapshot or the user shows that a GitLab job is already `running` far beyond normal duration and its trace is empty, treat it as a possible stuck runner/job instead of waiting again. Take one diagnostic snapshot: current job status, duration, runner status, trace tail, and recent same-name job durations. If recent successful same-name jobs are much shorter and there is no script output or failed trace, cancel and retry only that job; do not rerun the whole pipeline or push a no-op commit.

```bash
job=<job_id>
glab api "projects/<project_id>/jobs/${job}" \
  | jq -r '[
      "status="+.status,
      "duration="+((.duration // 0)|floor|tostring),
      "runner="+(.runner.description // ""),
      "runner_status="+(.runner.status // ""),
      "web_url="+.web_url
    ] | .[]'
glab api "projects/<project_id>/jobs/${job}/trace" | tail -n 160
glab api "projects/<project_id>/jobs?per_page=100&scope[]=success" \
  | jq -r --arg name "<job_name>" '[.[] | select(.name==$name) | {id,duration:(.duration|floor),created_at,web_url}] | .[:10][] | [.id,.duration,.created_at,.web_url] | @tsv'
glab api -X POST "projects/<project_id>/jobs/${job}/cancel"
glab api -X POST "projects/<project_id>/jobs/${job}/retry" \
  | jq -r '["retry_id="+(.id|tostring),"retry_status="+.status,"retry_web_url="+.web_url] | .[]'
```

After a single-job retry, re-check the MR compact fields once. If auto-merge stayed enabled and the replacement job is pending/running, report delegated/pending and stop. If the replacement fails, fetch only that job's trace tail and triage normally.

## Token Rules

- Treat efficiency as token budget control: fewer CLI calls, narrower fields, shorter log tails, and fewer chat turns.
- Never loop on pending/running CI in model turns.
- Use the bundled silent watcher, not ad hoc loops, terminal streams, subagents, reminders, or scheduled assistant continuations.
- Keep the watcher attached so its single exit event becomes a tool notification and it cannot become an orphan process.
- Prefer provider-side auto-merge over local polling whenever possible.
- Never fetch logs for successful, pending, or running jobs; skipped jobs have no useful trace — fetch metadata only.
- If a pipeline reaches failed, canceled, or manual — or GitLab reports it skipped while the merge gate is not mergeable — stop status checks and switch to failed-job triage. GitHub check-level skipped/neutral conclusions are non-blocking and count as done.
- Prefer structured JSON fields over full command output.
- Prefer local validation and focused fixes over waiting for remote reruns.
- The agent re-checks remote state only after a code push, after enabling auto-merge, on a watcher notification, or when the user explicitly asks for a fresh snapshot.
- A fresh snapshot is one bounded read, not permission to keep watching.
- When the user says polling is wasting tokens, stop model-driven polling and move the wait into the silent local watcher.
- When many MRs are open, process one mergeable MR at a time in dependency order. Do not refresh every MR after every merge unless the base branch changed in a way that can affect them.
- Keep final reports compact: current conclusion, status evidence, and the next useful action.

## Response Shape

Use this shape for terse operator updates:

```text
Conclusion: WAIT (watcher armed; Goal active)
Current state: PR #123 is open; checks are running; review is still required.
Action: auto-merge is enabled; a local script is polling silently without model turns.
Next: the watcher will notify the agent once when state changes; no agent polling is scheduled.
```

Match the user's language in the final answer. For Chinese users, use `结论`, `当前状态`, `处理`, and `下一步`.
