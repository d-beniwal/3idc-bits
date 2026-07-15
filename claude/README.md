# Claude Code context — `claude/` folder

This folder holds persistent context for Claude Code (and any human) about
**how this repository is managed**. It is *not* part of the `id3c` package
and must never reach the upstream (`BCDA-APS/3idc-bits`) main repo.

## Why this folder exists

- Keep a durable, version-controlled record of the fork/PR workflow so it
  survives fresh clones and machine changes.
- Give Claude Code immediate grounding on the repo's git topology and
  conventions at the start of every session.

## How it is stored (important)

This folder is deliberately **git-ignored on `main` and all feature
branches** (see the `claude/` entry in `.gitignore`). It is committed and
pushed **only** on a dedicated fork branch:

```
claude-context   <- holds this folder; pushed to origin (fork) only
```

Because feature branches are cut from a clean `main`, the `claude/` folder
never appears in a diff sent to upstream via pull request.

The folder stays present in your working tree across branch switches because
git leaves ignored files in place — so it is always available to Claude,
regardless of which branch is checked out.

## Files here

- `README.md`      — this file.
- `workflow.md`    — git remotes, branch strategy, PR flow, sync commands.
- `repo-context.md`— what this repo is and its conventions (for quick grounding).

## Maintaining this folder

See `workflow.md` → "Syncing the claude-context branch" for the exact
commands to commit updates to this folder and push them to your fork.
