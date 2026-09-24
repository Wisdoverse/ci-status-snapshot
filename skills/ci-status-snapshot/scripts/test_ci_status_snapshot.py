#!/usr/bin/env python3
"""State-table regression tests for the two classifiers in ci_status_snapshot.py.

Canned `gh`/`glab` JSON is pushed through the REAL classifiers by monkeypatching
ci_status_snapshot.run, so the provider state tables (and their precedence) are
what is under test, not the subprocess plumbing.
"""

from __future__ import annotations

import json
from typing import Any

import ci_status_snapshot as css


def stub(payload: dict[str, Any]) -> None:
    css.run = lambda cmd, timeout=25: (0, json.dumps(payload), "")


def check(status: str = "COMPLETED", conclusion: str = "SUCCESS", name: str = "ci") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def gh(
    checks: Any = (),
    merge_state: str = "CLEAN",
    state: str = "OPEN",
    review: str = "",
    draft: bool = False,
    allow_no_pipeline: bool = False,
) -> dict[str, Any]:
    stub(
        {
            "number": 1,
            "state": state,
            "isDraft": draft,
            "reviewDecision": review,
            "mergeStateStatus": merge_state,
            "statusCheckRollup": list(checks),
        }
    )
    return css.github_snapshot("1", allow_no_pipeline=allow_no_pipeline)


_UNSET: Any = object()


def gl(
    pipeline: Any = "success",
    gate: str = "mergeable",
    state: str = "opened",
    merge_status: str = "",
    draft: bool = False,
    allow_no_pipeline: bool = False,
    head_pipeline: Any = _UNSET,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "iid": 3,
        "state": state,
        "draft": draft,
        "detailed_merge_status": gate,
        "merge_status": merge_status,
    }
    if head_pipeline is not _UNSET:
        payload["head_pipeline"] = head_pipeline
    elif pipeline is not None:
        payload["head_pipeline"] = {"id": 9, "status": pipeline}
    stub(payload)
    return css.gitlab_snapshot("3", allow_no_pipeline=allow_no_pipeline)


def capture_gitlab(selector: str | None, response: tuple[int, str, str] = (0, "", "")) -> tuple[list[tuple[list[str], int]], dict[str, Any] | None, str | None]:
    calls: list[tuple[list[str], int]] = []
    original_run = css.run

    def fake_run(cmd: list[str], timeout: int = 25) -> tuple[int, str, str]:
        calls.append((cmd, timeout))
        return response

    css.run = fake_run
    try:
        try:
            snapshot = css.gitlab_snapshot(selector)
            return calls, snapshot, None
        except SystemExit as exc:
            return calls, None, str(exc)
    finally:
        css.run = original_run


def test_gitlab_numeric_iid_uses_single_api_call() -> None:
    payload = json.dumps(
        {
            "iid": 396,
            "state": "opened",
            "draft": False,
            "detailed_merge_status": "mergeable",
            "head_pipeline": {"id": 9, "status": "success"},
        }
    )
    calls, snapshot, error = capture_gitlab("396", (0, payload, ""))
    assert error is None and snapshot is not None, error
    assert calls == [(["glab", "api", "projects/:fullpath/merge_requests/396"], 25)], calls
    assert snapshot["conclusion"] == "DONE", snapshot


def test_gitlab_non_numeric_selectors_keep_mr_view() -> None:
    payload = json.dumps({"iid": 396, "state": "opened", "detailed_merge_status": "mergeable"})
    for selector in [None, "", "feature/foo", "https://gitlab.example.test/group/project/-/merge_requests/396", "٣٩٦"]:
        calls, _, error = capture_gitlab(selector, (0, payload, ""))
        assert error is None, (selector, error)
        expected = ["glab", "mr", "view"]
        if selector:
            expected.append(selector)
        expected.extend(["--output", "json"])
        assert calls == [(expected, 25)], (selector, calls)


def test_gitlab_snapshot_errors_name_the_single_api_call() -> None:
    for response, expected in [
        ((7, "", "permission denied"), "permission denied"),
        ((124, "", "glab timed out"), "glab timed out"),
        ((0, "not-json", ""), "Could not parse JSON from glab api projects/:fullpath/merge_requests/396"),
    ]:
        calls, snapshot, error = capture_gitlab("396", response)
        assert snapshot is None and error is not None and expected in error, (response, snapshot, error)
        assert calls == [(["glab", "api", "projects/:fullpath/merge_requests/396"], 25)], calls


# --- GitHub -----------------------------------------------------------------


def test_github_merge_state_table() -> None:
    # MergeStateStatus (GraphQL): only CLEAN/HAS_HOOKS are positive evidence
    table = {
        "CLEAN": "DONE",
        "HAS_HOOKS": "DONE",
        "DIRTY": "ACTION",
        "BEHIND": "ACTION",
        "BLOCKED": "WAIT",
        "UNSTABLE": "WAIT",
        "UNKNOWN": "WAIT",
        "DRAFT": "WAIT",
    }
    for merge_state, expected in table.items():
        snap = gh(checks=[check()], merge_state=merge_state)
        assert snap["conclusion"] == expected, (merge_state, snap)


def test_github_unknown_merge_state_has_its_own_reason() -> None:
    snap = gh(checks=[check()], merge_state="UNKNOWN")
    assert snap["reason"] == "merge state still computing", snap


def test_github_unrecognized_merge_state_waits() -> None:
    # a value GitHub adds after this table was written must never reach DONE
    snap = gh(checks=[check()], merge_state="SOMETHING_NEW")
    assert snap["conclusion"] == "WAIT" and "something_new" in snap["reason"], snap


def test_github_failed_conclusions_are_action() -> None:
    # CheckConclusionState failures plus StatusContext (StatusState) failures
    for conclusion in ["FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "STALE", "STARTUP_FAILURE"]:
        snap = gh(checks=[check(conclusion=conclusion)], merge_state="BLOCKED")
        assert snap["conclusion"] == "ACTION", (conclusion, snap)
    for state in ["FAILURE", "ERROR"]:
        stub(
            {
                "number": 1,
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": "",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [{"context": "legacy-status", "state": state}],
            }
        )
        assert css.github_snapshot("1")["conclusion"] == "ACTION", state


def test_github_pending_checks_wait() -> None:
    for status in ["QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING"]:
        snap = gh(checks=[check(status=status, conclusion="")], merge_state="CLEAN")
        assert snap["conclusion"] == "WAIT", (status, snap)
        assert snap["ci"]["pending"] == 1, snap


def test_github_completed_without_conclusion_counts_pending() -> None:
    # COMPLETED + null conclusion is the conclusion write-back window, not green
    snap = gh(checks=[check(status="COMPLETED", conclusion=None)], merge_state="CLEAN")
    assert snap["conclusion"] == "WAIT", snap
    assert snap["ci"]["pending"] == 1 and snap["ci"]["success"] == 0, snap
    assert "without a conclusion" in snap["reason"], snap


def test_github_skipped_and_neutral_are_green() -> None:
    snap = gh(checks=[check(conclusion="SKIPPED"), check(conclusion="NEUTRAL"), check()], merge_state="HAS_HOOKS")
    assert snap["conclusion"] == "DONE" and snap["ci"]["success"] == 3, snap


def test_github_no_checks_wait_and_optout() -> None:
    # CLEAN can arrive before Actions registers a single check run: zero checks
    # waits by default, and only the explicit no-CI opt-out may call it DONE
    for merge_state in ["CLEAN", "HAS_HOOKS"]:
        waiting = gh(checks=[], merge_state=merge_state)
        assert waiting["conclusion"] == "WAIT", (merge_state, waiting)
        assert waiting["reason"] == "no checks reported yet", waiting
        assert waiting["blockers"] == ["no checks reported"], waiting
        opted_out = gh(checks=[], merge_state=merge_state, allow_no_pipeline=True)
        assert opted_out["conclusion"] == "DONE" and opted_out["blockers"] == [], (merge_state, opted_out)
    # registered checks, other merge states and terminal PRs ignore the flag
    for flag in (False, True):
        assert gh(checks=[check()], allow_no_pipeline=flag)["conclusion"] == "DONE", flag
        assert gh(checks=[check(status="IN_PROGRESS", conclusion="")], allow_no_pipeline=flag)["conclusion"] == "WAIT", flag
        assert gh(checks=[check(conclusion="FAILURE")], allow_no_pipeline=flag)["conclusion"] == "ACTION", flag
        assert gh(checks=[], merge_state="", allow_no_pipeline=flag)["conclusion"] == "WAIT", flag
        blocked = gh(checks=[], merge_state="BLOCKED", allow_no_pipeline=flag)
        assert blocked["conclusion"] == "WAIT" and blocked["reason"] == "merge state: blocked", (flag, blocked)
        assert gh(checks=[], merge_state="DIRTY", allow_no_pipeline=flag)["conclusion"] == "ACTION", flag
        assert gh(checks=[], review="REVIEW_REQUIRED", allow_no_pipeline=flag)["reason"] == "review required", flag
        assert gh(checks=[], draft=True, allow_no_pipeline=flag)["reason"] == "draft", flag
        for state in ["MERGED", "CLOSED"]:
            assert gh(checks=[], state=state, allow_no_pipeline=flag)["conclusion"] == "DONE", (flag, state)


def test_github_green_checks_need_a_positive_merge_state() -> None:
    # green checks with no/absent merge state are not positive merge evidence
    assert gh(checks=[check()], merge_state="")["conclusion"] == "WAIT"


def test_github_review_and_draft_gates() -> None:
    assert gh(checks=[check()], review="CHANGES_REQUESTED")["conclusion"] == "ACTION"
    assert gh(checks=[check()], review="REVIEW_REQUIRED")["conclusion"] == "WAIT"
    assert gh(checks=[check()], draft=True)["conclusion"] == "WAIT"
    assert gh(checks=[check()], review="APPROVED")["conclusion"] == "DONE"


def test_github_terminal_states_are_done() -> None:
    for state in ["MERGED", "CLOSED"]:
        snap = gh(checks=[check(conclusion="FAILURE")], merge_state="DIRTY", state=state)
        assert snap["conclusion"] == "DONE", (state, snap)


def test_github_failure_outranks_pending() -> None:
    snap = gh(checks=[check(conclusion="FAILURE"), check(status="IN_PROGRESS", conclusion="")], merge_state="BLOCKED")
    assert snap["conclusion"] == "ACTION" and snap["reason"].startswith("1 failed"), snap


# --- GitLab -----------------------------------------------------------------


def test_gitlab_mergeable_is_done_once_ci_is_not_running() -> None:
    for pipeline in ["success", "skipped"]:
        snap = gl(pipeline=pipeline, gate="mergeable")
        assert snap["conclusion"] == "DONE", (pipeline, snap)


def test_gitlab_absent_pipeline_mergeable_waits() -> None:
    # GitLab can report mergeable before it has created the pipeline: null,
    # omitted and id-less head pipelines are all "no CI yet", never DONE
    for head_pipeline in [None, {}, {"status": "success"}, {"web_url": "https://gitlab.example.test/p/1"}]:
        snap = gl(gate="mergeable", head_pipeline=head_pipeline)
        assert snap["conclusion"] == "WAIT", (head_pipeline, snap)
        assert snap["reason"] == "no pipeline observed", (head_pipeline, snap)
        assert snap["blockers"] == ["no pipeline observed"], (head_pipeline, snap)
    omitted = gl(pipeline=None, gate="mergeable")
    assert omitted["conclusion"] == "WAIT" and omitted["reason"] == "no pipeline observed", omitted
    assert css.classify_gitlab("opened", "", "mergeable", "", False, pipeline_observed=False) == (
        "WAIT",
        "no_pipeline_observed",
        "no pipeline observed",
    )
    # negative control: an identified green pipeline is still DONE
    green = gl(pipeline="success", gate="mergeable")
    assert green["conclusion"] == "DONE" and green["blockers"] == [], green


def test_gitlab_no_pipeline_precedence_and_optout() -> None:
    # the opt-out changes exactly one outcome: an open, mergeable MR with no
    # pipeline becomes DONE
    opted_out = gl(pipeline=None, gate="mergeable", allow_no_pipeline=True)
    assert opted_out["conclusion"] == "DONE" and opted_out["blockers"] == [], opted_out
    # every blocker ranked above a missing pipeline keeps its result either way
    cases = [
        ({"state": "merged", "gate": "not_open"}, "DONE", "MR is merged"),
        ({"state": "closed", "gate": "not_open"}, "DONE", "MR is closed"),
        ({"gate": "conflict"}, "ACTION", "merge state: conflict"),
        ({"gate": "", "merge_status": "cannot_be_merged"}, "ACTION", "merge state: cannot_be_merged"),
        ({"gate": "mergeable", "draft": True}, "WAIT", "draft"),
        ({"gate": "not_approved"}, "WAIT", "merge gate: not_approved"),
    ]
    for flag in (False, True):
        for kwargs, conclusion, reason in cases:
            snap = gl(pipeline=None, allow_no_pipeline=flag, **kwargs)
            assert (snap["conclusion"], snap["reason"]) == (conclusion, reason), (flag, kwargs, snap)
        # an observed pipeline is never affected by the flag
        failed = gl(pipeline="failed", gate="mergeable", allow_no_pipeline=flag)
        assert failed["conclusion"] == "ACTION" and failed["reason"] == "pipeline failed", (flag, failed)
        running = gl(pipeline="running", gate="mergeable", allow_no_pipeline=flag)
        assert running["conclusion"] == "WAIT" and "running" in running["reason"], (flag, running)
    # a settling gate with no pipeline waits either way; only the reason differs
    assert gl(pipeline=None, gate="checking")["reason"] == "no pipeline observed"
    assert gl(pipeline=None, gate="checking", allow_no_pipeline=True)["reason"] == "merge gate: checking"


def test_gitlab_observed_pipeline_without_status_waits() -> None:
    # an identified pipeline whose status is empty or unreadable is CI that
    # exists and cannot be judged: it waits even under the no-pipeline opt-out
    for status in ["", "unknown", "brand_new_status"]:
        for flag in (False, True):
            snap = gl(pipeline=status, gate="mergeable", allow_no_pipeline=flag)
            assert snap["conclusion"] == "WAIT", (status, flag, snap)
            assert snap["reason"].startswith("unrecognized pipeline status"), (status, flag, snap)
    # the genuinely absent pipeline follows the opt-out
    assert gl(pipeline=None, gate="mergeable")["reason"] == "no pipeline observed"
    assert gl(pipeline=None, gate="mergeable", allow_no_pipeline=True)["conclusion"] == "DONE"


def test_gitlab_unrecognized_pipeline_status_waits() -> None:
    # fail closed: a status outside every table is not evidence CI passed, and
    # no gate — mergeable or still settling — may outrank it
    for gate in ["mergeable", "checking", "ci_still_running", ""]:
        snap = gl(pipeline="weird_new_state", gate=gate)
        assert snap["conclusion"] == "WAIT" and "weird_new_state" in snap["reason"], (gate, snap)
    named = gl(pipeline="totally_new_state", gate="mergeable")
    assert "totally_new_state" in named["reason"], named


def test_gitlab_human_gate_survives_an_unknown_pipeline_status() -> None:
    # unknown blocks optimistic outcomes only: a known approval/discussion gate
    # is pipeline-independent and must still be reported as the blocker
    for gate in ["not_approved", "discussions_not_resolved", "merge_request_blocked", "blocked_status"]:
        snap = gl(pipeline="weird_new_state", gate=gate)
        assert snap["conclusion"] == "WAIT" and snap["reason"] == f"merge gate: {gate}", (gate, snap)


def test_gitlab_mergeable_with_running_pipeline_waits() -> None:
    # CI is not a required merge check here: the gate is open, CI is not done
    for pipeline in ["running", "pending", "created"]:
        snap = gl(pipeline=pipeline, gate="mergeable")
        assert snap["conclusion"] == "WAIT", (pipeline, snap)
        assert "mergeable" in snap["reason"] and pipeline in snap["reason"], snap


def test_gitlab_green_pipeline_alone_is_never_done() -> None:
    assert gl(pipeline="success", gate="")["conclusion"] == "WAIT"
    # legacy server: can_be_merged only proves basic mergeability
    assert gl(pipeline="success", gate="", merge_status="can_be_merged")["conclusion"] == "WAIT"


def test_gitlab_skipped_pipeline_rule() -> None:
    # skipped is actionable only where the merge gate actually needs a pipeline
    assert gl(pipeline="skipped", gate="")["conclusion"] == "ACTION"
    assert gl(pipeline="skipped", gate="not_approved")["conclusion"] == "WAIT"

    required = gl(pipeline="skipped", gate="ci_must_pass")
    assert required["conclusion"] == "ACTION" and "pipeline skipped" in required["blockers"], required
    # blockers must agree with the conclusion: a non-blocking skip is no blocker
    done = gl(pipeline="skipped", gate="mergeable")
    assert done["conclusion"] == "DONE" and done["blockers"] == [], done


def test_gitlab_actionable_pipelines() -> None:
    for status in ["failed", "canceled", "manual"]:
        snap = gl(pipeline=status, gate="")
        assert snap["conclusion"] == "ACTION" and snap["reason"] == f"pipeline {status}", snap


def test_gitlab_pending_pipelines_wait() -> None:
    for status in [
        "created",
        "waiting_for_resource",
        "waiting_for_callback",
        "preparing",
        "pending",
        "running",
        "scheduled",
        "canceling",
    ]:
        snap = gl(pipeline=status, gate="")
        assert snap["conclusion"] == "WAIT", (status, snap)


def test_gitlab_actionable_gates() -> None:
    # gates that never clear by waiting: a person must change something
    for gate in [
        "broken_status",
        "commits_status",
        "conflict",
        "jira_association_missing",
        "locked_lfs_files",
        "locked_paths",
        "need_rebase",
        "policies_denied",
        "requested_changes",
        "security_policy_violations",
        "title_regex",
    ]:
        snap = gl(pipeline="success", gate=gate)
        assert snap["conclusion"] == "ACTION", (gate, snap)


def test_gitlab_waiting_gates() -> None:
    for gate in [
        "approvals_syncing",
        "checking",
        "ci_must_pass",
        "ci_still_running",
        "discussions_not_resolved",
        "draft_status",
        "external_status_checks",
        "status_checks_must_pass",
        "merge_request_blocked",
        "blocked_status",
        "merge_time",
        "not_approved",
        "security_policy_pipeline_check",
        "preparing",
        "unchecked",
    ]:
        snap = gl(pipeline="success", gate=gate)
        assert snap["conclusion"] == "WAIT", (gate, snap)


def test_gitlab_legacy_merge_status() -> None:
    assert gl(pipeline="success", gate="", merge_status="cannot_be_merged")["conclusion"] == "ACTION"
    assert gl(pipeline="success", gate="", merge_status="unchecked")["conclusion"] == "WAIT"
    assert gl(pipeline="success", gate="", merge_status="cannot_be_merged_recheck")["conclusion"] == "WAIT"


def test_gitlab_terminal_states_are_done() -> None:
    # merged/closed MRs still report gate "not_open": DONE must carry no blockers
    for state in ["merged", "closed"]:
        snap = gl(pipeline="failed", gate="not_open", state=state)
        assert snap["conclusion"] == "DONE" and snap["blockers"] == [], (state, snap)
    stale = gl(pipeline="success", gate="not_open", state="merged", draft=True)
    assert stale["conclusion"] == "DONE" and stale["blockers"] == [], stale


def test_gitlab_unrecognized_gate_waits() -> None:
    snap = gl(pipeline="success", gate="brand_new_gate")
    assert snap["conclusion"] == "WAIT" and "brand_new_gate" in snap["reason"], snap


def test_gitlab_draft_waits() -> None:
    assert gl(pipeline="success", gate="", draft=True)["conclusion"] == "WAIT"
    # a stale mergeability recompute can report mergeable on a draft; draft wins
    assert gl(pipeline="success", gate="mergeable", draft=True)["conclusion"] == "WAIT"
    # but a red pipeline on a draft is still actionable
    assert gl(pipeline="failed", gate="mergeable", draft=True)["conclusion"] == "ACTION"


def test_allow_no_pipeline_cli_routing() -> None:
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    for provider in ["github", "gitlab"]:
        for flags, expected in [([], False), (["--allow-no-pipeline"], True)]:
            calls: list[tuple[str, str | None, bool]] = []

            def recorder(name: str) -> Any:
                def fake(selector: str | None, allow_no_pipeline: bool = False) -> dict[str, Any]:
                    calls.append((name, selector, allow_no_pipeline))
                    return {"conclusion": "WAIT"}

                return fake

            argv = ["ci_status_snapshot.py", "--provider", provider, "--selector", "12", "--json", *flags]
            with patch.object(css, "github_snapshot", recorder("github")), \
                 patch.object(css, "gitlab_snapshot", recorder("gitlab")), \
                 patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                assert css.main() == 0
            assert calls == [(provider, "12", expected)], (provider, flags, calls)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ci_status_snapshot.py classifier tests passed")
