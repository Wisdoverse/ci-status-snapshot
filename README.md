# ci-status-snapshot

An agent skill for reading CI/PR/MR state cheaply. Coding agents burn tokens re-asking "is CI green yet?" once per model turn; this skill replaces that loop with three stdlib-only Python scripts:

- a one-shot snapshot that classifies a GitHub PR or GitLab MR as `ACTION`, `WAIT`, or `DONE`,
- a silent local watcher that polls without model turns and prints exactly one JSON event when the state actually changes,
- a GitLab auto-merge delegation snapshot that confirms the merge is server-side delegated, or names the blocker.

`SKILL.md` is the agent-facing contract that Codex and Claude Code load on every trigger; it stays lean on purpose. The deeper playbooks — `references/gitlab-triage.md` (failed/stuck GitLab jobs) and `references/merge-flow.md` (merge and auto-merge delegation) — are read on demand, only when their trigger condition is met. This README is for humans.

## The contract

| Conclusion | Means | Next move |
| --- | --- | --- |
| `ACTION` | A concrete blocker fixable now: failed check/pipeline, manual job, merge conflict, branch behind, changes requested. | Triage the failure, fix, push. |
| `WAIT` | Checks running, review/approval pending, merge gate still computing, or any state the classifier does not positively recognize. | Do other work; arm the watcher if the goal depends on the result. |
| `DONE` | Positive evidence: merged/closed, or all checks green with a mergeable state (`CLEAN`/`HAS_HOOKS` on GitHub, `detailed_merge_status: mergeable` on GitLab). | Report and stop reading remote state. |

`DONE` always requires positive evidence — an unknown or unrecognized provider state is `WAIT`, never `DONE`. A green pipeline alone is not `DONE` on GitLab: the merge gate decides — and a still-running pipeline stays `WAIT` even when that gate already says `mergeable`.

## Install

Copy or symlink this repository into the host's skills directory:

```bash
# Codex
ln -s "$PWD" ~/.codex/skills/ci-status-snapshot
# Claude Code
ln -s "$PWD" ~/.claude/skills/ci-status-snapshot
```

`agents/openai.yaml` is the Codex interface manifest (display name and default prompt).

## Requirements

- Python 3.9 or newer, standard library only — no `pip install`, no virtualenv.
- `git`, plus the provider CLI you use: [`gh`](https://cli.github.com/) for GitHub, [`glab`](https://gitlab.com/gitlab-org/cli) for GitLab, authenticated.

## Usage

```bash
# One-shot snapshot (provider auto-detected from the git remote)
python3 scripts/ci_status_snapshot.py --selector 123
python3 scripts/ci_status_snapshot.py --provider gitlab --selector 396 --json

# Silent watcher: one JSON event on the first decision-relevant change
# exit 0 = change, 2 = three consecutive errors, 3 = timeout backstop
python3 scripts/ci_state_watch.py --provider github --selector 123 --interval-seconds 30

# GitLab auto-merge delegation snapshot (never blocks)
# exit 0 = merged/delegated_auto_merge/waiting/no_pipeline_observed, 2 = pipeline
# needs triage, 3 = a human must act, 4 = api_error — always read `result`
python3 scripts/ci_merge_delegate.py --provider gitlab --selector 396 --json
```

The watcher requires `--selector`: without it `gh`/`glab` resolve "the PR of the current branch" on every poll, so a checkout mid-watch would silently retarget it.

## Tests

No test framework required; each file runs standalone and is also collectible by pytest.

```bash
python3 scripts/test_ci_status_snapshot.py
python3 scripts/test_ci_state_watch.py
python3 scripts/test_ci_merge_delegate.py

python3 -m pytest scripts -q
```

## Limitations

Provider auto-detection matches the literal strings `github`/`gitlab` in the `origin` URL, so self-hosted hosts need an explicit `--provider`. `ci_merge_delegate.py` talks to the host `glab` infers from the repo remote, so an MR URL selector pointing at a different host is not routed there — run it from a checkout of that project or pass `--project`. GitLab servers older than 15.6 have no `detailed_merge_status`, and the deprecated `merge_status` only proves the branches merge cleanly, so those MRs never reach `DONE` from a green pipeline; snapshot again after the merge, or upgrade the server.

## License

Apache-2.0. See [LICENSE](LICENSE).
