#!/usr/bin/env python3
"""Compact one-shot GitHub/GitLab CI and PR/MR status snapshot."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


# Provider enums verified 2026-08 against docs.github.com/en/graphql/reference/enums
# (CheckStatusState, CheckConclusionState, StatusState, MergeStateStatus) and
# docs.gitlab.com/api/pipelines + /api/merge_requests/#merge-status. Values that
# older self-hosted GitLab versions still return are kept next to their
# replacements: the wire format is whatever the server runs, not what the docs say.
FAIL_STATES = {
    "action_required",
    "canceled",
    "cancelled",
    "error",
    "failed",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}
PENDING_STATES = {
    "canceling",
    "created",
    "expected",
    "in_progress",
    # "manual" stays here for GitHub check statuses; GitLab pipeline-level
    # manual is classified ACTION explicitly before this set is consulted
    "manual",
    "pending",
    "preparing",
    "queued",
    "requested",
    "running",
    "scheduled",
    "waiting",
    "waiting_for_callback",
    "waiting_for_resource",
}
# "completed" is deliberately NOT here: a GitHub check run reports status
# COMPLETED with a null conclusion during the conclusion write-back window, and
# an absent conclusion is not evidence of success.
SUCCESS_STATES = {"mergeable", "passed", "success", "succeeded"}

# GitHub mergeStateStatus. CLEAN ("mergeable and passing commit status") and
# HAS_HOOKS ("mergeable with passing commit status and pre-receive hooks") are
# the only two positive states; everything else is either fixable now or still
# moving, and an unrecognized value must never read as success.
GH_MERGE_ACTION = {"behind", "dirty"}
GH_MERGE_OK = {"clean", "has_hooks"}
# UNSTABLE is "mergeable with non-passing commit status" — a real check failure
# already returns ACTION from the failed-check branch above it, so what is left
# here is a flaky/required-but-unfinished context: wait, do not merge.
GH_MERGE_WAIT = {"blocked", "draft", "unknown", "unstable"}

# GitLab detailed_merge_status. Concrete blockers: these never clear by waiting,
# someone has to change the branch, the title, the locks, or the policy result.
GL_GATE_ACTION = {
    "broken_status",  # old name of "conflict", still emitted up to GitLab 16.6
    "commits_status",
    "conflict",
    "jira_association_missing",
    "locked_lfs_files",
    "locked_paths",
    "need_rebase",
    "policies_denied",  # old name of "security_policy_violations", up to 16.6
    "requested_changes",
    "security_policy_violations",
    "title_regex",
}
# Needs a person, but nothing about it is fixable by the agent. The snapshot
# waits (an approval can arrive without a push, so this is a review-pending
# state, not a failure); the one-shot merge delegate treats the same set as
# terminal because it has no one to wait for.
GL_GATE_HUMAN = {
    "blocked_status",  # old name of "merge_request_blocked", emitted up to 17.0
    "discussions_not_resolved",
    "merge_request_blocked",
    "not_approved",
}
# Remote is still computing, or the gate clears itself without local work.
GL_GATE_WAIT = {
    "approvals_syncing",
    "checking",
    "ci_must_pass",
    "ci_still_running",
    "draft_status",
    "external_status_checks",  # old name of "status_checks_must_pass", up to 17.0
    "merge_time",
    "not_open",  # merged/closed is decided from `state` before this is read
    "preparing",
    "security_policy_pipeline_check",  # the policy pipeline is still evaluating
    "status_checks_must_pass",
    "unchecked",
}
GL_LEGACY_MERGE_ACTION = {"cannot_be_merged", "cannot be merged"}


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


def detect_provider() -> str:
    remote = git_value(["config", "--get", "remote.origin.url"]).lower()
    if "github" in remote:
        return "github"
    if "gitlab" in remote:
        return "gitlab"
    return "unknown"


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def load_json_or_die(raw: str, source: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON from {source}: {exc}") from exc


def summarize_github_check(item: dict[str, Any]) -> dict[str, str]:
    name = first(item, "name", "context", "workflowName") or "unnamed-check"
    status = norm(first(item, "status", "state"))
    conclusion = norm(first(item, "conclusion"))
    effective = conclusion or status
    return {"name": str(name), "status": status, "conclusion": conclusion, "effective": effective}


def github_snapshot(selector: str | None) -> dict[str, Any]:
    fields = ",".join(
        [
            "number",
            "title",
            "state",
            "url",
            "headRefName",
            "headRefOid",
            "baseRefName",
            "isDraft",
            "reviewDecision",
            "mergeStateStatus",
            "autoMergeRequest",
            "statusCheckRollup",
        ]
    )
    cmd = ["gh", "pr", "view"]
    if selector:
        cmd.append(selector)
    cmd.extend(["--json", fields])
    code, out, err = run(cmd)
    if code != 0:
        raise SystemExit(err or "gh pr view failed")

    data = load_json_or_die(out, "gh pr view")
    checks = [summarize_github_check(item) for item in data.get("statusCheckRollup") or [] if isinstance(item, dict)]
    failed = [c for c in checks if c["effective"] in FAIL_STATES]
    # COMPLETED with no conclusion yet: GitHub is still writing the conclusion
    # back, so the run counts as pending and gets its own reason instead of
    # silently landing in the "not success, not pending" hole
    settling = [c for c in checks if c["status"] == "completed" and not c["conclusion"]]
    pending = [c for c in checks if c["effective"] in PENDING_STATES or c["status"] in PENDING_STATES] + settling
    # skipped/neutral GitHub check conclusions are non-blocking, count as done
    success = [c for c in checks if c["effective"] in SUCCESS_STATES or c["conclusion"] in {"success", "skipped", "neutral"}]

    blockers: list[str] = []
    state = norm(data.get("state"))
    review = norm(data.get("reviewDecision"))
    merge_state = norm(data.get("mergeStateStatus"))
    # most actionable first: reason/report use blockers[0]
    if failed:
        blockers.append(f"{len(failed)} failed check(s)")
    if merge_state in GH_MERGE_ACTION:
        blockers.append(f"merge state: {merge_state}")
    if review == "changes_requested":
        blockers.append("changes requested")
    elif review == "review_required":
        blockers.append("review required")
    if settling:
        blockers.append(f"{len(settling)} check(s) completed without a conclusion")
    if pending:
        blockers.append(f"{len(pending)} pending check(s)")
    if merge_state == "unknown":
        blockers.append("merge state still computing")
    elif merge_state in GH_MERGE_WAIT:
        blockers.append(f"merge state: {merge_state}")
    if data.get("isDraft"):
        blockers.append("draft")

    if state == "merged":
        conclusion, reason = "DONE", "PR is merged"
    elif state == "closed":
        conclusion, reason = "DONE", "PR is closed"
    elif failed or review == "changes_requested" or merge_state in GH_MERGE_ACTION:
        conclusion, reason = "ACTION", blockers[0]
    elif pending or data.get("isDraft") or review == "review_required" or merge_state in GH_MERGE_WAIT:
        conclusion, reason = "WAIT", blockers[0] if blockers else "remote gate is still pending"
    elif merge_state not in GH_MERGE_OK:
        # DONE needs positive merge evidence from GitHub itself: green checks
        # with an absent/unrecognized mergeStateStatus prove nothing about
        # branch protection, required reviews, or rulesets
        conclusion, reason = "WAIT", f"unrecognized merge state: {merge_state}" if merge_state else "no actionable failure in one-shot snapshot"
    elif checks and len(success) == len(checks):
        conclusion, reason = "DONE", "checks are green"
    elif not checks:
        # ambiguous right after a push: checks may still be registering
        conclusion, reason = "DONE", f"no checks reported; merge state is {merge_state} (checks may still be registering if just pushed)"
    else:
        conclusion, reason = "WAIT", "no actionable failure in one-shot snapshot"

    return {
        "provider": "github",
        "conclusion": conclusion,
        "reason": reason,
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "url": data.get("url"),
        "source": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "target": data.get("baseRefName"),
        "review": data.get("reviewDecision"),
        "merge_state": data.get("mergeStateStatus"),
        "draft": bool(data.get("isDraft")),
        "auto_merge": bool(data.get("autoMergeRequest")),
        "ci": {"total": len(checks), "failed": len(failed), "pending": len(pending), "success": len(success)},
        "failed_checks": [c["name"] for c in failed[:5]],
        "pending_checks": [c["name"] for c in pending[:5]],
        "blockers": blockers,
    }


def classify_gitlab(
    state: str,
    pipeline_status: str,
    detailed_merge: str,
    merge_status: str,
    draft: bool,
) -> tuple[str, str, str]:
    """The one GitLab MR decision, shared with ci_merge_delegate.py.

    Returns (conclusion, cause, reason). `cause` is a stable machine token so the
    merge delegate can name its result from this decision instead of keeping a
    second copy of the state tables that drifts away from this one. All inputs
    are already normalized (lowercase, "" when absent).
    """
    # manual pipelines are terminal without a human. skipped ([ci skip], rules
    # filtering) only blocks when the merge gate actually requires a pipeline
    # ("ci_must_pass") or on legacy servers with no detailed_merge_status at all
    # ("", fail-safe); a skipped pipeline on an otherwise mergeable MR is normal.
    actionable_pipeline = (
        pipeline_status in FAIL_STATES
        or pipeline_status == "manual"
        or (pipeline_status == "skipped" and detailed_merge in {"", "ci_must_pass"})
    )
    if state == "merged":
        return "DONE", "merged", "MR is merged"
    if state == "closed":
        return "DONE", "closed", "MR is closed"
    if actionable_pipeline:
        return "ACTION", "pipeline", f"pipeline {pipeline_status}"
    if detailed_merge in GL_GATE_ACTION or merge_status in GL_LEGACY_MERGE_ACTION:
        return "ACTION", "merge_gate", f"merge state: {detailed_merge or merge_status}"
    if draft:
        # a draft never merges, whatever the gate says: GitLab can still report
        # mergeable from a stale mergeability recompute (right after a title
        # change, say), so draft outranks every DONE path — but not a red
        # pipeline, which stays actionable on a draft like anywhere else
        return "WAIT", "draft", "draft"
    if detailed_merge in GL_GATE_HUMAN:
        # above the unknown-status fallback: an approval/discussion gate is
        # pipeline-independent evidence, and hiding it behind "we cannot read the
        # pipeline" would lose a real blocker
        return "WAIT", "human_gate", f"merge gate: {detailed_merge}"
    if detailed_merge == "mergeable" and pipeline_status in PENDING_STATES:
        # where CI is not a required merge check, GitLab reports mergeable while
        # the pipeline is still running: the merge gate is open but validation is
        # not finished, and "CI running" is never DONE
        return "WAIT", "pipeline_pending", f"pipeline {pipeline_status} (merge gate already mergeable)"
    if pipeline_status and pipeline_status not in SUCCESS_STATES and pipeline_status not in PENDING_STATES and pipeline_status != "skipped":
        # fail closed: a status in none of the tables (a value GitLab adds later,
        # or a literal "unknown") is not evidence that CI passed. Unknown blocks
        # OPTIMISTIC outcomes, never negative ones — a mergeable gate may not
        # promote it to DONE and a settling gate may not let the delegate call it
        # delegated, but a known human gate above still reports its own blocker.
        return "WAIT", "unknown", f"unrecognized pipeline status: {pipeline_status}"
    if detailed_merge == "mergeable":
        # authoritative once CI is neither running nor unrecognized: GitLab
        # itself says this MR can merge right now, whether the pipeline was
        # green, skipped, or never created. A green pipeline on its own never
        # reaches this line.
        return "DONE", "mergeable", "GitLab reports the MR mergeable"
    if detailed_merge in GL_GATE_WAIT:
        return "WAIT", "merge_gate_pending", f"merge gate: {detailed_merge}"
    if pipeline_status in PENDING_STATES:
        return "WAIT", "pipeline_pending", f"pipeline {pipeline_status}"
    if detailed_merge:
        return "WAIT", "unknown", f"unrecognized merge gate: {detailed_merge}"
    # Legacy servers (<15.6) have no detailed_merge_status: can_be_merged only
    # says the branches merge cleanly, not that approvals/CI/policies passed, so
    # a green pipeline plus can_be_merged is still not the positive evidence
    # DONE requires.
    return "WAIT", "unknown", "no actionable failure in one-shot snapshot"


def gitlab_snapshot(selector: str | None) -> dict[str, Any]:
    numeric_iid = bool(selector) and selector.isascii() and selector.isdecimal()
    if numeric_iid:
        cmd = ["glab", "api", f"projects/:fullpath/merge_requests/{selector}"]
        source = f"glab api projects/:fullpath/merge_requests/{selector}"
    else:
        cmd = ["glab", "mr", "view"]
        if selector:
            cmd.append(selector)
        cmd.extend(["--output", "json"])
        source = "glab mr view"
    code, out, err = run(cmd)
    if code != 0:
        raise SystemExit(err or f"{source} failed")

    data = load_json_or_die(out, source)
    pipeline = first(data, "head_pipeline", "headPipeline", "pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}

    state = norm(data.get("state"))
    pipeline_status = norm(first(pipeline, "status", "detailedStatus", "detailed_status"))
    detailed_merge = norm(first(data, "detailed_merge_status", "detailedMergeStatus"))
    merge_status = norm(first(data, "merge_status", "mergeStatus"))
    draft = bool(first(data, "draft", "work_in_progress", "workInProgress"))

    conclusion, cause, reason = classify_gitlab(state, pipeline_status, detailed_merge, merge_status, draft)

    # evidence list for the human-readable report; the decision above owns the
    # conclusion, so the pipeline is only listed when the decision actually
    # blamed it — a skipped pipeline on a mergeable MR is a DONE with no blocker,
    # not "pipeline skipped". The raw status stays visible under "ci".
    blockers: list[str] = []
    if cause in {"pipeline", "pipeline_pending"}:
        blockers.append(f"pipeline {pipeline_status}")
    if conclusion != "DONE":
        # a DONE MR has no blockers by definition: merged/closed MRs still report
        # detailed_merge_status "not_open" and can carry a stale draft flag, and
        # listing either would contradict the conclusion
        if detailed_merge in GL_GATE_ACTION or merge_status in GL_LEGACY_MERGE_ACTION:
            blockers.append(f"merge state: {detailed_merge or merge_status}")
        elif detailed_merge and detailed_merge != "mergeable":
            blockers.append(f"merge gate: {detailed_merge}")
        if draft:
            blockers.append("draft")

    return {
        "provider": "gitlab",
        "conclusion": conclusion,
        "reason": reason,
        "number": first(data, "iid", "id"),
        "title": data.get("title"),
        "state": data.get("state"),
        "url": first(data, "web_url", "webUrl", "url"),
        "source": first(data, "source_branch", "sourceBranch"),
        "head_sha": data.get("sha"),
        "target": first(data, "target_branch", "targetBranch"),
        "merge_state": detailed_merge or merge_status,
        "draft": draft,
        "auto_merge": bool(first(data, "merge_when_pipeline_succeeds", "mergeWhenPipelineSucceeds", "auto_merge_enabled")),
        "ci": {
            "pipeline_id": first(pipeline, "id", "iid"),
            "status": pipeline_status or "unknown",
            "url": first(pipeline, "web_url", "webUrl"),
        },
        "blockers": blockers,
    }


def render_text(snapshot: dict[str, Any]) -> str:
    lines = [
        f"Conclusion: {snapshot['conclusion']}",
        f"Reason: {snapshot['reason']}",
        f"Provider: {snapshot['provider']}",
    ]
    number = snapshot.get("number")
    title = snapshot.get("title")
    if number or title:
        lines.append(f"Target: {('#' + str(number)) if number else ''} {title or ''}".strip())
    if snapshot.get("url"):
        lines.append(f"URL: {snapshot['url']}")
    lines.append(f"State: {snapshot.get('state') or 'unknown'}")
    if snapshot.get("source") or snapshot.get("target"):
        lines.append(f"Branch: {snapshot.get('source') or '?'} -> {snapshot.get('target') or '?'}")
    lines.append(f"Merge gate: {snapshot.get('merge_state') or 'unknown'}")
    ci = snapshot.get("ci") or {}
    lines.append(f"CI: {json.dumps(ci, ensure_ascii=True, sort_keys=True)}")
    blockers = snapshot.get("blockers") or []
    lines.append(f"Blockers: {', '.join(blockers) if blockers else 'none detected'}")
    if snapshot["conclusion"] == "WAIT":
        lines.append("Next: use the attached local watcher if an active goal depends on this WAIT state.")
    lines.append(f"Snapshot: {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Take one compact CI/PR/MR status snapshot.")
    parser.add_argument("--provider", choices=["auto", "github", "gitlab"], default="auto")
    parser.add_argument("--selector", help="PR/MR number, URL, or branch selector")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args()

    provider = args.provider if args.provider != "auto" else detect_provider()
    if provider == "unknown":
        raise SystemExit("Could not detect provider from git remote; pass --provider github or --provider gitlab")

    snapshot = github_snapshot(args.selector) if provider == "github" else gitlab_snapshot(args.selector)
    snapshot["snapshot_time_utc"] = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_text(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
