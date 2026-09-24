#!/usr/bin/env python3
"""Smoke tests for ci_merge_delegate.py using a stubbed glab executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


SCRIPT = Path(__file__).with_name("ci_merge_delegate.py")


def write_glab_stub(directory: Path) -> None:
    stub = directory / "glab"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            mode = os.environ["GLAB_STUB_MODE"]
            path = sys.argv[2]

            def mr(pipeline_status, gate, **extra):
                body = {
                    "state": "opened",
                    "sha": "abc123",
                    "detailed_merge_status": gate,
                    "merge_when_pipeline_succeeds": False,
                    "web_url": "https://gitlab.example.test/group/project/-/merge_requests/396",
                }
                if pipeline_status:
                    body["head_pipeline"] = {
                        "id": extra.pop("pipeline_id", 123),
                        "status": pipeline_status,
                        "web_url": "https://gitlab.example.test/group/project/-/pipelines/123"
                    }
                body.update(extra)
                print(json.dumps(body))
                sys.exit(0)

            if "merge_requests/396" in path:
                if mode == "html_response":
                    print("<html>login</html>")
                    sys.exit(0)
                if mode == "skipped_not_approved":
                    mr("skipped", "not_approved")
                if mode == "skipped_ci_must_pass":
                    mr("skipped", "ci_must_pass")
                if mode == "requested_changes":
                    mr("success", "requested_changes")
                if mode == "mergeable_running":
                    mr("running", "mergeable")
                if mode == "unknown_status_auto_merge":
                    mr("totally_new_state", "mergeable", merge_when_pipeline_succeeds=True)
                if mode == "literal_unknown_status_auto_merge":
                    mr("unknown", "mergeable", merge_when_pipeline_succeeds=True)
                if mode == "weird_status_settling_gate_auto_merge":
                    mr("weird_new_state", "checking", merge_when_pipeline_succeeds=True)
                if mode == "weird_status_human_gate_auto_merge":
                    mr("weird_new_state", "not_approved", merge_when_pipeline_succeeds=True)
                if mode == "draft_no_pipeline":
                    mr(None, "unchecked", draft=True)
                if mode == "bare_running_auto_merge":
                    mr("running", "", merge_when_pipeline_succeeds=True)
                if mode == "draft_auto_merge_running":
                    mr("running", "ci_still_running", merge_when_pipeline_succeeds=True, draft=True)
                if mode == "auto_merge_checking":
                    mr("success", "checking", merge_when_pipeline_succeeds=True)
                if mode == "auto_merge_not_approved":
                    mr("success", "not_approved", merge_when_pipeline_succeeds=True)
                if mode == "title_regex":
                    mr("success", "title_regex")
                if mode == "no_pipeline":
                    mr(None, "unchecked")
                if mode == "jobs_api_error":
                    mr("failed", "ci_must_pass", pipeline_id=999)
                if mode == "merged":
                    print(json.dumps({
                        "state": "merged",
                        "sha": "abc123",
                        "detailed_merge_status": "merged",
                        "merge_when_pipeline_succeeds": False,
                        "merged_at": "2026-06-16T18:00:00Z",
                        "merge_commit_sha": "def456",
                        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/396",
                        "head_pipeline": {
                            "id": 123,
                            "status": "success",
                            "web_url": "https://gitlab.example.test/group/project/-/pipelines/123"
                        }
                    }))
                    sys.exit(0)
                if mode == "failed":
                    print(json.dumps({
                        "state": "opened",
                        "sha": "abc123",
                        "detailed_merge_status": "ci_must_pass",
                        "merge_when_pipeline_succeeds": True,
                        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/396",
                        "head_pipeline": {
                            "id": 123,
                            "status": "failed",
                            "web_url": "https://gitlab.example.test/group/project/-/pipelines/123"
                        }
                    }))
                    sys.exit(0)
                if mode == "running_auto_merge":
                    print(json.dumps({
                        "state": "opened",
                        "sha": "abc123",
                        "detailed_merge_status": "ci_still_running",
                        "merge_when_pipeline_succeeds": True,
                        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/396",
                        "head_pipeline": {
                            "id": 123,
                            "status": "running",
                            "web_url": "https://gitlab.example.test/group/project/-/pipelines/123"
                        }
                    }))
                    sys.exit(0)

            if "pipelines/123/jobs" in path:
                print(json.dumps([
                    {
                        "id": 10,
                        "name": "rust:test",
                        "stage": "verify",
                        "status": "failed",
                        "failure_reason": "script_failure",
                        "web_url": "https://gitlab.example.test/group/project/-/jobs/10"
                    },
                    {
                        "id": 11,
                        "name": "web:build",
                        "stage": "verify",
                        "status": "success",
                        "failure_reason": None,
                        "web_url": "https://gitlab.example.test/group/project/-/jobs/11"
                    }
                ]))
                sys.exit(0)

            print("unexpected path: " + path, file=sys.stderr)
            sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)


def run_snapshot(mode: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        write_glab_stub(temp_dir)
        env = os.environ.copy()
        env["PATH"] = f"{temp_dir}{os.pathsep}{env['PATH']}"
        env["GLAB_STUB_MODE"] = mode
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--provider",
                "gitlab",
                "--project",
                "115",
                "--selector",
                "396",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )


def test_merged() -> None:
    proc = run_snapshot("merged")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merged"
    assert payload["mr"] == 396
    assert payload["pipeline_id"] == 123
    assert payload["merge_commit"] == "def456"


def test_failed_pipeline_includes_failed_jobs() -> None:
    proc = run_snapshot("failed")
    assert proc.returncode == 2, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "pipeline_failed"
    assert payload["failed_jobs"] == [
        {
            "id": 10,
            "name": "rust:test",
            "stage": "verify",
            "status": "failed",
            "failure_reason": "script_failure",
            "web_url": "https://gitlab.example.test/group/project/-/jobs/10",
        }
    ]


def test_running_auto_merge_delegates_without_blocking() -> None:
    proc = run_snapshot("running_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "delegated_auto_merge"
    assert payload["auto_merge"] is True
    assert payload["pipeline_status"] == "running"

    # and on a legacy server with no detailed_merge_status at all
    proc = run_snapshot("bare_running_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout)["result"] == "delegated_auto_merge"


def test_skipped_pipeline_with_human_gate_is_merge_blocked() -> None:
    # skipped is not a failure: the approval gate is what stops this MR, and
    # reporting pipeline_skipped with an empty failed_jobs list sent the agent
    # hunting for a CI problem that does not exist
    proc = run_snapshot("skipped_not_approved")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merge_blocked", payload
    assert "failed_jobs" not in payload, payload


def test_skipped_pipeline_with_required_ci_is_actionable() -> None:
    proc = run_snapshot("skipped_ci_must_pass")
    assert proc.returncode == 2, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "pipeline_skipped", payload


def test_requested_changes_is_merge_blocked() -> None:
    proc = run_snapshot("requested_changes")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    assert json.loads(proc.stdout)["result"] == "merge_blocked"


def test_mergeable_gate_with_running_pipeline_is_waiting() -> None:
    # gate open, CI still running, nobody delegated the merge: nothing terminal
    proc = run_snapshot("mergeable_running")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting", payload


def test_unrecognized_pipeline_status_is_not_delegated() -> None:
    # a status we cannot classify is not a delegated merge, mergeable gate or not
    proc = run_snapshot("unknown_status_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting", payload


def test_non_json_response_is_an_api_error() -> None:
    # glab exiting 0 with a proxy/login page must stay inside the 0/2/3/4 contract
    proc = run_snapshot("html_response")
    assert proc.returncode == 4, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "api_error" and payload["last_error"], payload


def test_literal_unknown_pipeline_status_is_not_delegated() -> None:
    # a head pipeline that exists and reports "unknown" is not an absent
    # pipeline: it must fail closed, not ride the mergeable gate to delegation
    proc = run_snapshot("literal_unknown_status_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting", payload


def test_unrecognized_status_behind_settling_gate_is_not_delegated() -> None:
    # the settling gate must not outrank a pipeline status we cannot classify
    proc = run_snapshot("weird_status_settling_gate_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting", payload


def test_unrecognized_status_behind_human_gate_is_still_blocked() -> None:
    # the other direction: an unreadable pipeline must not hide the approval gate
    proc = run_snapshot("weird_status_human_gate_auto_merge")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merge_blocked", payload


def test_draft_without_pipeline_reports_the_draft() -> None:
    proc = run_snapshot("draft_no_pipeline")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting" and payload["draft"] is True, payload


def test_draft_with_auto_merge_is_not_delegated() -> None:
    # GitLab never auto-merges a draft, however green the pipeline gets
    proc = run_snapshot("draft_auto_merge_running")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "waiting", payload


def test_auto_merge_with_settling_gate_is_delegated() -> None:
    # pipeline green, auto-merge armed, gate still recomputing: GitLab will
    # merge this without us, so it is delegation, not waiting
    proc = run_snapshot("auto_merge_checking")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "delegated_auto_merge", payload


def test_auto_merge_with_human_gate_is_not_delegated() -> None:
    # an approval may never come: armed auto-merge must not hide a stuck MR
    proc = run_snapshot("auto_merge_not_approved")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merge_blocked", payload


def test_gate_that_never_clears_is_merge_blocked() -> None:
    proc = run_snapshot("title_regex")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merge_blocked", payload
    assert "failed_jobs" not in payload and "failed_jobs_error" not in payload, payload


def test_missing_pipeline_is_not_waiting() -> None:
    proc = run_snapshot("no_pipeline")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "no_pipeline_observed", payload


def test_failed_jobs_api_error_is_explicit() -> None:
    # a jobs API failure must not look like "pipeline failed, nothing failed"
    proc = run_snapshot("jobs_api_error")
    assert proc.returncode == 2, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "pipeline_failed", payload
    assert payload["failed_jobs_error"], payload
    assert "failed_jobs" not in payload, payload


# --- scenario stub: every glab call is logged, responses are served in order --

HEAD = "a" * 40
MR_PATH = "projects/115/merge_requests/396"
NOTES_PATH = f"{MR_PATH}/notes?order_by=created_at&sort=desc&per_page=100&page=1"
MERGE_PATH = f"{MR_PATH}/merge"
MR_GET = ["api", MR_PATH]
NOTES_GET = ["api", NOTES_PATH]
# the no-flag payload shape; --enable output may add fields, this may not
BASE_KEYS = [
    "result", "provider", "project", "mr", "state", "sha", "merge_status", "auto_merge",
    "draft", "pipeline_id", "pipeline_status", "web_url", "snapshot_time_utc",
]

SCENARIO_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    argv = sys.argv[1:]
    log = os.environ["GLAB_LOG"]
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")
    with open(os.environ["GLAB_SCENARIO"], encoding="utf-8") as fh:
        scenario = json.load(fh)
    path = argv[1] if len(argv) > 1 else ""
    if "--method" in argv:
        key = "put"
    elif "/notes?" in path:
        key = "notes"
    elif "/jobs?" in path:
        key = "jobs"
    elif path.endswith("/merge_requests/396"):
        key = "mr"
    else:
        print("unexpected call: " + json.dumps(argv), file=sys.stderr)
        sys.exit(1)
    counter = log + "." + key
    index = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as fh:
        fh.write(str(index + 1))
    responses = scenario.get(key, [])
    if index >= len(responses):
        print("no response %d scripted for %s" % (index, key), file=sys.stderr)
        sys.exit(1)
    response = responses[index]
    if "json" in response:
        print(json.dumps(response["json"]))
    else:
        sys.stdout.write(response.get("stdout", ""))
        sys.stderr.write(response.get("stderr", ""))
    sys.exit(response.get("code", 0))
    """
)


def ok(body: object) -> dict[str, object]:
    return {"json": body}


def fail(stderr: str = "HTTP 502 Bad Gateway", code: int = 1) -> dict[str, object]:
    return {"code": code, "stderr": stderr}


def pipeline(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"id": 123, "sha": HEAD, "status": "running", "created_at": "2026-09-24T10:00:00.000Z"}
    body.update(overrides)
    return body


def mr_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "state": "opened",
        "sha": HEAD,
        "target_branch": "main",
        "draft": False,
        "detailed_merge_status": "mergeable",
        "merge_when_pipeline_succeeds": False,
        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/396",
        "head_pipeline": pipeline(),
    }
    body.update(overrides)
    return body


def run_scenario(scenario: dict[str, object], *flags: str, as_json: bool = True) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        stub = temp_dir / "glab"
        stub.write_text(SCENARIO_STUB, encoding="utf-8")
        stub.chmod(0o755)
        (temp_dir / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
        log = temp_dir / "calls.log"
        env = os.environ.copy()
        env["PATH"] = f"{temp_dir}{os.pathsep}{env['PATH']}"
        env["GLAB_SCENARIO"] = str(temp_dir / "scenario.json")
        env["GLAB_LOG"] = str(log)
        cmd = [sys.executable, str(SCRIPT), "--provider", "gitlab", "--project", "115", "--selector", "396", *flags]
        if as_json:
            cmd.append("--json")
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return proc, calls


def result_of(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert proc.stdout, proc.stderr
    return json.loads(proc.stdout)


def test_delegate_mergeable_no_pipeline_and_optout() -> None:
    # GitLab can report mergeable before the pipeline exists: by default that
    # is no_pipeline_observed (exit 0), never a mergeable_unmerged hand-off
    no_pipeline = {"mr": [ok(mr_body(head_pipeline=None))]}
    proc, calls = run_scenario(no_pipeline)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = result_of(proc)
    assert payload["result"] == "no_pipeline_observed", payload
    assert sorted(payload) == sorted(BASE_KEYS), payload
    assert calls == [MR_GET], calls

    proc, calls = run_scenario(no_pipeline, "--allow-no-pipeline")
    assert proc.returncode == 3, proc.stderr or proc.stdout
    assert result_of(proc)["result"] == "mergeable_unmerged", proc.stdout
    assert calls == [MR_GET], calls

    # the delegate names its result from the classifier alone: under the
    # opt-out a pipeline-free MR with armed auto-merge and a settling gate is
    # delegated, not a second, delegate-local "no pipeline" verdict
    settling = {"mr": [ok(mr_body(head_pipeline=None, detailed_merge_status="checking", merge_when_pipeline_succeeds=True))]}
    proc, _ = run_scenario(settling, "--allow-no-pipeline")
    assert proc.returncode == 0 and result_of(proc)["result"] == "delegated_auto_merge", proc.stdout
    proc, _ = run_scenario(settling)
    assert proc.returncode == 0 and result_of(proc)["result"] == "no_pipeline_observed", proc.stdout

    # an unrelated no-flag result keeps its exact JSON and text shape
    running = {"mr": [ok(mr_body(merge_when_pipeline_succeeds=True))]}
    for flags in [(), ("--allow-no-pipeline",)]:
        proc, calls = run_scenario(running, *flags)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        payload = result_of(proc)
        assert payload["result"] == "delegated_auto_merge" and sorted(payload) == sorted(BASE_KEYS), payload
        assert calls == [MR_GET], calls
        proc, _ = run_scenario(running, *flags, as_json=False)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        assert [line.split("=", 1)[0] for line in proc.stdout.splitlines()] == BASE_KEYS, proc.stdout
        assert proc.stdout.splitlines()[0] == "result=delegated_auto_merge", proc.stdout



# --- --enable -----------------------------------------------------------------

JOBS_GET = ["api", "projects/115/pipelines/123/jobs?per_page=100"]
PUT_PAIRS = [
    ("--method", "PUT"),
    ("--field", "merge_when_pipeline_succeeds=true"),
    ("--field", "auto_merge=true"),
    ("--raw-field", f"sha={HEAD}"),
]


def pairs(argv: list[str]) -> list[tuple[str, str]]:
    return list(zip(argv, argv[1:]))


def puts(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "--method" in call]


def retarget(created_at: str, body: str = "changed target branch from `develop` to `main`", system: bool = True) -> dict[str, object]:
    return {"id": 1, "body": body, "system": system, "created_at": created_at}


def enable(before: dict[str, object], after: dict[str, object] | None = None, notes: object = (), put: dict[str, object] | None = None) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, object]]:
    # the PUT response always looks like success: only the re-read may decide
    scenario: dict[str, object] = {
        "mr": [ok(before), ok(after if after is not None else mr_body(merge_when_pipeline_succeeds=True))],
        # a dict is a raw scripted response; anything else is the list of notes
        "notes": [notes if isinstance(notes, dict) else ok(list(notes))],
        "put": [put if put is not None else ok(mr_body(merge_when_pipeline_succeeds=True))],
        "jobs": [ok([{"id": 10, "name": "unit", "stage": "test", "status": "failed"}])],
    }
    proc, calls = run_scenario(scenario, "--enable")
    return proc, calls, result_of(proc)


def test_enable_running_current_head_calls_once() -> None:
    proc, calls, payload = enable(mr_body())
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert payload["result"] == "delegated_auto_merge" and payload["auto_merge"] is True, payload
    assert payload["put_attempted"] is True and "put_error" not in payload, payload
    # exact reads, bounded notes page, one PUT, one confirming re-read
    assert len(calls) == 4, calls
    assert calls[0] == MR_GET and calls[1] == NOTES_GET and calls[3] == MR_GET, calls
    put = calls[2]
    assert put[:2] == ["api", MERGE_PATH], put
    for flag, value in PUT_PAIRS:
        assert (flag, value) in pairs(put), (flag, value, put)
    # typed booleans only: a --raw-field boolean is the string "true"
    assert not any(flag == "--raw-field" and value.split("=")[0] != "sha" for flag, value in pairs(put)), put
    assert len(puts(calls)) == 1, calls


def test_enable_refuses_ineligible_mr() -> None:
    cases = [
        ({"state": "closed"}, "state_not_open"),
        ({"state": "merged"}, "state_not_open"),
        ({"state": "locked"}, "state_not_open"),
        ({"draft": True}, "draft"),
        ({"sha": ""}, "head_sha_missing"),
        ({"head_pipeline": None}, "no_pipeline_observed"),
        ({"head_pipeline": {"sha": HEAD, "status": "running", "created_at": "2026-09-24T10:00:00Z"}}, "no_pipeline_observed"),
        ({"head_pipeline": pipeline(sha=None)}, "pipeline_metadata_missing"),
        ({"head_pipeline": pipeline(status="")}, "pipeline_metadata_missing"),
        ({"head_pipeline": pipeline(created_at=None)}, "pipeline_metadata_missing"),
        ({"head_pipeline": pipeline(sha="b" * 40)}, "pipeline_head_mismatch"),
        ({"head_pipeline": pipeline(status="pending")}, "pipeline_not_running"),
        ({"head_pipeline": pipeline(status="created")}, "pipeline_not_running"),
        ({"head_pipeline": pipeline(status="success")}, "pipeline_not_running"),
        ({"head_pipeline": pipeline(status="brand_new_status")}, "pipeline_not_running"),
        ({"detailed_merge_status": "not_approved"}, "merge_gate_blocked"),
        ({"detailed_merge_status": "discussions_not_resolved"}, "merge_gate_blocked"),
        ({"detailed_merge_status": "conflict"}, "merge_gate_blocked"),
        ({"detailed_merge_status": "need_rebase"}, "merge_gate_blocked"),
        ({"detailed_merge_status": "", "merge_status": "cannot_be_merged"}, "merge_gate_blocked"),
        ({"detailed_merge_status": "brand_new_gate"}, "merge_gate_unknown"),
        ({"detailed_merge_status": ""}, "merge_gate_unknown"),
    ]
    for overrides, reason in cases:
        proc, calls, payload = enable(mr_body(**overrides))
        assert proc.returncode == 3, (overrides, proc.stderr or proc.stdout)
        assert payload["result"] == "enable_refused" and payload["reason"] == reason, (overrides, payload)
        assert payload["put_attempted"] is False, payload
        assert calls == [MR_GET], (overrides, calls)
    # the no-pipeline opt-out never relaxes the running-pipeline requirement
    proc, calls = run_scenario({"mr": [ok(mr_body(head_pipeline=None))]}, "--enable", "--allow-no-pipeline")
    payload = result_of(proc)
    assert proc.returncode == 3 and payload["reason"] == "no_pipeline_observed", payload
    assert calls == [MR_GET], calls
    # the settling gates that clear by themselves are eligible
    for gate in ["mergeable", "ci_still_running", "ci_must_pass", "checking"]:
        proc, calls, payload = enable(mr_body(detailed_merge_status=gate))
        assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", (gate, payload)
        assert len(puts(calls)) == 1, (gate, calls)
    # terminal CI keeps the no-flag triage result and exit 2, with no mutation
    for overrides, result in [
        ({"head_pipeline": pipeline(status="failed")}, "pipeline_failed"),
        ({"head_pipeline": pipeline(status="canceled")}, "pipeline_canceled"),
        ({"head_pipeline": pipeline(status="manual")}, "pipeline_manual"),
        ({"head_pipeline": pipeline(status="skipped"), "detailed_merge_status": "ci_must_pass"}, "pipeline_skipped"),
        ({"head_pipeline": pipeline(status="failed"), "draft": True}, "pipeline_failed"),
    ]:
        proc, calls, payload = enable(mr_body(**overrides))
        assert proc.returncode == 2, (overrides, proc.stderr or proc.stdout)
        assert payload["result"] == result and payload["failed_jobs"], (overrides, payload)
        assert calls == [MR_GET, JOBS_GET], (overrides, calls)


def test_enable_uses_newest_system_retarget() -> None:
    # head pipeline created at 10:00; notes arrive newest first
    newest_after_pipeline = [retarget("2026-09-24T10:05:00.000Z"), retarget("2026-09-24T09:00:00.000Z")]
    proc, calls, payload = enable(mr_body(), notes=newest_after_pipeline)
    assert proc.returncode == 3 and payload["reason"] == "pipeline_before_retarget", payload
    assert not puts(calls), calls

    capitalised = [retarget("2026-09-24T10:05:00.000Z", body="Changed target branch from `develop` to `main`")]
    proc, calls, payload = enable(mr_body(), notes=capitalised)
    assert payload["reason"] == "pipeline_before_retarget" and not puts(calls), payload

    # a user comment quoting the phrase is not a retarget
    quoted = [
        retarget("2026-09-24T10:05:00.000Z", body="changed target branch from main? ask first", system=False),
        retarget("2026-09-24T09:00:00.000Z"),
    ]
    proc, calls, payload = enable(mr_body(), notes=quoted)
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload
    assert len(puts(calls)) == 1, calls

    # an unrelated newer system note does not stand in for the retarget time
    unrelated_first = [
        retarget("2026-09-24T10:30:00.000Z", body="added 1 commit"),
        retarget("2026-09-24T09:00:00.000Z"),
    ]
    proc, calls, payload = enable(mr_body(), notes=unrelated_first)
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload


def test_enable_detects_automatic_retarget_on_branch_deletion() -> None:
    # deleting the old target branch retargets the MR automatically, and some
    # self-hosted GitLab versions record only this note, no "changed target branch"
    body = "deleted the `feature/parent` branch. This merge request now targets the `main` branch"
    note = retarget("2026-03-01T12:00:00.500Z", body=body)
    before_note = mr_body(head_pipeline=pipeline(created_at="2026-03-01T11:59:58.000Z"))
    proc, calls, payload = enable(before_note, notes=[note])
    assert proc.returncode == 3 and payload["reason"] == "pipeline_before_retarget", payload
    assert not puts(calls), calls
    after_note = mr_body(head_pipeline=pipeline(created_at="2026-03-01T12:01:31.250Z"))
    proc, calls, payload = enable(after_note, notes=[note])
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload
    assert len(puts(calls)) == 1, calls
    # the same words in a user comment are not a retarget
    proc, calls, payload = enable(before_note, notes=[retarget("2026-03-01T12:00:00.500Z", body=body, system=False)])
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload


def test_enable_retarget_boundary_and_page_limit() -> None:
    # strictly after: a pipeline created at the retarget instant ran against
    # either target, so it cannot authorize the merge
    for note_time in ["2026-09-24T10:00:00.000Z", "2026-09-24T12:00:00.000+02:00"]:
        proc, calls, payload = enable(mr_body(), notes=[retarget(note_time)])
        assert proc.returncode == 3 and payload["reason"] == "pipeline_before_retarget", (note_time, payload)
        assert not puts(calls), calls
    proc, calls, payload = enable(mr_body(), notes=[retarget("2026-09-24T09:59:59.999Z")])
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload

    # a full page with no retarget leaves older history unread
    unrelated = [retarget("2026-09-24T09:00:00.000Z", body="added 1 commit")]
    proc, calls, payload = enable(mr_body(), notes=unrelated * 100)
    assert proc.returncode == 3 and payload["reason"] == "retarget_history_incomplete", payload
    assert not puts(calls), calls
    proc, calls, payload = enable(mr_body(), notes=unrelated * 99)
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload

    # unreadable times refuse rather than guess
    for before, notes in [
        (mr_body(), [retarget("yesterday")]),
        (mr_body(), [retarget("2026-09-24T09:00:00")]),
        (mr_body(head_pipeline=pipeline(created_at="2026-09-24T10:00:00")), [retarget("2026-09-24T09:00:00Z")]),
        (mr_body(head_pipeline=pipeline(created_at="not a time")), [retarget("2026-09-24T09:00:00Z")]),
    ]:
        proc, calls, payload = enable(before, notes=notes)
        assert proc.returncode == 3 and payload["reason"] == "retarget_time_unknown", (notes, payload)
        assert not puts(calls), calls

    # an unexpected payload is not an empty history
    for body in [{"message": "404 Not found"}, ["not a note"]]:
        proc, calls, payload = enable(mr_body(), notes={"json": body})
        assert proc.returncode == 3 and payload["reason"] == "retarget_history_incomplete", (body, payload)
        assert not puts(calls), calls

    # notes API failures are api_error, before any mutation
    for response in [fail("HTTP 500"), {"stdout": "<html>login</html>"}]:
        proc, calls, payload = enable(mr_body(), notes=response)
        assert proc.returncode == 4 and payload["result"] == "api_error", (response, payload)
        assert payload["put_attempted"] is False and calls == [MR_GET, NOTES_GET], (response, calls)


def test_enable_already_enabled_is_idempotent() -> None:
    for before in [
        mr_body(merge_when_pipeline_succeeds=True),
        # a server that reports only auto_merge
        {key: value for key, value in mr_body(auto_merge=True).items() if key != "merge_when_pipeline_succeeds"},
    ]:
        proc, calls, payload = enable(before)
        assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload
        assert payload["put_attempted"] is False and calls == [MR_GET, NOTES_GET], calls
    # an existing auto-merge never bypasses eligibility
    for overrides, reason in [
        ({"head_pipeline": pipeline(sha="b" * 40)}, "pipeline_head_mismatch"),
        ({"head_pipeline": pipeline(status="success")}, "pipeline_not_running"),
    ]:
        proc, calls, payload = enable(mr_body(merge_when_pipeline_succeeds=True, **overrides))
        assert proc.returncode == 3 and payload["reason"] == reason, (overrides, payload)
        assert not puts(calls), calls
    proc, calls, payload = enable(mr_body(merge_when_pipeline_succeeds=True), notes=[retarget("2026-09-24T10:05:00Z")])
    assert proc.returncode == 3 and payload["reason"] == "pipeline_before_retarget", payload


def test_enable_put_failure_never_retries() -> None:
    unchanged = mr_body()
    proc, calls, payload = enable(mr_body(), after=unchanged, put=fail("HTTP 502 Bad Gateway"))
    assert proc.returncode == 4 and payload["result"] == "api_error", payload
    assert payload["put_error"] == "HTTP 502 Bad Gateway" and payload["put_attempted"] is True, payload
    assert len(puts(calls)) == 1 and calls[-1] == MR_GET and len(calls) == 4, calls

    # the PUT timed out client-side but GitLab applied it: the re-read decides
    proc, calls, payload = enable(mr_body(), after=mr_body(merge_when_pipeline_succeeds=True), put=fail("glab timed out", 124))
    assert proc.returncode == 0 and payload["result"] == "delegated_auto_merge", payload
    assert payload["put_error"] == "glab timed out" and len(puts(calls)) == 1, (payload, calls)

    # the sha guard refused because the head moved: name the move
    proc, calls, payload = enable(mr_body(), after=mr_body(sha="c" * 40), put=fail("HTTP 409 SHA does not match HEAD"))
    assert proc.returncode == 3 and payload["result"] == "enable_not_confirmed", payload
    assert payload["reason"] == "head_changed" and len(puts(calls)) == 1, (payload, calls)


def test_enable_reports_merged_before_ci() -> None:
    def merged(head_pipeline: object) -> dict[str, object]:
        return mr_body(state="merged", merge_commit_sha="d" * 40, head_pipeline=head_pipeline)

    for after in [
        merged(pipeline()),
        merged(pipeline(status="failed")),
        merged(None),
        merged(pipeline(id=124, status="success")),
    ]:
        proc, calls, payload = enable(mr_body(), after=after)
        assert proc.returncode == 3 and payload["result"] == "merged_before_ci", (after, payload)
        assert len(puts(calls)) == 1, calls
    proc, calls, payload = enable(mr_body(), after=merged(pipeline(status="success")))
    assert proc.returncode == 0 and payload["result"] == "merged", payload
    assert payload["merge_commit"] == "d" * 40, payload
    # a green pipeline confirms nothing about a head or target it never ran on
    for changes, reason in [({"sha": "c" * 40}, "head_changed"), ({"target_branch": "release"}, "target_changed")]:
        after = merged(pipeline(status="success"))
        after.update(changes)
        proc, calls, payload = enable(mr_body(), after=after)
        assert proc.returncode == 3, (changes, proc.stderr or proc.stdout)
        assert payload["result"] == "merged_before_ci" and payload["reason"] == reason, (changes, payload)


def test_enable_requires_postread_confirmation() -> None:
    for after, reason in [
        (mr_body(), "auto_merge_not_enabled"),
        (mr_body(merge_when_pipeline_succeeds=True, sha="c" * 40), "head_changed"),
        (mr_body(merge_when_pipeline_succeeds=True, target_branch="release"), "target_changed"),
        (mr_body(merge_when_pipeline_succeeds=True, state="closed"), "state_changed"),
        (mr_body(merge_when_pipeline_succeeds=True, state="locked"), "state_changed"),
    ]:
        proc, calls, payload = enable(mr_body(), after=after)
        assert proc.returncode == 3, (after, proc.stderr or proc.stdout)
        assert payload["result"] == "enable_not_confirmed" and payload["reason"] == reason, (after, payload)
        assert len(puts(calls)) == 1 and len(calls) == 4, calls
    for reread in [fail("HTTP 503"), {"stdout": "<html>login</html>"}]:
        scenario = {"mr": [ok(mr_body()), reread], "notes": [ok([])], "put": [ok(mr_body(merge_when_pipeline_succeeds=True))]}
        proc, calls = run_scenario(scenario, "--enable")
        payload = result_of(proc)
        assert proc.returncode == 4 and payload["result"] == "api_error", payload
        assert payload["put_attempted"] is True and len(puts(calls)) == 1, (payload, calls)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ci_merge_delegate.py smoke tests passed")
