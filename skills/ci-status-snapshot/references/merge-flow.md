# Merge and auto-merge delegation flow

Read this when the user asks to submit, merge, "merge when CI passes", or to hand the merge to GitLab auto-merge.
`$SKILL_DIR` is defined in `SKILL.md`; the snapshot/watcher/delegate commands and their exit codes stay there.

## Low-Token Merge Flow

When the user asks to submit, merge, or "merge when CI passes", do the remote workflow without chat polling:

1. Use one compact status/delegation snapshot before acting; reuse already-fresh fields instead of invoking both helpers on unchanged state.
2. If CI is failed or canceled, fetch only failed/canceled jobs and the shortest useful trace tail — the recipes are in `$SKILL_DIR/references/gitlab-triage.md`.
3. Fix locally, run the narrow gate that proves the fix, and run the formatter/linter gate that failed remotely.
4. If the MR should stay as one clean commit, amend the existing commit and push with `git push --force-with-lease` only to the branch you just amended.
5. After a push, verify the new head and enable auto-merge/merge-when-pipeline-succeeds when policy and permissions allow. Reuse the mutation response when sufficient; otherwise take one delegation snapshot. If it is `WAIT`/delegated, arm one watcher with `--expected-head <that full SHA>`. Do not follow it with another status helper.
6. If GitLab reports the MR as merged while the MR head pipeline is still pending/running, do not treat the merge as CI success. Report the split state from the snapshot instead of waiting: merged is done for Git state, CI is still pending for validation state.
7. When cleanup is authorized, verify the exact merged source head, integration ancestry (or reviewed squash equivalence), clean owned worktree and no open dependents. Then fast-forward the repository's integration checkout if safe and remove only the proved-owned feature worktree/branch. Preserve unrelated dirt and closed-unmerged work; do not assume every repository uses `DEV`.
8. If the user asks to ensure the MR merges, enable server-side auto-merge for the exact head. Reuse the returned state or run `$SKILL_DIR/scripts/ci_merge_delegate.py` once if needed to confirm delegation, then arm the local watcher while the Goal remains unfinished. Retain its target, head and attached tool handle for handoff.

Do not wait for a running pipeline in model turns. Server-side auto-merge preserves merge intent; the local watcher supplies the event that resumes the Goal.

## Delegation Snapshot

Run the bundled GitLab auto-merge delegation helper (command in `SKILL.md`, Snapshot First) to confirm that merge is delegated to GitLab auto-merge or to surface an immediate terminal blocker.

Result per exit code: `0` = nothing to do now (`result` is `merged`, `delegated_auto_merge`, `waiting`, or `no_pipeline_observed`), `2` = terminal pipeline state, triage the failed jobs, `3` = a human must act (`closed_unmerged`, `merge_blocked`, `pipeline_success_unmerged`, `pipeline_skipped_mergeable`, `mergeable_unmerged`), `4` = `api_error`. Exit `0` does not mean merged — always read the `result` field. A `failed_jobs_error` field means the job list could not be fetched; it is not the same as zero failed jobs.

The helper must:

- Prefer non-blocking delegation after auto-merge is enabled: emit `delegated_auto_merge` and exit while CI is merely running/pending.
- Print only compact JSON/text results. Do not print progress updates or state-change streams.
- Exit after one snapshot unless it found an immediate terminal state.
- Fetch failed job details only after a terminal failed/canceled/manual pipeline state, or a skipped pipeline whose merge gate is `ci_must_pass` (or a legacy server that reports no `detailed_merge_status` at all); a skipped pipeline under any other gate is not a failure.
- Never be left running when sending the final answer.
- If the helper cannot run, use the compact `glab api ... | jq` one-shot snapshot below. Do not replace the helper with a hand-written polling loop.

## GitLab caveats

- `ci_merge_delegate.py` runs `glab api` against the host `glab` infers from the repo remote and never passes `--hostname`. An MR URL selector for a different host is not routed there, and `--project` does not fix it — it only changes the project path, so the same IID can be read off the wrong instance (or 404). Run the helper from a checkout of that host's project. `--project` is for project inference within the right host.
- Legacy GitLab (before 15.6, no `detailed_merge_status`) never reports `DONE` from a green pipeline alone — the deprecated `merge_status` only proves the branches merge cleanly. Take a fresh snapshot after the merge, or upgrade the server.

## Compact MR field snapshot

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
