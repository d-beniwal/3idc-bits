# Git workflow for this repository

## Repository topology

This local clone is a fork-based contribution setup:

| Remote     | URL                                          | Role                          |
|------------|----------------------------------------------|-------------------------------|
| `origin`   | https://github.com/d-beniwal/3idc-bits.git   | Your fork (push here)         |
| `upstream` | https://github.com/BCDA-APS/3idc-bits.git    | Canonical main repo (PR here) |

Contribution direction:

```
local working tree  --push-->  origin (fork)  --pull request-->  upstream (BCDA-APS)
```

## Branch strategy

- `main` — a clean mirror of `upstream/main`. Never commit feature work or
  the `claude/` folder here beyond the one-line `.gitignore` entry.
- `feature/*` — one branch per change, cut from an up-to-date `main`.
  These become pull requests to `upstream`.
- `claude-context` — dedicated branch that carries the git-ignored `claude/`
  folder. Pushed to `origin` only; never PR'd to `upstream`.

## Keeping `main` in sync with upstream

```bash
git checkout main
git fetch upstream
git merge --ff-only upstream/main   # or: git rebase upstream/main
git push origin main                # keep the fork's main current
```

## Making a change (PR to upstream)

```bash
git checkout main
git pull --ff-only upstream main
git checkout -b feature/<short-name>
# ... edit, then ...
git add -A                          # claude/ is ignored, won't be staged
git commit -m "TYPE: concise message"
git push -u origin feature/<short-name>
# open a PR on GitHub from d-beniwal:feature/<short-name> -> BCDA-APS:main
```

Because `claude/` is git-ignored on every branch derived from `main`, it
cannot leak into these PRs.

## Gotcha: the folder is deleted when you leave `claude-context`

Because `claude/` is **tracked** on `claude-context` but ignored elsewhere,
switching *from* `claude-context` back to `main` makes git delete the folder
from the working tree. Restore it (worktree only, so it stays unstaged and
ignored) with:

```bash
git restore --source=origin/claude-context --worktree -- claude/
```

Once restored this way it is untracked+ignored, so it survives ordinary
`main` <-> `feature/*` switches. It is only clobbered again by visiting
`claude-context` and returning — so re-run the restore after each sync.

## Syncing the claude-context branch (updating this folder)

The `claude/` folder is ignored everywhere, so it must be force-added on the
dedicated branch:

```bash
git checkout claude-context
git add -f claude/                  # -f overrides .gitignore
git commit -m "claude: update repo-management context"
git push origin claude-context
git checkout main
git restore --source=origin/claude-context --worktree -- claude/  # bring folder back
```

To populate this folder on a fresh clone (without switching branches):

```bash
git clone https://github.com/d-beniwal/3idc-bits.git
cd 3idc-bits
git fetch origin claude-context
git restore --source=origin/claude-context --worktree -- claude/
```

## Commit message convention

Upstream uses short type prefixes seen in history: `FIX:`, `DOC:`, `ENH:`,
`MNT:`, etc. For `claude/` folder commits, prefix with `claude:` — these
never reach upstream so they only need to be clear to you.
