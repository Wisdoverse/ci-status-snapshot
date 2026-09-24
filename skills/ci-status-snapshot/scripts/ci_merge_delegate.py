#!/usr/bin/env python3
"""GitLab MR auto-merge delegation status helper.

The script takes one authoritative MR snapshot and returns. If GitLab
server-side auto-merge is enabled and CI is still running, it reports
`delegated_auto_merge` instead of waiting. It intentionally has no blocking
mode: this skill exists to avoid local CI polling.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

# One decision, one set of state tables: this helper names its results from the
# same classifier the snapshot uses, so a provider enum change is a one-file fix.
from ci_status_snapshot import PENDING_STATES, SUCCESS_STATES, classify_gitlab


def run(cmd: list[str], timeout: int = 25) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_value(args: list[str]) -> str:
    code, out, _ = run(["git", *args], timeout=10)
    return out if code == 0 else ""


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_or_die(raw: str, source: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON from {source}: {exc}") from exc


def parse_gitlab_url(value: str) -> tuple[str | None, str | None]:
    parsed = urlparse(value)
    match = re.search(r"/-/merge_requests/(\d+)(?:/|$)", parsed.path)
    if not match:
        return None, None
    project_path = parsed.path[: match.start()].strip("/")
    return project_path or None, match.group(1)


def project_from_remote() -> str:
    remote = git_value(["config", "--get", "remote.origin.url"])
    if not remote:
        return ""

    if "://" in remote:
        parsed = urlparse(remote)
        path = parsed.path.strip("/")
    elif ":" in remote:
        path = remote.split(":", 1)[1].strip("/")
    else:
        path = remote.strip("/")

    if path.endswith(".git"):
        path = path[:-4]
    return path


def resolve_project_and_mr(project: str | None, selector: str) -> tuple[str, str]:
    url_project, url_mr = parse_gitlab_url(selector)
    mr = url_mr or selector
    if not re.fullmatch(r"\d+", mr):
        raise SystemExit("--selector must be a GitLab MR IID or MR URL")

    resolved_project = project or url_project or os.environ.get("CI_PROJECT_ID") or project_from_remote()
    if not resolved_project:
        raise SystemExit("Could not determine GitLab project; pass --project <id-or-path>")
    return resolved_project, mr


def project_api_id(project: str) -> str:
    if re.fullmatch(r"\d+", project):
        return project
    return quote(project, safe="")


def glab_api(path: str, timeout: int = 25) -> tuple[int, Any | None, str]:
    code, out, err = run(["glab", "api", path], timeout=timeout)
    if code != 0:
        return code, None, err or out
    return 0, load_json_or_die(out, f"glab api {path}"), ""


def failed_jobs(project: str, pipeline_id: Any) -> tuple[list[dict[str, Any]] | None, str]:
    """Return (jobs, error). Never (empty list, "") on failure.

    An API/parse error that returned [] would read as "the pipeline failed but no
    job failed", which sends the agent looking for a phantom infra problem. The
    caller emits failed_jobs_error instead so the two cases stay distinguishable.
    """
    if pipeline_id in (None, ""):
        return None, "merge request has no head pipeline id"
    try:
        code, data, err = glab_api(f"projects/{project_api_id(project)}/pipelines/{pipeline_id}/jobs?per_page=100")
    except SystemExit as exc:  # load_json_or_die: glab printed something that is not JSON
        return None, str(exc)
    if code != 0:
        return None, err or f"glab api exited {code}"
    if not isinstance(data, list):
        return None, "unexpected jobs payload: expected a JSON list"

    jobs: list[dict[str, Any]] = []
    for job in data:
        if not isinstance(job, dict):
            continue
        if norm(job.get("status")) not in {"failed", "canceled", "cancelled", "manual"}:
            continue
        jobs.append(
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "stage": job.get("stage"),
                "status": job.get("status"),
                "failure_reason": job.get("failure_reason"),
                "web_url": first(job, "web_url", "webUrl"),
            }
        )
    return jobs, ""


def base_result(result: str, project: str, mr: str, data: dict[str, Any]) -> dict[str, Any]:
    pipeline = first(data, "head_pipeline", "headPipeline", "pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    return {
        "result": result,
        "provider": "gitlab",
        "project": project,
        "mr": int(mr),
        "state": data.get("state"),
        "sha": data.get("sha"),
        "merge_status": first(data, "detailed_merge_status", "detailedMergeStatus", "merge_status", "mergeStatus"),
        "auto_merge": bool(first(data, "merge_when_pipeline_succeeds", "mergeWhenPipelineSucceeds")),
        "draft": bool(first(data, "draft", "work_in_progress", "workInProgress")),
        "pipeline_id": first(pipeline, "id", "iid"),
        "pipeline_status": first(pipeline, "status", "detailedStatus", "detailed_status") or "unknown",
        "web_url": first(data, "web_url", "webUrl", "url"),
        "snapshot_time_utc": utc_now(),
    }


def print_result(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True, sort_keys=True)
        print(f"{key}={value}")


def supervise_gitlab_mr(args: argparse.Namespace) -> int:
    project, mr = resolve_project_and_mr(args.project, args.selector)
    try:
        code, data, err = glab_api(f"projects/{project_api_id(project)}/merge_requests/{mr}")
    except SystemExit as exc:
        # glab can exit 0 and print a proxy/login page instead of JSON; that is an
        # api_error like any other, not a bare exit 1 outside the 0/2/3/4 contract
        code, data, err = 1, None, str(exc)
    if code != 0 or not isinstance(data, dict):
        print_result(
            {
                "result": "api_error",
                "provider": "gitlab",
                "project": project,
                "mr": int(mr),
                "last_error": err,
                "snapshot_time_utc": utc_now(),
            },
            args.json,
        )
        return 4

    result = base_result("", project, mr, data)
    # absence is a property of the pipeline object, never of its status string:
    # base_result displays "unknown" for both a missing pipeline and a pipeline
    # that reports a literal "unknown" status, and those must not classify alike.
    # Missing -> "" (no CI to judge); present -> the literal status, which
    # classify_gitlab fail-closes if it recognizes none of its tables.
    no_pipeline = result["pipeline_id"] in (None, "")
    pipeline_status = "" if no_pipeline else norm(result["pipeline_status"])
    _conclusion, cause, _reason = classify_gitlab(
        norm(data.get("state")),
        pipeline_status,
        norm(first(data, "detailed_merge_status", "detailedMergeStatus")),
        norm(first(data, "merge_status", "mergeStatus")),
        result["draft"],
        pipeline_observed=not no_pipeline,
        allow_no_pipeline=args.allow_no_pipeline,
    )

    if cause == "merged":
        result.update(
            {
                "result": "merged",
                "merged_at": data.get("merged_at"),
                "merge_commit": first(data, "merge_commit_sha", "mergeCommitSha"),
            }
        )
        print_result(result, args.json)
        return 0

    if cause == "closed":
        result["result"] = "closed_unmerged"
        print_result(result, args.json)
        return 3

    if cause == "pipeline":
        # terminal CI state (failed/canceled/manual, or skipped where the gate
        # requires a pipeline): hand over the jobs the agent has to triage
        result["result"] = f"pipeline_{pipeline_status}"
        jobs, jobs_error = failed_jobs(project, result.get("pipeline_id"))
        if jobs_error:
            result["failed_jobs_error"] = jobs_error
        else:
            result["failed_jobs"] = jobs
        print_result(result, args.json)
        return 2

    if cause in {"merge_gate", "human_gate"}:
        # conflict/rebase/requested changes, or an approval/discussion gate: the
        # snapshot may keep waiting for a human, this one-shot helper cannot
        result["result"] = "merge_blocked"
        print_result(result, args.json)
        return 3

    if cause == "mergeable":
        if result["auto_merge"]:
            result["result"] = "delegated_auto_merge"
            print_result(result, args.json)
            return 0
        if pipeline_status == "skipped":
            result["result"] = "pipeline_skipped_mergeable"
        elif pipeline_status in SUCCESS_STATES:
            result["result"] = "pipeline_success_unmerged"
        else:
            result["result"] = "mergeable_unmerged"
        print_result(result, args.json)
        return 3

    if cause == "draft":
        # the classifier ranks draft above a missing pipeline: a draft with no
        # pipeline is blocked by the draft, and "no pipeline observed" would hide it
        result["result"] = "waiting"
        print_result(result, args.json)
        return 0

    if cause == "no_pipeline_observed":
        # not the same as waiting for CI: an MR with no head pipeline may still
        # be registering one, or may never create one (rules, no .gitlab-ci.yml)
        result["result"] = "no_pipeline_observed"
        print_result(result, args.json)
        return 0

    # auto-merge is armed server-side, so anything that clears itself will merge
    # without us: a pipeline still running, or a gate still settling (checking,
    # approvals_syncing, merge_time, preparing, ...). Gate on the classified
    # cause, never on the raw pipeline status — a draft with a running pipeline
    # is cause "draft", and GitLab does not auto-merge drafts. Human gates are
    # excluded too: cause "human_gate" already returned merge_blocked above.
    if result["auto_merge"] and cause in {"pipeline_pending", "merge_gate_pending"}:
        result["result"] = "delegated_auto_merge"
    else:
        result["result"] = "waiting"
    print_result(result, args.json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take one GitLab MR auto-merge delegation snapshot.",
        epilog=(
            "Exit codes: 0 = nothing to do now (result is merged, delegated_auto_merge, "
            "waiting, or no_pipeline_observed), 2 = terminal pipeline state, triage the "
            "failed jobs, 3 = a human must act (closed_unmerged, merge_blocked, "
            "*_unmerged/mergeable), 4 = api_error. Exit 0 is NOT 'merged': always read "
            "the `result` field."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--provider", choices=["gitlab"], default="gitlab")
    parser.add_argument("--selector", required=True, help="GitLab MR IID or MR URL")
    parser.add_argument("--project", help="GitLab project id or path. Defaults to CI_PROJECT_ID or git remote path.")
    parser.add_argument("--json", action="store_true", help="Print one terminal JSON object.")
    parser.add_argument(
        "--allow-no-pipeline",
        action="store_true",
        help=(
            "This project runs no pipeline for the MR: a mergeable MR with no head pipeline reports "
            "mergeable_unmerged instead of no_pipeline_observed."
        ),
    )
    args = parser.parse_args()

    return supervise_gitlab_mr(args)


if __name__ == "__main__":
    sys.exit(main())
