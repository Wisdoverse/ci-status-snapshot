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


PIPELINE_FAIL_STATES = {"failed", "canceled", "cancelled", "skipped", "manual"}
PIPELINE_PENDING_STATES = {
    "created",
    "pending",
    "preparing",
    "running",
    "scheduled",
    "waiting",
    "waiting_for_resource",
}
PIPELINE_SUCCESS_STATES = {"success", "passed", "succeeded"}
MERGE_BLOCK_STATES = {
    "blocked_status",
    "cannot_be_merged",
    "conflict",
    "discussions_not_resolved",
    "need_rebase",
    "not_approved",
}


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


def failed_jobs(project: str, pipeline_id: Any) -> list[dict[str, Any]]:
    if pipeline_id in (None, ""):
        return []
    code, data, _ = glab_api(f"projects/{project_api_id(project)}/pipelines/{pipeline_id}/jobs?per_page=100")
    if code != 0 or not isinstance(data, list):
        return []

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
    return jobs


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
    code, data, err = glab_api(f"projects/{project_api_id(project)}/merge_requests/{mr}")
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
    state = norm(data.get("state"))
    pipeline_status = norm(result["pipeline_status"])
    merge_status = norm(result["merge_status"])

    if state == "merged":
        result.update(
            {
                "result": "merged",
                "merged_at": data.get("merged_at"),
                "merge_commit": first(data, "merge_commit_sha", "mergeCommitSha"),
            }
        )
        print_result(result, args.json)
        return 0

    if state == "closed":
        result["result"] = "closed_unmerged"
        print_result(result, args.json)
        return 3

    # skipped ([ci skip]/rules) does not block a mergeable gate; align with
    # ci_status_snapshot.py instead of triaging a pipeline with no failed jobs
    if pipeline_status == "skipped" and merge_status == "mergeable":
        if result["auto_merge"]:
            result["result"] = "delegated_auto_merge"
            print_result(result, args.json)
            return 0
        result["result"] = "pipeline_skipped_mergeable"
        print_result(result, args.json)
        return 3

    if pipeline_status in PIPELINE_FAIL_STATES:
        result["result"] = f"pipeline_{pipeline_status}"
        result["failed_jobs"] = failed_jobs(project, result.get("pipeline_id"))
        print_result(result, args.json)
        return 2

    if merge_status in MERGE_BLOCK_STATES:
        result["result"] = "merge_blocked"
        print_result(result, args.json)
        return 3

    if pipeline_status in PIPELINE_SUCCESS_STATES:
        if result["auto_merge"]:
            result["result"] = "delegated_auto_merge"
            print_result(result, args.json)
            return 0
        result["result"] = "pipeline_success_unmerged"
        print_result(result, args.json)
        return 3

    if result["auto_merge"] and pipeline_status in PIPELINE_PENDING_STATES:
        result["result"] = "delegated_auto_merge"
    else:
        result["result"] = "waiting"
    print_result(result, args.json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Take one GitLab MR auto-merge delegation snapshot.")
    parser.add_argument("--provider", choices=["gitlab"], default="gitlab")
    parser.add_argument("--selector", required=True, help="GitLab MR IID or MR URL")
    parser.add_argument("--project", help="GitLab project id or path. Defaults to CI_PROJECT_ID or git remote path.")
    parser.add_argument("--json", action="store_true", help="Print one terminal JSON object.")
    args = parser.parse_args()

    return supervise_gitlab_mr(args)


if __name__ == "__main__":
    sys.exit(main())
