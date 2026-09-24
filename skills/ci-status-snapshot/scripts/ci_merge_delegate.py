#!/usr/bin/env python3
"""GitLab MR auto-merge delegation status helper.

The script takes one authoritative MR snapshot and returns. If GitLab
server-side auto-merge is enabled and CI is still running, it reports
`delegated_auto_merge` instead of waiting. It intentionally has no blocking
mode: this skill exists to avoid local CI polling.

With --enable it also turns auto-merge on, guarded: one PUT at most, only for
the MR's current head while that head's pipeline is running and postdates the
newest target-branch change, then one re-read decides the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlparse

# One decision, one set of state tables: this helper names its results from the
# same classifier the snapshot uses, so a provider enum change is a one-file fix.
from ci_status_snapshot import PENDING_STATES, SUCCESS_STATES, classify_gitlab, gitlab_auto_merge_enabled


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


def glab_api(path: str, timeout: int = 25, extra: Sequence[str] = ()) -> tuple[int, Any | None, str]:
    code, out, err = run(["glab", "api", path, *extra], timeout=timeout)
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


def attach_failed_jobs(result: dict[str, Any], project: str) -> None:
    jobs, jobs_error = failed_jobs(project, result.get("pipeline_id"))
    if jobs_error:
        result["failed_jobs_error"] = jobs_error
    else:
        result["failed_jobs"] = jobs


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
        "auto_merge": gitlab_auto_merge_enabled(data),
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
        attach_failed_jobs(result, project)
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


# --- --enable -----------------------------------------------------------------

NOTES_PAGE_SIZE = 100
# Two system-note forms move an MR to another target: a manual retarget, and
# the automatic one when the old target branch is deleted ("deleted the `x`
# branch. This merge request now targets the `main` branch"), for which some
# self-hosted GitLab versions write no "changed target branch" note at all.
RETARGET_NOTE_PREFIX = "changed target branch from"
RETARGET_NOTE_AUTO = "this merge request now targets the"
# GitLab servers that do not recognise a parameter ignore it, and a bare PUT
# /merge merges at once. Send both names: merge_when_pipeline_succeeds (the
# only one some self-hosted GitLab versions report back) and auto_merge.
# --field sends typed booleans; --method PUT stops glab defaulting to POST.
ENABLE_FIELDS = ("--field", "merge_when_pipeline_succeeds=true", "--field", "auto_merge=true")


def api_json(path: str, *extra: str) -> tuple[Any, str]:
    """(payload, "") or (None, error). A non-JSON body is an error like any other."""
    try:
        code, data, err = glab_api(path, extra=extra)
    except SystemExit as exc:
        return None, str(exc)
    if code != 0:
        return None, err or f"glab api exited {code}"
    return data, ""




def head_pipeline(data: dict[str, Any]) -> dict[str, Any]:
    pipeline = first(data, "head_pipeline", "headPipeline", "pipeline") or {}
    return pipeline if isinstance(pipeline, dict) else {}


def text(value: Any) -> str:
    return str(value or "").strip()


def parse_gitlab_time(value: Any) -> datetime | None:
    """A timezone-aware timestamp, or None. Python 3.9 rejects a trailing Z."""
    raw = value.strip() if isinstance(value, str) else ""
    if raw[-1:] in {"Z", "z"}:
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def retarget_refusal(notes: Any, pipeline_created_at: Any) -> str:
    """"" when the head pipeline postdates the newest retarget, else a reason.

    A pipeline created before a retarget ran against the old target: letting it
    carry the merge is how an MR merges into a branch its CI never saw.
    """
    if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
        return "retarget_history_incomplete"
    for note in notes:  # newest first: requested with order_by=created_at&sort=desc
        body = norm(note.get("body"))
        if note.get("system") is True and (body.startswith(RETARGET_NOTE_PREFIX) or RETARGET_NOTE_AUTO in body):
            retargeted = parse_gitlab_time(note.get("created_at"))
            created = parse_gitlab_time(pipeline_created_at)
            if retargeted is None or created is None:
                return "retarget_time_unknown"
            return "" if created > retargeted else "pipeline_before_retarget"
    # no retarget on this page: only a short page proves there is no older one
    return "" if len(notes) < NOTES_PAGE_SIZE else "retarget_history_incomplete"


def eligibility_refusal(data: dict[str, Any]) -> str:
    """The first failed --enable precondition readable from the MR itself, or ""."""
    pipeline = head_pipeline(data)
    head = text(data.get("sha"))
    if norm(data.get("state")) != "opened":
        return "state_not_open"
    if bool(first(data, "draft", "work_in_progress", "workInProgress")):
        return "draft"
    if not head:
        return "head_sha_missing"
    if first(pipeline, "id", "iid") is None:
        return "no_pipeline_observed"
    if not (text(pipeline.get("sha")) and first(pipeline, "status") and pipeline.get("created_at")):
        return "pipeline_metadata_missing"
    if text(pipeline.get("sha")) != head:
        return "pipeline_head_mismatch"
    if norm(pipeline.get("status")) != "running":
        return "pipeline_not_running"
    # the gate as the shared classifier reads it once CI is green: the running
    # pipeline is required separately above, and classified as running it would
    # mask an unrecognized gate as pipeline_pending
    _conclusion, gate, _reason = classify_gitlab(
        "opened",
        "success",
        norm(first(data, "detailed_merge_status", "detailedMergeStatus")),
        norm(first(data, "merge_status", "mergeStatus")),
        False,
        pipeline_observed=True,
    )
    if gate in {"merge_gate", "human_gate"}:
        return "merge_gate_blocked"
    if gate not in {"mergeable", "merge_gate_pending"}:
        return "merge_gate_unknown"
    return ""


def confirmed_outcome(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, int, str]:
    """(result, exit code, reason) from the single post-PUT read."""
    state = norm(after.get("state"))
    head_changed = text(after.get("sha")) != text(before.get("sha"))
    target_changed = first(after, "target_branch", "targetBranch") != first(before, "target_branch", "targetBranch")
    if state == "merged":
        # merged is Git state, not validation: only the head and target that
        # were checked, merged after the same pipeline we saw running turned
        # green, make this a clean merge
        if head_changed:
            return "merged_before_ci", 3, "head_changed"
        if target_changed:
            return "merged_before_ci", 3, "target_changed"
        pipeline = head_pipeline(after)
        same = first(pipeline, "id", "iid") == first(head_pipeline(before), "id", "iid")
        if same and norm(pipeline.get("status")) in SUCCESS_STATES:
            return "merged", 0, ""
        return "merged_before_ci", 3, ""
    if state != "opened":
        return "enable_not_confirmed", 3, "state_changed"
    if head_changed:
        return "enable_not_confirmed", 3, "head_changed"
    if target_changed:
        return "enable_not_confirmed", 3, "target_changed"
    if gitlab_auto_merge_enabled(after):
        return "delegated_auto_merge", 0, ""
    return "enable_not_confirmed", 3, "auto_merge_not_enabled"


def enable_gitlab_auto_merge(args: argparse.Namespace) -> int:
    project, mr = resolve_project_and_mr(args.project, args.selector)
    mr_path = f"projects/{project_api_id(project)}/merge_requests/{mr}"

    def emit(result: str, data: dict[str, Any], code: int, **extra: Any) -> int:
        payload = base_result(result, project, mr, data)
        payload["auto_merge"] = gitlab_auto_merge_enabled(data)
        payload["target_branch"] = first(data, "target_branch", "targetBranch")
        if norm(data.get("state")) == "merged":
            payload["merged_at"] = data.get("merged_at")
            payload["merge_commit"] = first(data, "merge_commit_sha", "mergeCommitSha")
        payload.update(extra)
        if result.startswith("pipeline_"):
            attach_failed_jobs(payload, project)
        print_result(payload, args.json)
        return code

    def api_error(error: str, **extra: Any) -> int:
        print_result(
            {
                "result": "api_error",
                "provider": "gitlab",
                "project": project,
                "mr": int(mr),
                "last_error": error,
                **extra,
                "snapshot_time_utc": utc_now(),
            },
            args.json,
        )
        return 4

    before, error = api_json(mr_path)
    if error or not isinstance(before, dict):
        return api_error(error or "unexpected merge request payload: expected a JSON object", put_attempted=False)

    pipeline = head_pipeline(before)
    pipeline_status = norm(first(pipeline, "status", "detailedStatus", "detailed_status"))
    if norm(before.get("state")) == "opened":
        _conclusion, cause, _reason = classify_gitlab(
            "opened",
            pipeline_status,
            norm(first(before, "detailed_merge_status", "detailedMergeStatus")),
            norm(first(before, "merge_status", "mergeStatus")),
            bool(first(before, "draft", "work_in_progress", "workInProgress")),
            pipeline_observed=first(pipeline, "id", "iid") is not None,
        )
        if cause == "pipeline":
            # terminal CI is triage, exactly as without --enable
            return emit(f"pipeline_{pipeline_status}", before, 2, put_attempted=False)

    refusal = eligibility_refusal(before)
    if refusal:
        return emit("enable_refused", before, 3, reason=refusal, put_attempted=False)

    notes, error = api_json(
        f"{mr_path}/notes?order_by=created_at&sort=desc&per_page={NOTES_PAGE_SIZE}&page=1"
    )
    if error:
        return api_error(error, put_attempted=False)
    refusal = retarget_refusal(notes, pipeline.get("created_at"))
    if refusal:
        return emit("enable_refused", before, 3, reason=refusal, put_attempted=False)

    if gitlab_auto_merge_enabled(before):
        return emit("delegated_auto_merge", before, 0, put_attempted=False)

    # exactly one PUT, never retried: the sha guard makes GitLab refuse (409)
    # if the source head moved since the read above
    _response, put_error = api_json(
        f"{mr_path}/merge", "--method", "PUT", *ENABLE_FIELDS, "--raw-field", f"sha={text(before.get('sha'))}"
    )
    extra: dict[str, Any] = {"put_attempted": True}
    if put_error:
        extra["put_error"] = put_error

    after, error = api_json(mr_path)
    if error or not isinstance(after, dict):
        return api_error(error or "unexpected merge request payload: expected a JSON object", **extra)
    result, code, reason = confirmed_outcome(before, after)
    if reason:
        extra["reason"] = reason
    if put_error and reason == "auto_merge_not_enabled":
        # nothing changed and the PUT itself failed: the API error is the result
        return emit("api_error", after, 4, last_error=put_error, **extra)
    return emit(result, after, code, **extra)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take one GitLab MR auto-merge delegation snapshot.",
        epilog=(
            "Exit codes: 0 = nothing to do now (result is merged, delegated_auto_merge, "
            "waiting, or no_pipeline_observed), 2 = terminal pipeline state (pipeline_<status>), "
            "triage the failed jobs, 3 = a human must act (closed_unmerged, merge_blocked, "
            "*_unmerged/mergeable; with --enable also enable_refused, enable_not_confirmed "
            "and merged_before_ci, each naming its `reason` where it has one), 4 = api_error. "
            "Exit 0 is NOT 'merged': always read the `result` field."
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
            "mergeable_unmerged instead of no_pipeline_observed. Ignored by --enable, which always "
            "requires a running head pipeline."
        ),
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help=(
            "Turn on GitLab auto-merge for the MR's current head: only while that head's pipeline "
            "is running and was created after the newest target-branch change. One PUT at most, "
            "never retried, then one re-read decides the result."
        ),
    )
    args = parser.parse_args()

    return enable_gitlab_auto_merge(args) if args.enable else supervise_gitlab_mr(args)


if __name__ == "__main__":
    sys.exit(main())
