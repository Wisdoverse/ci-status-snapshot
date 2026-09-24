#!/usr/bin/env python3
"""Silently watch PR/MR state and emit one JSON event when it changes."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ci_status_snapshot import detect_provider, github_snapshot, gitlab_snapshot


def take_snapshot(provider: str, selector: str, allow_no_pipeline: bool = False) -> dict[str, Any]:
    # called on every poll with the CLI's setting: the opt-out is never
    # inferred from, or frozen at, the first snapshot
    resolved = detect_provider() if provider == "auto" else provider
    if resolved == "unknown":
        raise RuntimeError("Could not detect provider; pass --provider github or --provider gitlab")
    snapshot_fn = github_snapshot if resolved == "github" else gitlab_snapshot
    return snapshot_fn(selector, allow_no_pipeline=allow_no_pipeline)


# Merge-gate values that describe the MACHINE still working, not a decision
# anyone made: GitLab's detailed_merge_status while pipelines/approval sync
# churn, and GitHub's transient UNKNOWN. A busy repository flaps between these
# every time its target branch moves — six merges produced nine such wake-ups
# in one observed session, each burning the model turn this watcher exists to
# save. They are folded into the last SOLID gate value for fingerprinting, so
# flapping among them is silence while a real gate move (approval landing,
# BLOCKED -> CLEAN, conflicts) still wakes.
TRANSIENT_MERGE_STATES = frozenset({
    "checking",
    "ci_still_running",
    "approvals_syncing",
    "unchecked",
    "preparing",
    "unknown",
    "",
})


def is_transient_merge_state(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.lower() in TRANSIENT_MERGE_STATES)


def fingerprint(snapshot: dict[str, Any], solid_merge_state: Any) -> str:
    # Only decision-relevant fields: conclusion flips (WAIT->ACTION/DONE), a new
    # push, human gates moving (review decision on GitHub — GitLab surfaces
    # approvals via merge_state — draft toggle, merge gate), or someone
    # disabling delegated auto-merge. Per-check progress (ci counts,
    # pending_checks) is deliberately excluded — waking the agent on every
    # completed check degrades into slow polling — and so are transient
    # merge-gate states, which carry the last solid value instead (see
    # TRANSIENT_MERGE_STATES).
    stable = {
        "provider": snapshot.get("provider"),
        "number": snapshot.get("number"),
        "conclusion": snapshot.get("conclusion"),
        "state": snapshot.get("state"),
        "head_sha": snapshot.get("head_sha"),
        "review": snapshot.get("review"),
        "merge_state": solid_merge_state,
        "draft": snapshot.get("draft"),
        "auto_merge": snapshot.get("auto_merge"),
    }
    return json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def watch(
    snapshot_fn: Callable[[], dict[str, Any]],
    interval_seconds: float,
    error_threshold: int,
    timeout_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    expected_head: str | None = None,
) -> tuple[dict[str, Any], int]:
    started = monotonic_fn()
    baseline: str | None = None
    solid_merge_state: Any = None
    errors = 0
    total_errors = 0
    last_error = ""
    last_snapshot: dict[str, Any] | None = None

    while True:
        try:
            current = dict(snapshot_fn())
            current["snapshot_time_utc"] = datetime.now(timezone.utc).isoformat()
            if expected_head is not None and not current.get("head_sha"):
                raise RuntimeError("snapshot has no head SHA to verify the watcher handoff")
            last_snapshot = current
            if not is_transient_merge_state(current.get("merge_state")):
                solid_merge_state = current.get("merge_state")
            current_fingerprint = fingerprint(current, solid_merge_state)
            errors = 0
            last_error = ""
            if baseline is None:
                # WAIT can remain WAIT while a push replaces the reviewed head
                # between the caller's snapshot and our first read. Do not
                # silently adopt that new head as the baseline.
                if expected_head is not None and current["head_sha"] != expected_head:
                    return {"event": "change", "initial": True, "reason": "head_changed",
                            "expected_head": expected_head, "current": current}, 0
                # Guard against the snapshot->arm race: if state already left
                # WAIT before the watcher started, report it now instead of
                # silently baselining a terminal state and burning the full
                # timeout (or hanging forever with --timeout-seconds 0).
                if current.get("conclusion") != "WAIT":
                    return {"event": "change", "initial": True, "current": current}, 0
                baseline = current_fingerprint
            elif current_fingerprint != baseline:
                return {"event": "change", "current": current}, 0
        # broad on purpose: the notify contract is exactly one JSON event, so
        # any per-poll failure (OSError from subprocess, TypeError on odd CLI
        # JSON, SystemExit from the snapshot helpers) must be counted and
        # emitted, never allowed to crash the process with an empty stdout
        except (Exception, SystemExit) as exc:
            errors += 1
            total_errors += 1
            last_error = str(exc)
            if errors >= error_threshold:
                return {"event": "error", "consecutive_errors": errors, "total_errors": total_errors,
                        "error": last_error, "last_snapshot": last_snapshot}, 2

        elapsed = monotonic_fn() - started
        if timeout_seconds > 0:
            if elapsed >= timeout_seconds:
                return {"event": "timeout", "elapsed_seconds": round(elapsed, 3), "total_errors": total_errors,
                        "last_error": last_error, "last_snapshot": last_snapshot}, 3
            sleep_fn(min(interval_seconds, timeout_seconds - elapsed))
        else:
            sleep_fn(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch CI outside model turns and emit one state event.")
    parser.add_argument("--provider", choices=["auto", "github", "gitlab"], default="auto")
    # required on purpose: without it gh/glab resolve "the PR of the current
    # branch" on every poll, so any checkout during the watcher's (up to 2h)
    # life silently retargets the watch at a different PR/MR
    parser.add_argument("--selector", required=True, help="PR/MR number, URL, or branch selector")
    parser.add_argument("--expected-head", help="Full head SHA from the caller's snapshot; detects a push before the first read")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--error-threshold", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0, help="backstop against orphan processes; 0 waits indefinitely")
    parser.add_argument(
        "--allow-no-pipeline",
        action="store_true",
        help=(
            "This project runs no CI for the PR/MR: a missing GitLab head pipeline or zero registered "
            "GitHub checks is nothing to wait for. Without it, a mergeable PR/MR with no CI yet is WAIT."
        ),
    )
    args = parser.parse_args()
    if args.expected_head is not None:
        args.expected_head = args.expected_head.lower()
        if len(args.expected_head) not in {40, 64} or any(c not in "0123456789abcdef" for c in args.expected_head):
            parser.error("expected head must be a full 40- or 64-character hexadecimal SHA")
    if not (math.isfinite(args.interval_seconds) and math.isfinite(args.timeout_seconds)):
        parser.error("interval and timeout must be finite numbers")
    # upper bounds keep time.sleep from raising OverflowError (~9.2e9s limit)
    if args.interval_seconds < 1 or args.interval_seconds > 86400 or args.error_threshold <= 0:
        parser.error("interval must be 1-86400 seconds; error threshold must be positive")
    if args.timeout_seconds < 0 or args.timeout_seconds > 31_536_000:
        parser.error("timeout must be 0-31536000 seconds (0 waits indefinitely)")

    event, exit_code = watch(
        lambda: take_snapshot(args.provider, args.selector, args.allow_no_pipeline),
        args.interval_seconds,
        args.error_threshold,
        args.timeout_seconds,
        expected_head=args.expected_head,
    )
    # Errors before the first successful read still need an unambiguous target.
    # last_snapshot on error/timeout is context, never a fresh status claim.
    event["watch"] = {"provider": args.provider, "selector": args.selector,
                      "expected_head": args.expected_head, "allow_no_pipeline": args.allow_no_pipeline,
                      "cwd": os.getcwd()}
    event["event_time_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(event, ensure_ascii=True, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
