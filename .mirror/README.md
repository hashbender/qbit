# qbit upstream mirror

Mirrors PRs from **Qbit-Org/qbit** (upstream) into this fork so the same GitHub
Actions fire on the fork's **tenki** runners — real-world load testing.

## How it works

`mirror.py` reconstructs each upstream base branch on the fork and replays every
upstream PR: it overlays exactly the files the PR changed (from the PR head),
rewrites every workflow `runs-on:` to the tenki pools, opens a PR, then
merges / closes / leaves it open to match the upstream PR's state. Each mirrored
PR body carries a `Mirror-of: Qbit-Org/qbit#<n>` marker, so re-runs are
idempotent (skip already-mirrored PRs; sync open→merged/closed transitions).

Runner-label mapping (taken from the original main migration):

| upstream `runs-on` | tenki |
|---|---|
| `ubuntu-*`, `macos-*`, `windows-*`, `blacksmith-*` | `tenki-standard-medium-4c-8g` |
| `[self-hosted, …]`, `${{ … qbit-trusted-ci … }}` | `tenki-standard-large-plus-16c-32g` |

Base start points are chosen so code and workflows stay coherent (otherwise
`ci.yml` dies at the `classify` job): historical when a replayed PR introduces
the CI infrastructure (e.g. the release PR on `main`), otherwise the current
upstream tip (e.g. `0.1.x`).

## Ongoing watcher

`.github/workflows/zzz-mirror-watch.yml` runs `mirror.py --execute` every 30
minutes (and on demand via *Run workflow*).

### One-time setup: the `MIRROR_PAT` secret

The watcher must authenticate with a **Personal Access Token**, not the default
`GITHUB_TOKEN` — pushes/PRs made with `GITHUB_TOKEN` do **not** trigger other
workflows, which would defeat the mirror.

1. Create a token (classic: scopes `repo` + `workflow`; or fine-grained on
   `hashbender/qbit` with **Contents: RW**, **Pull requests: RW**,
   **Workflows: RW**).
2. Add it as a repo secret named `MIRROR_PAT`:
   `gh secret set MIRROR_PAT --repo hashbender/qbit`

## Manual use

```bash
# initial backfill (force-resets base branches, replays all PRs):
python3 mirror.py --repo-dir <clone> --execute --reset-bases --merge-wait

# continue / watch (no reset, only new PRs):
python3 mirror.py --repo-dir <clone> --execute

# preview without touching the fork:
python3 mirror.py --repo-dir <clone>
```

The `<clone>` must have remotes `origin` → Qbit-Org/qbit and `fork` →
hashbender/qbit.
