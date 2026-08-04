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


def gh(checks: Any = (), merge_state: str = "CLEAN", state: str = "OPEN", review: str = "", draft: bool = False) -> dict[str, Any]:
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
    return css.github_snapshot("1")


def gl(pipeline: Any = "success", gate: str = "mergeable", state: str = "opened", merge_status: str = "", draft: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "iid": 3,
        "state": state,
        "draft": draft,
        "detailed_merge_status": gate,
        "merge_status": merge_status,
    }
    if pipeline is not None:
        payload["head_pipeline"] = {"id": 9, "status": pipeline}
    stub(payload)
    return css.gitlab_snapshot("3")


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


def test_github_no_checks_needs_a_positive_merge_state() -> None:
    clean = gh(checks=[], merge_state="CLEAN")
    assert clean["conclusion"] == "DONE" and "registering" in clean["reason"], clean
    assert gh(checks=[], merge_state="HAS_HOOKS")["conclusion"] == "DONE"
    assert gh(checks=[], merge_state="")["conclusion"] == "WAIT"


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
    for pipeline in ["success", "skipped", "", None]:
        snap = gl(pipeline=pipeline, gate="mergeable")
        assert snap["conclusion"] == "DONE", (pipeline, snap)


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ci_status_snapshot.py classifier tests passed")
