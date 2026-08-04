#!/usr/bin/env python3
"""Compact one-shot GitHub/GitLab CI and PR/MR status snapshot."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


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
    "waiting_for_resource",
}
SUCCESS_STATES = {"completed", "mergeable", "passed", "success", "succeeded"}


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
    pending = [c for c in checks if c["effective"] in PENDING_STATES or c["status"] in PENDING_STATES]
    # skipped/neutral GitHub check conclusions are non-blocking, count as done
    success = [c for c in checks if c["effective"] in SUCCESS_STATES or c["conclusion"] in {"success", "skipped", "neutral"}]

    blockers: list[str] = []
    state = norm(data.get("state"))
    review = norm(data.get("reviewDecision"))
    merge_state = norm(data.get("mergeStateStatus"))
    # most actionable first: reason/report use blockers[0]
    if failed:
        blockers.append(f"{len(failed)} failed check(s)")
    if merge_state in {"dirty", "behind"}:
        blockers.append(f"merge state: {merge_state}")
    if review == "changes_requested":
        blockers.append("changes requested")
    elif review == "review_required":
        blockers.append("review required")
    if pending:
        blockers.append(f"{len(pending)} pending check(s)")
    if data.get("isDraft"):
        blockers.append("draft")

    if state == "merged":
        conclusion, reason = "DONE", "PR is merged"
    elif state == "closed":
        conclusion, reason = "DONE", "PR is closed"
    elif failed or review == "changes_requested" or merge_state in {"dirty", "behind"}:
        conclusion, reason = "ACTION", blockers[0]
    elif pending or data.get("isDraft") or review == "review_required" or merge_state in {"blocked", "has_hooks", "unstable"}:
        conclusion, reason = "WAIT", blockers[0] if blockers else "remote gate is still pending"
    elif checks and len(success) == len(checks):
        conclusion, reason = "DONE", "checks are green"
    elif not checks and merge_state == "clean":
        # ambiguous right after a push: checks may still be registering
        conclusion, reason = "DONE", "no checks reported; merge state is clean (checks may still be registering if just pushed)"
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


def gitlab_snapshot(selector: str | None) -> dict[str, Any]:
    cmd = ["glab", "mr", "view"]
    if selector:
        cmd.append(selector)
    cmd.extend(["--output", "json"])
    code, out, err = run(cmd)
    if code != 0:
        raise SystemExit(err or "glab mr view failed")

    data = load_json_or_die(out, "glab mr view")
    pipeline = first(data, "head_pipeline", "headPipeline", "pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}

    state = norm(data.get("state"))
    pipeline_status = norm(first(pipeline, "status", "detailedStatus", "detailed_status"))
    detailed_merge = norm(first(data, "detailed_merge_status", "detailedMergeStatus"))
    merge_status = norm(first(data, "merge_status", "mergeStatus"))
    draft = bool(first(data, "draft", "work_in_progress", "workInProgress"))

    # manual pipelines are terminal without a human; skipped ([ci skip], rules
    # filtering) only blocks when the merge gate actually requires a pipeline
    # ("ci_must_pass") or on legacy servers without detailed_merge_status ("",
    # fail-safe). Other gate values (not_approved, checking, ...) keep their
    # normal WAIT classification below.
    actionable_pipeline = (
        pipeline_status in FAIL_STATES
        or pipeline_status == "manual"
        or (pipeline_status == "skipped" and detailed_merge in {"", "ci_must_pass"})
    )
    blockers: list[str] = []
    # most actionable first: reason/report use blockers[0]
    if actionable_pipeline:
        blockers.append(f"pipeline {pipeline_status}")
    elif pipeline_status in PENDING_STATES:
        blockers.append(f"pipeline {pipeline_status}")
    if detailed_merge in {"conflict", "need_rebase"} or merge_status in {"cannot_be_merged", "cannot be merged"}:
        blockers.append(f"merge state: {detailed_merge or merge_status}")
    if detailed_merge in {"blocked_status", "checking", "unchecked", "ci_still_running", "not_approved", "discussions_not_resolved"}:
        blockers.append(f"merge gate: {detailed_merge}")
    if draft:
        blockers.append("draft")

    if state == "merged":
        conclusion, reason = "DONE", "MR is merged"
    elif state == "closed":
        conclusion, reason = "DONE", "MR is closed"
    elif actionable_pipeline or detailed_merge in {"conflict", "need_rebase"} or merge_status in {"cannot_be_merged", "cannot be merged"}:
        conclusion, reason = "ACTION", blockers[0] if blockers else "actionable remote blocker"
    elif pipeline_status in PENDING_STATES or draft or detailed_merge in {"blocked_status", "checking", "unchecked", "ci_still_running", "not_approved", "discussions_not_resolved"}:
        conclusion, reason = "WAIT", blockers[0] if blockers else "remote gate is still pending"
    elif pipeline_status in SUCCESS_STATES or detailed_merge == "mergeable":
        conclusion, reason = "DONE", "pipeline is green or MR is mergeable"
    else:
        conclusion, reason = "WAIT", "no actionable failure in one-shot snapshot"

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
