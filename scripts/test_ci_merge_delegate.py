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

            if "merge_requests/396" in path:
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


def assert_merged() -> None:
    proc = run_snapshot("merged")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "merged"
    assert payload["mr"] == 396
    assert payload["pipeline_id"] == 123
    assert payload["merge_commit"] == "def456"


def assert_failed_pipeline_includes_failed_jobs() -> None:
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


def assert_running_auto_merge_delegates_without_blocking() -> None:
    proc = run_snapshot("running_auto_merge")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["result"] == "delegated_auto_merge"
    assert payload["auto_merge"] is True
    assert payload["pipeline_status"] == "running"


if __name__ == "__main__":
    assert_merged()
    assert_failed_pipeline_includes_failed_jobs()
    assert_running_auto_merge_delegates_without_blocking()
    print("ci_merge_delegate.py smoke tests passed")
