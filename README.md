# ci-status-snapshot

An agent skill for reading CI/PR/MR state cheaply. Coding agents burn tokens re-asking "is CI green yet?" once per model turn; this skill replaces that loop with three stdlib-only Python scripts:

- a one-shot snapshot that classifies a GitHub PR or GitLab MR as `ACTION`, `WAIT`, or `DONE`,
- a silent local watcher that polls without model turns and prints exactly one JSON event when the state actually changes,
- a GitLab auto-merge delegation snapshot that confirms the merge is server-side delegated, or names the blocker.

Everything lives under `skills/ci-status-snapshot/`: `SKILL.md` is the agent-facing contract that Codex and Claude Code load on every trigger, and it stays lean on purpose. The deeper playbooks — `references/gitlab-triage.md` (failed/stuck GitLab jobs) and `references/merge-flow.md` (merge and auto-merge delegation) — are read on demand, only when their trigger condition is met. This README is for humans.

## The contract

| Conclusion | Means | Next move |
| --- | --- | --- |
| `ACTION` | A concrete blocker fixable now: failed check/pipeline, manual job, merge conflict, branch behind, changes requested. | Triage the failure, fix, push. |
| `WAIT` | Checks running, review/approval pending, merge gate still computing, or any state the classifier does not positively recognize. | Do other work; arm the watcher if the goal depends on the result. |
| `DONE` | Positive evidence: merged/closed, or all checks green with a mergeable state (`CLEAN`/`HAS_HOOKS` on GitHub, `detailed_merge_status: mergeable` on GitLab). | Report and stop reading remote state. |

`DONE` always requires positive evidence — an unknown or unrecognized provider state is `WAIT`, never `DONE`. A green pipeline alone is not `DONE` on GitLab: the merge gate decides — and a still-running pipeline stays `WAIT` even when that gate already says `mergeable`.

## Install

The repository is its own plugin marketplace, so both hosts install it in two commands.

### Claude Code

```
/plugin marketplace add Wisdoverse/ci-status-snapshot
```
```
/plugin install ci-status-snapshot@ci-status-snapshot
```

(Send the two `/plugin` commands as separate prompts.)

### Codex

```bash
codex plugin marketplace add https://github.com/Wisdoverse/ci-status-snapshot.git
codex plugin add ci-status-snapshot@ci-status-snapshot
```

Start a new thread afterwards so the skill is picked up. There are no hooks to trust — the plugin ships instructions and three stdlib-only scripts, nothing that runs on its own.

If `marketplace add` reports that `ci-status-snapshot` is already added from a different source, the local Codex registration is stale or was created with another spelling of the same Git URL. Remove only that marketplace entry, then recreate it with the canonical URL:

```bash
codex plugin marketplace remove ci-status-snapshot
codex plugin marketplace add https://github.com/Wisdoverse/ci-status-snapshot.git
codex plugin add ci-status-snapshot@ci-status-snapshot
```

Do not remove the marketplace for other installation errors; inspect the reported error first.

### Uninstall

| Host | Command |
| --- | --- |
| Claude Code | `/plugin uninstall ci-status-snapshot@ci-status-snapshot` |
| Codex | `codex plugin remove ci-status-snapshot@ci-status-snapshot` |

### Manual copy (fallback)

Without the plugin system, copy or symlink the skill directory into the host's skills directory:

```bash
# Codex
ln -s "$PWD/skills/ci-status-snapshot" ~/.codex/skills/ci-status-snapshot
# Claude Code
ln -s "$PWD/skills/ci-status-snapshot" ~/.claude/skills/ci-status-snapshot
```

Do not keep a manual copy and the plugin install side by side — the skill would register twice.

## Requirements

- Python 3.9 or newer, standard library only — no `pip install`, no virtualenv.
- `git`, plus the provider CLI you use: [`gh`](https://cli.github.com/) for GitHub, [`glab`](https://gitlab.com/gitlab-org/cli) for GitLab, authenticated.

## Usage

Paths below are relative to the repo; installed as a plugin, use the skill directory the host reports.

```bash
cd skills/ci-status-snapshot

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
python3 skills/ci-status-snapshot/scripts/test_ci_status_snapshot.py
python3 skills/ci-status-snapshot/scripts/test_ci_state_watch.py
python3 skills/ci-status-snapshot/scripts/test_ci_merge_delegate.py

python3 -m pytest skills/ci-status-snapshot/scripts -q
```

## Limitations

Provider auto-detection matches the literal strings `github`/`gitlab` in the `origin` URL, so self-hosted hosts need an explicit `--provider`. `ci_merge_delegate.py` talks to the host `glab` infers from the repo remote and never passes `--hostname`, so an MR URL selector pointing at a different host is not routed there: `--project` only changes the project path, which can read the same IID off the wrong instance. Run the helper from a checkout of that host's project. GitLab servers older than 15.6 have no `detailed_merge_status`, and the deprecated `merge_status` only proves the branches merge cleanly, so those MRs never reach `DONE` from a green pipeline; snapshot again after the merge, or upgrade the server.

## License

Apache-2.0. See [LICENSE](LICENSE).
