# GitLab job triage

Read this when a GitLab snapshot is `ACTION` for a failed, canceled, or manual pipeline (or a skipped one whose merge gate requires CI), or when a running job looks stuck.
`$SKILL_DIR` is defined in `SKILL.md`; these recipes replace guesswork, not the snapshot helpers.

## Fetch only what triage needs

If CI is failed or canceled, fetch only failed/canceled jobs and the shortest useful trace tail:

```bash
glab api "projects/<project_id>/pipelines/<pipeline_id>/jobs?per_page=100" \
  | jq -r '.[] | select(.status=="failed" or .status=="canceled") | [.id,.name,.stage,.status,.failure_reason,.web_url] | @tsv'

glab api "projects/<project_id>/jobs/<job_id>/trace" | tail -n 160
```

## Infrastructure failure, not a code defect

If the failed job trace shows the runner failed before project scripts ran, classify it as CI infrastructure rather than a code defect. Common examples include Docker executor preparation timeouts, `Failed to remove network for build`, source checkout timeouts in `get_sources`, or service container startup timeouts before `script`. Retry only the failed job with the provider API at most once, then take one compact MR snapshot. If the replacement job is pending/running and auto-merge is enabled, report delegated/pending and stop. If the same infrastructure class repeats, stop and report the runner/checkout blocker instead of changing code.

## Never auto-retry a quality gate

Never auto-retry a failed `lint`/`test`/`gofmt`/typecheck job (any in-`script` quality gate). A gate that ran and reported `path/file.go:line:col: msg (linter)` findings or `--- FAIL:` is a code defect, not a transient — blind-retrying it just burns pipelines and hides the real failure. Read the trace and triage: reproduce locally with the SAME pinned tool version the CI uses (e.g. install `golangci-lint@${GOLANGCI_LINT_VERSION}` from the CI config and run the exact `golangci-lint run ...` command, or `go test` the failing package), fix the finding, and only then re-push. The one allowed auto-retry is reserved for the pre-`script` runner/infra classes above and for clearly tool-provisioning transients inside the job (the linter binary or `go mod download` failing to fetch over the network — i.e. a download/timeout error, not a `path:line:col` finding). A snapshot that finds failed lint/test stops and surfaces the finding; it does not retry or loop.

## Stuck runner or job

If the current snapshot or the user shows that a GitLab job is already `running` far beyond normal duration and its trace is empty, treat it as a possible stuck runner/job instead of waiting again. Take one diagnostic snapshot: current job status, duration, runner status, trace tail, and recent same-name job durations. If recent successful same-name jobs are much shorter and there is no script output or failed trace, cancel and retry only that job, at most once; do not rerun the whole pipeline or push a no-op commit. Then take one final compact snapshot and stop.

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
