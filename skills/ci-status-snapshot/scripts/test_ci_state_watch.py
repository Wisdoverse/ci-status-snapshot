#!/usr/bin/env python3
"""Small deterministic checks for ci_state_watch.py.

Classifier behaviour lives in test_ci_status_snapshot.py; this file only covers
the watch loop (silence, debounce, fingerprint, timeout).
"""

from __future__ import annotations

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


def test_silent_until_change() -> None:
    states = iter([snapshot("WAIT", 1), snapshot("WAIT", 1), snapshot("DONE", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["event"] == "change"
    assert "initial" not in event
    assert event["current"]["conclusion"] == "DONE"


def test_errors_are_debounced() -> None:
    calls = 0

    def fail() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary API error")

    event, exit_code = watch(fail, 0, 3, 0, sleep_fn=lambda _: None)
    assert calls == 3
    assert exit_code == 2
    assert event["event"] == "error"


def test_terminal_baseline_fires_immediately() -> None:
    # snapshot->arm race: state left WAIT before the watcher started
    states = iter([snapshot("ACTION", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["event"] == "change"
    assert event.get("initial") is True
    assert event["current"]["conclusion"] == "ACTION"


def test_check_progress_does_not_wake() -> None:
    # per-check progress (pending 3 -> 1) is not decision-relevant
    states = iter([snapshot("WAIT", 3), snapshot("WAIT", 2), snapshot("WAIT", 1), snapshot("DONE", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["conclusion"] == "DONE"


def test_human_gate_change_wakes() -> None:
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


def test_new_push_wakes() -> None:
    # a force-push/new commit changes only head_sha; must wake
    pushed = snapshot("WAIT", 1)
    pushed["head_sha"] = "def456"
    states = iter([snapshot("WAIT", 1), pushed])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["head_sha"] == "def456"


def test_push_before_first_read_wakes() -> None:
    pushed = snapshot("WAIT", 1)
    pushed["head_sha"] = "def456"
    states = iter([pushed])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None, expected_head="abc123")
    assert exit_code == 0
    assert event["initial"] is True and event["reason"] == "head_changed"
    assert event["expected_head"] == "abc123"
    assert event["current"]["head_sha"] == "def456"
    assert event["current"]["snapshot_time_utc"]


def test_matching_handoff_stays_silent_until_change() -> None:
    states = iter([snapshot("WAIT", 2), snapshot("WAIT", 1), snapshot("DONE", 0)])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None, expected_head="abc123")
    assert exit_code == 0 and "initial" not in event
    assert event["current"]["conclusion"] == "DONE"


def test_missing_head_cannot_establish_handoff() -> None:
    missing = snapshot("WAIT", 1)
    missing.pop("head_sha")
    event, exit_code = watch(lambda: missing, 0, 3, 0, sleep_fn=lambda _: None, expected_head="abc123")
    assert exit_code == 2 and event["consecutive_errors"] == 3
    assert event["last_snapshot"] is None


def test_error_preserves_last_observation_without_claiming_freshness() -> None:
    calls = 0

    def sequence() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return snapshot("WAIT", 1)
        raise RuntimeError("connection unavailable")

    event, exit_code = watch(sequence, 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 2 and "current" not in event
    assert event["last_snapshot"]["head_sha"] == "abc123"
    assert event["last_snapshot"]["snapshot_time_utc"]


def test_cli_emits_one_targeted_event() -> None:
    import io
    import json
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from ci_state_watch import main

    pushed = snapshot("WAIT", 1)
    pushed["head_sha"] = "b" * 40
    output = io.StringIO()
    with patch("sys.argv", ["ci_state_watch.py", "--provider", "gitlab", "--selector", "2400", "--expected-head", "a" * 40, "--timeout-seconds", "1"]), \
         patch("ci_state_watch.take_snapshot", return_value=pushed), redirect_stdout(output):
        assert main() == 0
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["watch"]["selector"] == "2400"
    assert event["watch"]["expected_head"] == "a" * 40
    assert event["watch"]["cwd"] and event["event_time_utc"]
    assert event["current"]["head_sha"] == "b" * 40


def test_sleep_capped_by_timeout() -> None:
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
    assert "current" not in event
    assert event["last_snapshot"]["head_sha"] == "abc123"
    assert event["last_snapshot"]["snapshot_time_utc"]


def test_recovery_resets_debounce() -> None:
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

def test_draft_and_auto_merge_flips_wake() -> None:
    ready = snapshot("WAIT", 1)
    ready["draft"] = True
    event, exit_code = watch(lambda states=iter([snapshot("WAIT", 1), ready]): next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0 and event["current"]["draft"] is True

    disabled = snapshot("WAIT", 1)
    disabled["auto_merge"] = True
    event, exit_code = watch(lambda states=iter([disabled, snapshot("WAIT", 1)]): next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0 and event["current"]["auto_merge"] is False

def test_timeout_reports_clean_last_error() -> None:
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

def test_unexpected_exceptions_are_counted() -> None:
    # notify contract: odd CLI output must become an error event, not a crash
    boom = iter([SystemExit("gh died"), AttributeError("json was null"), OSError("no exec")])
    event, exit_code = watch(lambda: (_ for _ in ()).throw(next(boom)), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 2
    assert event["event"] == "error"
    assert event["error"] == "no exec"

def test_transient_gate_churn_stays_silent() -> None:
    # A busy target branch flaps the GitLab gate mergeable -> checking ->
    # ci_still_running -> mergeable on every sibling merge, all while the
    # conclusion stays WAIT. None of that is a decision; waking on it degrades
    # into one model turn per sibling merge (observed: nine wake-ups across six
    # merges in one session).
    def gated(state: str, conclusion: str = "WAIT") -> dict[str, object]:
        s = snapshot(conclusion, 1 if conclusion == "WAIT" else 0)
        s["merge_state"] = state
        return s

    states = iter([
        gated("ci_still_running"),
        gated("checking"),
        gated("approvals_syncing"),
        gated("mergeable"),           # first solid while CI still runs: wakes (approval evidence)
    ])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["merge_state"] == "mergeable"

    # Once a solid value is known, flapping back through transients and
    # returning to the SAME solid value must not wake; only the terminal
    # conclusion does.
    states = iter([
        gated("mergeable"),
        gated("checking"),
        gated("ci_still_running"),
        gated("mergeable"),
        gated("checking"),
        gated("mergeable", "DONE"),
    ])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["conclusion"] == "DONE"

    # A SOLID gate move through a transient window still wakes: an approval
    # landing (not_approved -> mergeable) is a decision even mid-churn.
    states = iter([
        gated("not_approved"),
        gated("checking"),
        gated("mergeable"),
    ])
    event, exit_code = watch(lambda: next(states), 0, 3, 0, sleep_fn=lambda _: None)
    assert exit_code == 0
    assert event["current"]["merge_state"] == "mergeable"


def run_reads(sequence: list[dict[str, object]]) -> tuple[dict[str, object], int, int]:
    """(event, exit code, number of reads taken). Running out of reads is an error event."""
    reads = 0
    states = iter(sequence)

    def next_state() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return next(states)

    event, exit_code = watch(next_state, 0, 1, 0, sleep_fn=lambda _: None)
    return event, exit_code, reads


def with_(base: dict[str, object], **changes: object) -> dict[str, object]:
    updated = dict(base)
    updated.update(changes)
    return updated


def test_auto_merge_enable_is_silent_until_disabled() -> None:
    off = snapshot("WAIT", 1)
    on = with_(off, auto_merge=True)
    # enabling is silent, and the new value is remembered so the later
    # cancellation still wakes — including when the watcher started enabled
    for sequence in [[off, on, off], [off, on, on, on, off], [on, on, off]]:
        event, exit_code, reads = run_reads(sequence)
        assert event["event"] == "change" and exit_code == 0, (sequence, event)
        assert reads == len(sequence) and event["current"]["auto_merge"] is False, (reads, event)
    # a missing field is no observation: silent, and not a cancellation
    absent = dict(on)
    absent.pop("auto_merge")
    event, exit_code, reads = run_reads([on, absent, absent, with_(absent, conclusion="DONE")])
    assert event["event"] == "change" and reads == 4 and event["current"]["conclusion"] == "DONE", (reads, event)
    event, exit_code, reads = run_reads([on, absent, off])
    assert event["event"] == "change" and reads == 3, (reads, event)


def test_other_fingerprint_changes_and_transient_gates() -> None:
    base = with_(snapshot("WAIT", 1), provider="gitlab", review=None, merge_state="not_approved")
    # every other decision field still wakes on the same read auto-merge turns on
    for change in [
        {"head_sha": "def456"},
        {"review": "APPROVED"},
        {"draft": True},
        {"state": "closed"},
        {"merge_state": "mergeable"},
        {"conclusion": "ACTION"},
    ]:
        event, exit_code, reads = run_reads([base, with_(base, auto_merge=True, **change)])
        assert event["event"] == "change" and reads == 2, (change, event)
    # transient gate churn stays folded while auto-merge turns on
    solid = with_(base, merge_state="mergeable")
    event, exit_code, reads = run_reads([
        solid,
        with_(solid, merge_state="checking", auto_merge=True),
        with_(solid, merge_state="ci_still_running", auto_merge=True),
        with_(solid, auto_merge=True),
        with_(solid, merge_state="checking", auto_merge=True, conclusion="DONE"),
    ])
    assert event["event"] == "change" and reads == 5 and event["current"]["conclusion"] == "DONE", (reads, event)


def gl_pipeline(pipeline_id: object = None, status: str = "unknown", **changes: object) -> dict[str, object]:
    # the GitLab snapshot shape: ci carries the head pipeline id and status
    base = with_(snapshot("WAIT", 1), provider="gitlab", review=None, merge_state="mergeable", target="main")
    return with_(base, ci={"pipeline_id": pipeline_id, "status": status, "url": None}, **changes)


def test_pipeline_appearing_starting_or_replaced_wakes() -> None:
    absent = gl_pipeline()
    for before, after in [
        (absent, gl_pipeline(5, "running")),
        (absent, gl_pipeline(5, "created")),
        (gl_pipeline(5, "pending"), gl_pipeline(5, "running")),
        (gl_pipeline(5, "running"), gl_pipeline(6, "running")),
        (gl_pipeline(5, "pending"), gl_pipeline(6, "pending")),
    ]:
        event, exit_code, reads = run_reads([before, after])
        assert event["event"] == "change" and exit_code == 0 and reads == 2, (before["ci"], after["ci"], event)


def test_pipeline_churn_within_a_phase_stays_silent() -> None:
    done = with_(gl_pipeline(5, "success"), conclusion="DONE")
    for sequence in [
        # not started yet: created -> pending -> ... is not a decision
        [gl_pipeline(5, "created"), gl_pipeline(5, "waiting_for_resource"), gl_pipeline(5, "preparing"),
         gl_pipeline(5, "pending"), gl_pipeline(5, ""), done],
        # started: running, then green while an approval is still missing
        [gl_pipeline(5, "running", merge_state="not_approved"), gl_pipeline(5, "running", merge_state="not_approved"),
         gl_pipeline(5, "success", merge_state="not_approved"), with_(done, merge_state="not_approved")],
        # no pipeline, read after read
        [gl_pipeline(), gl_pipeline(), gl_pipeline(), with_(gl_pipeline(), conclusion="DONE")],
    ]:
        event, exit_code, reads = run_reads(sequence)
        assert event["event"] == "change" and reads == len(sequence), (reads, event)
        assert event["current"]["conclusion"] == "DONE", event
    # GitHub reports check counts, not a head pipeline: per-check progress is silent
    event, exit_code, reads = run_reads([snapshot("WAIT", 3), snapshot("WAIT", 0), snapshot("DONE", 0)])
    assert reads == 3 and event["current"]["conclusion"] == "DONE", (reads, event)


def test_target_change_wakes() -> None:
    for base in [with_(snapshot("WAIT", 1), target="main"), gl_pipeline(5, "running")]:
        event, exit_code, reads = run_reads([base, with_(base, target="release")])
        assert event["event"] == "change" and exit_code == 0 and reads == 2, (base["provider"], event)
        assert event["current"]["target"] == "release", event


def test_allow_no_pipeline_cli_routing() -> None:
    # the opt-out reaches the provider on EVERY poll, not only the first read
    import io
    import json
    from contextlib import redirect_stdout
    from unittest.mock import patch

    import ci_state_watch

    for flags, expected, sequence in [
        (["--allow-no-pipeline"], True, [snapshot("WAIT", 1), snapshot("DONE", 0)]),
        ([], False, [snapshot("DONE", 0)]),
    ]:
        calls: list[tuple[str, bool]] = []
        states = iter(sequence)

        def fake_gitlab(selector: str, allow_no_pipeline: bool = False) -> dict[str, object]:
            calls.append((selector, allow_no_pipeline))
            return next(states)

        def wrong_provider(selector: str, allow_no_pipeline: bool = False) -> dict[str, object]:
            raise AssertionError("github_snapshot called for a gitlab watch")

        output = io.StringIO()
        argv = ["ci_state_watch.py", "--provider", "gitlab", "--selector", "7",
                "--interval-seconds", "1", "--timeout-seconds", "60", *flags]
        with patch("sys.argv", argv), patch.object(ci_state_watch, "gitlab_snapshot", fake_gitlab), \
             patch.object(ci_state_watch, "github_snapshot", wrong_provider), redirect_stdout(output):
            assert ci_state_watch.main() == 0
        assert calls == [("7", expected)] * len(sequence), (flags, calls)
        event = json.loads(output.getvalue())
        assert event["event"] == "change" and event["current"]["conclusion"] == "DONE", event
        assert event["watch"]["allow_no_pipeline"] is expected, event


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ci_state_watch.py checks passed")
