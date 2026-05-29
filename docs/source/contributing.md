# Contributing

How to make changes to the `id3c` BITS instrument: code, configuration,
or documentation.

## Conventions

The project conventions are documented once, in
[`AGENTS.md`](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md)
at the repository root.  These cover:

- The `RE(plan(...))` invocation pattern in user-facing examples.
- The `@plan` decorator on plans and plan stubs we author.
- The `InterlockedEpicsMotor` late-binding wiring pattern.
- The `mb_creator` per-axis `class:` trick for custom motor classes.
- Bluesky-session-only interlock scope (no EPICS-side protection).
- Documentation conventions (Diátaxis, `.md` default,
  autoapi-not-committed).
- Off-network reality on the development workstation.

Read `AGENTS.md` before making non-trivial changes.

## Developer setup

```bash
pip install -e .[all]
pre-commit install
```

The `[all]` extra pulls in development and documentation tools:
`ruff`, `pytest`, `pre-commit`, `sphinx`, `sphinx-autoapi`, and the
rest.

`pre-commit install` activates the local git hook so each `git
commit` runs `ruff` and `ruff-format` on staged files, auto-fixing
what it can.

### Opting out of pre-commit

The hook is a developer convenience, not a requirement.  Several
ways to opt out:

- **Never enable it.**  If you have not run `pre-commit install`,
  nothing happens at commit time.
- **Disable it for this clone:** `pre-commit uninstall`.
- **Skip the toolchain entirely:** install with `pip install -e .`
  (no `[all]`); `pre-commit` will not be available locally.
- **Skip a single commit:** `git commit --no-verify`.

CI runs `pre-commit run --all-files` on every push and PR, but the
lint job is **advisory** -- it does not block merging or fail the
build.  Mis-formatted code can reach `main` without CI complaint.

## Style

- `ruff` + `ruff-format`, configured in `pyproject.toml`.
- The `D100`-`D107` docstring rules are enabled: every public
  module, class, and function (including `__init__`) needs a
  docstring.
- Line length: 88 (ruff-format default).
- Imports: one per line (`force_single_line = true`).

## Tests

```bash
pytest
```

CI runs the same.  The repository's tests are intentionally limited
to things that work without live EPICS access -- import sanity,
class construction, attribute wiring.  See
[`AGENTS.md` > Off-network
reality](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#off-network-reality).

## Commits and pushes

Commit when your change is locally green (tests pass, pre-commit
passes if you have it enabled).

Do **not** push or open pull requests without explicit request from
the maintainer.  This is a deployed beamline instrument; reviewers'
attention should be requested deliberately, not by reflex.

## Documentation

If your change affects user-visible behaviour or adds a new device /
plan / configuration option, update the docs at the same time.  See
[How to edit and build docs](how_to/edit_and_build_docs.md).

Doc builds run in CI (`.github/workflows/docs.yml`); on push to
`main`, the built HTML is deployed to the `gh-pages` branch.  Like
the lint job, the docs build is **advisory** -- failures show but do
not block.

### Slide decks (Marp)

The presentations under `docs/source/presentations/intro_*.md` are
written for [Marp](https://marp.app/) and rendered to HTML + PDF
by the same CI workflow.  Marp is a **Node.js / npm** tool, not a
Python package; it cannot live in `pyproject.toml`.  You only need
it if you want to *render* the decks locally; *editing* them is
plain Markdown editing.

Render locally with `npx` (no install):

```bash
cd docs/source/presentations
npx @marp-team/marp-cli intro_standard.md -o intro_standard.html
```

Or install the [Marp for VS Code](https://marketplace.visualstudio.com/items?itemName=marp-team.marp-vscode)
extension for live preview.

See [Presentations](presentations/index.md) for the audience and
length of each deck.

## Reporting issues

Issue tracker: <https://github.com/BCDA-APS/3idc-bits/issues>.

For bugs, include:

- Bluesky session log (the INFO/WARNING/ERROR lines around the
  failure).
- Output of `device.summary()` for any device involved.
- The exact plan or command that triggered it.
- The final exception line (the bottom of the traceback is the
  diagnosis; the rest is usually framework internals).
