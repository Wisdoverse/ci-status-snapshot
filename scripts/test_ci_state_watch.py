#!/usr/bin/env python3
"""Small deterministic checks for ci_state_watch.py."""

from __future__ import annotations

import json

import ci_status_snapshot as css
from ci_state_watch import watch


def snapshot(conclusion: str, pending: int) -> dict[str, object]:
    return {
        "provider": "github",
        "number": 87,
        "conclusion": conclusion,
        "state": "OPEN",
        "head_sha": "abc123",
        "review": "REVIEW_REQUIRED",
        "merge_state": "BLOCKED",
        "draft": False,
        "auto_merge": False,
        "ci": {"pending": pending},
        "pending_checks": ["test"] if pending else [],
        "blockers": ["review required"] if conclusion == "WAIT" else [],
    }


def assert_silent_until_change() -> None:
    states = iter([snapshot("WAIT", 1), snapshot("WAIT", 1), snapshot("DONE", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["event"] == "change"
    assert "initial" not in event
    assert event["current"]["conclusion"] == "DONE"


def assert_errors_are_debounced() -> None:
    calls = 0

    def fail() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary API error")

    event, exit_code = watch(fail, 0, 3, 0, sleep_fn=lambda _: None)
    assert calls == 3
    assert exit_code == 2
    assert event["event"] == "error"


def assert_terminal_baseline_fires_immediately() -> None:
    # snapshot->arm race: state left WAIT before the watcher started
    states = iter([snapshot("ACTION", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["event"] == "change"
    assert event.get("initial") is True
    assert event["current"]["conclusion"] == "ACTION"


def assert_check_progress_does_not_wake() -> None:
    # per-check progress (pending 3 -> 1) is not decision-relevant
    states = iter([snapshot("WAIT", 3), snapshot("WAIT", 2), snapshot("WAIT", 1), snapshot("DONE", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["conclusion"] == "DONE"


def assert_human_gate_change_wakes() -> None:
    # review approval ALONE (merge gate unchanged) keeps conclusion WAIT but
    # must wake — guards the review field's presence in the fingerprint
    approved = snapshot("WAIT", 0)
    approved["review"] = "APPROVED"
    states = iter([snapshot("WAIT", 0), approved])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["review"] == "APPROVED"

    # merge gate change alone must also wake
    unblocked = snapshot("WAIT", 0)
    unblocked["merge_state"] = "CLEAN"
    states = iter([snapshot("WAIT", 0), unblocked])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["merge_state"] == "CLEAN"


def assert_new_push_wakes() -> None:
    # a force-push/new commit changes only head_sha; must wake
    pushed = snapshot("WAIT", 1)
    pushed["head_sha"] = "def456"
    states = iter([snapshot("WAIT", 1), pushed])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["head_sha"] == "def456"


def assert_sleep_capped_by_timeout() -> None:
    sleeps: list[float] = []
    clock = iter([0.0, 0.5, 0.5, 2.0, 2.0])
    event, exit_code = watch(
        lambda: snapshot("WAIT", 1),
        3600,
        3,
        1.0,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: next(clock),
    )
    assert exit_code == 3
    assert event["event"] == "timeout"
    assert all(s <= 1.0 for s in sleeps), sleeps


def assert_recovery_resets_debounce() -> None:
    # fail, fail, recover, then 3 consecutive fails: only the final run trips
    calls = 0

    def sequence() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 3:
            return snapshot("WAIT", 1)
        raise RuntimeError(f"transient {calls}")

    event, exit_code = watch(sequence, 0, 3, 0, sleep_fn=lambda _: None)
    assert calls == 6, calls
    assert exit_code == 2
    assert event["consecutive_errors"] == 3
    assert event["total_errors"] == 5

def assert_draft_and_auto_merge_flips_wake() -> None:
    ready = snapshot("WAIT", 1)
    ready["draft"] = True
    event, exit_code = watch(lambda states=iter([snapshot("WAIT", 1), ready]): next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0 and event["current"]["draft"] is True

    disabled = snapshot("WAIT", 1)
    disabled["auto_merge"] = True
    event, exit_code = watch(lambda states=iter([disabled, snapshot("WAIT", 1)]): next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0 and event["current"]["auto_merge"] is False

def assert_timeout_reports_clean_last_error() -> None:
    calls = 0

    def flaky() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("blip")
        return snapshot("WAIT", 1)

    clock = iter([0.0, 1.0, 11.0])
    event, exit_code = watch(flaky, 1, 3, 10, sleep_fn=lambda _: None, monotonic_fn=lambda: next(clock))
    assert exit_code == 3
    assert event["event"] == "timeout"
    assert event["last_error"] == ""
    assert event["total_errors"] == 1

def assert_unexpected_exceptions_are_counted() -> None:
    # notify contract: odd CLI output must become an error event, not a crash
    boom = iter([SystemExit("gh died"), AttributeError("json was null"), OSError("no exec")])
    event, exit_code = watch(lambda: (_ for _ in ()).throw(next(boom)), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 2
    assert event["event"] == "error"
    assert event["error"] == "no exec"

def assert_classification_edges() -> None:
    # push canned gh/glab payloads through the REAL classifiers via run()
    real_run = css.run
    try:
        def fake(payload: dict) -> None:
            css.run = lambda cmd, timeout=25: (0, json.dumps(payload), "")

        fake({"number": 1, "state": "OPEN", "isDraft": False, "reviewDecision": "",
              "mergeStateStatus": "CLEAN", "statusCheckRollup": []})
        snap = css.github_snapshot(None)
        assert snap["conclusion"] == "DONE" and "registering" in snap["reason"], snap

        fake({"number": 2, "state": "OPEN", "isDraft": False, "reviewDecision": "",
              "mergeStateStatus": "BLOCKED",
              "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}]})
        assert css.github_snapshot(None)["conclusion"] == "ACTION"

        def gl(detailed: str) -> dict:
            return {"iid": 3, "state": "opened", "draft": False,
                    "detailed_merge_status": detailed,
                    "head_pipeline": {"id": 9, "status": "skipped"}}

        fake(gl("mergeable"))
        assert css.gitlab_snapshot(None)["conclusion"] == "DONE"
        fake(gl("ci_must_pass"))
        assert css.gitlab_snapshot(None)["conclusion"] == "ACTION"
        fake(gl("not_approved"))
        assert css.gitlab_snapshot(None)["conclusion"] == "WAIT"
    finally:
        css.run = real_run


if __name__ == "__main__":
    assert_silent_until_change()
    assert_errors_are_debounced()
    assert_terminal_baseline_fires_immediately()
    assert_check_progress_does_not_wake()
    assert_human_gate_change_wakes()
    assert_new_push_wakes()
    assert_sleep_capped_by_timeout()
    assert_recovery_resets_debounce()
    assert_draft_and_auto_merge_flips_wake()
    assert_timeout_reports_clean_last_error()
    assert_unexpected_exceptions_are_counted()
    assert_classification_edges()
    print("ci_state_watch.py checks passed")
