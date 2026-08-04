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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ci_merge_delegate.py smoke tests passed")
