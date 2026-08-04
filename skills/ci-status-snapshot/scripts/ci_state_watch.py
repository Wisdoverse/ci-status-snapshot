#!/usr/bin/env python3
"""Silently watch PR/MR state and emit one JSON event when it changes."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from typing import Any

from ci_status_snapshot import detect_provider, github_snapshot, gitlab_snapshot


def take_snapshot(provider: str, selector: str) -> dict[str, Any]:
    resolved = detect_provider() if provider == "auto" else provider
    if resolved == "unknown":
        raise RuntimeError("Could not detect provider; pass --provider github or --provider gitlab")
    return github_snapshot(selector) if resolved == "github" else gitlab_snapshot(selector)


def fingerprint(snapshot: dict[str, Any]) -> str:
    # Only decision-relevant fields: conclusion flips (WAIT->ACTION/DONE), a new
    # push, human gates moving (review decision on GitHub — GitLab surfaces
    # approvals via merge_state — draft toggle, merge gate), or someone
    # disabling delegated auto-merge. Per-check progress (ci counts,
    # pending_checks) is deliberately excluded — waking the agent on every
    # completed check degrades into slow polling.
    stable = {
        "provider": snapshot.get("provider"),
        "number": snapshot.get("number"),
        "conclusion": snapshot.get("conclusion"),
        "state": snapshot.get("state"),
        "head_sha": snapshot.get("head_sha"),
        "review": snapshot.get("review"),
        "merge_state": snapshot.get("merge_state"),
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
) -> tuple[dict[str, Any], int]:
    started = monotonic_fn()
    baseline: str | None = None
    errors = 0
    total_errors = 0
    last_error = ""

    while True:
        try:
            current = snapshot_fn()
            current_fingerprint = fingerprint(current)
            errors = 0
            last_error = ""
            if baseline is None:
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
                return {"event": "error", "consecutive_errors": errors, "total_errors": total_errors, "error": last_error}, 2

        elapsed = monotonic_fn() - started
        if timeout_seconds > 0:
            if elapsed >= timeout_seconds:
                return {"event": "timeout", "elapsed_seconds": round(elapsed, 3), "total_errors": total_errors, "last_error": last_error}, 3
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
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--error-threshold", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0, help="backstop against orphan processes; 0 waits indefinitely")
    args = parser.parse_args()
    if not (math.isfinite(args.interval_seconds) and math.isfinite(args.timeout_seconds)):
        parser.error("interval and timeout must be finite numbers")
    # upper bounds keep time.sleep from raising OverflowError (~9.2e9s limit)
    if args.interval_seconds < 1 or args.interval_seconds > 86400 or args.error_threshold <= 0:
        parser.error("interval must be 1-86400 seconds; error threshold must be positive")
    if args.timeout_seconds < 0 or args.timeout_seconds > 31_536_000:
        parser.error("timeout must be 0-31536000 seconds (0 waits indefinitely)")

    event, exit_code = watch(
        lambda: take_snapshot(args.provider, args.selector),
        args.interval_seconds,
        args.error_threshold,
        args.timeout_seconds,
    )
    print(json.dumps(event, ensure_ascii=True, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
