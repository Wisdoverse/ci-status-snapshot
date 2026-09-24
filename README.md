# ci-status-snapshot

An agent skill for reading CI/PR/MR state cheaply. Coding agents burn tokens re-asking "is CI green yet?" once per model turn; this skill replaces that loop with three stdlib-only Python scripts:

- a one-shot snapshot that classifies a GitHub PR or GitLab MR as `ACTION`, `WAIT`, or `DONE`,
- a silent local watcher that polls without model turns and prints exactly one JSON event when the state actually changes,
- a GitLab auto-merge delegation snapshot that confirms the merge is server-side delegated, or names the blocker — and, with `--enable`, turns auto-merge on under guards.

Everything lives under `skills/ci-status-snapshot/`: `SKILL.md` is the agent-facing contract that Codex and Claude Code load on every trigger, and it stays lean on purpose. The deeper playbooks — `references/gitlab-triage.md` (failed/stuck GitLab jobs) and `references/merge-flow.md` (merge and auto-merge delegation) — are read on demand, only when their trigger condition is met. This README is for humans.

## The contract

| Conclusion | Means | Next move |
| --- | --- | --- |
| `ACTION` | A concrete blocker fixable now: failed check/pipeline, manual job, merge conflict, branch behind, changes requested. | Triage the failure, fix, push. |
| `WAIT` | Checks running, review/approval pending, merge gate still computing, or any state the classifier does not positively recognize. | Do other work; arm the watcher if the goal depends on the result. |
| `DONE` | Positive evidence: merged/closed, or all checks green with a mergeable state (`CLEAN`/`HAS_HOOKS` on GitHub, `detailed_merge_status: mergeable` on GitLab). | Report and stop reading remote state. |

`DONE` always requires positive evidence — an unknown or unrecognized provider state is `WAIT`, never `DONE`. A green pipeline alone is not `DONE` on GitLab: the merge gate decides — and a still-running pipeline stays `WAIT` even when that gate already says `mergeable`.

Missing CI is `WAIT` on both providers: right after a push GitLab can report `mergeable` before it creates the head pipeline, and GitHub can report `CLEAN` before any check run registers. The snapshot waits (`no pipeline observed` / `no checks reported yet`) and the watcher follows it until CI appears. Projects that run no CI pass `--allow-no-pipeline` to get `DONE` back; a pipeline that exists but reports an empty or unrecognized status stays `WAIT` either way.

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

# Numeric GitLab IIDs use one REST MR request; branch and URL selectors retain `glab mr view` routing.

# Project without CI: a missing pipeline / zero checks is nothing to wait for
python3 scripts/ci_status_snapshot.py --provider github --selector 123 --allow-no-pipeline

# Silent watcher: one JSON event on the first decision-relevant change
# exit 0 = change, 2 = three consecutive errors, 3 = timeout backstop
python3 scripts/ci_state_watch.py --provider github --selector 123 --expected-head <full-head-sha> --interval-seconds 30

# GitLab auto-merge delegation snapshot (never blocks)
# exit 0 = merged/delegated_auto_merge/waiting/no_pipeline_observed, 2 = pipeline
# needs triage, 3 = a human must act, 4 = api_error — always read `result`
python3 scripts/ci_merge_delegate.py --provider gitlab --selector 396 --json

# Guarded enablement: at most one PUT for the MR's current head, only while its
# pipeline is running and postdates the newest target-branch change; one re-read
# decides the result (adds enable_refused / enable_not_confirmed /
# merged_before_ci at exit 3, each with a `reason` where it has one)
python3 scripts/ci_merge_delegate.py --provider gitlab --selector 396 --enable --json
```

The watcher requires `--selector`: without it `gh`/`glab` resolve "the PR of the current branch" on every poll, so a checkout mid-watch would silently retarget it. It stays silent when auto-merge is turned on (that is the delegation you asked for) and wakes when auto-merge is cancelled. It also wakes on a retarget and when a GitLab head pipeline appears, starts running, or is replaced — the moments `--enable` can become eligible or stale — while per-job progress stays silent.

`--enable` sends `merge_when_pipeline_succeeds=true` and `auto_merge=true` together with the head `sha`: a GitLab server ignores a parameter it does not recognise, and a merge PUT without one merges immediately. The `sha` guards the source head only; the final re-read reports a retarget it observes, but the notes check and the PUT are not atomic against a concurrent retarget. The result table and refusal reasons are in `skills/ci-status-snapshot/references/merge-flow.md`.

Pass the previous snapshot's full SHA as `--expected-head` to catch a push before
the watcher's first read, even if CI is still pending. Without a prior snapshot,
omit it. Error/timeout events include the target and a timestamped `last_snapshot`
when available; that is stale context, not fresh CI evidence. The default timeout
is a finite 7200 seconds. Existing watchers keep their original code until they
exit; updating the skill does not restart them.

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
