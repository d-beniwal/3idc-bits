# How to edit and build the documentation

The Sphinx source for this site lives in `docs/source/`.  Build output
goes to `docs/build/html/` (gitignored).

## Build locally

From the repository root:

```bash
cd docs
make html
```

That produces `docs/build/html/index.html`, which you can open in a
browser:

```bash
xdg-open docs/build/html/index.html      # Linux
open docs/build/html/index.html          # macOS
```

For a full rebuild (also clears the auto-generated `api/` tree):

```bash
make clean
make html
```

## Build dependencies

You need the `[doc]` optional dependencies installed:

```bash
pip install -e .[doc]      # docs-only
pip install -e .[all]      # docs + dev
```

This pulls in `sphinx`, `myst_parser`, `sphinx-autoapi`,
`sphinx-copybutton`, `sphinx-design`, `sphinx-tabs`,
`pydata-sphinx-theme`, and a few support libraries.

## Source format

Pages may be **Markdown** (`.md`, the default) or **reStructuredText**
(`.rst`).  Most new pages should be Markdown.  Inside `.md` files,
MyST extensions are enabled:

- `colon_fence` -- `:::{note}` ... `:::` as an alternative to
  triple-backtick directives.
- `deflist` -- term / definition lists.
- `tasklist` -- GitHub-style `- [ ]` and `- [x]`.
- `linkify` -- bare URLs auto-link.
- `attrs_inline` / `attrs_block` -- `{.class #id}` attributes.
- `dollarmath` -- `$inline$` and `$$display$$` LaTeX math.

## Where to add a new page

The docs follow the [Diátaxis](https://diataxis.fr/) structure:

- `tutorials/` -- learning-oriented walkthroughs.
- `how_to/` -- task-oriented recipes (this very page).
- `reference/` -- information-oriented lookup.
- `explanation/` -- understanding-oriented background.

Add your new `.md` file to the appropriate directory **and** add an
entry under the section's `toctree` in `<section>/index.md`.  Sphinx
will warn if a file is not reachable from any `toctree`.

## API reference

`docs/source/api/` is generated at build time by `sphinx-autoapi`
from `src/id3c/`.  Do not edit it; do not commit it.  If you add a
new Python module under `src/id3c/`, the next `make html` will pick
it up automatically.

To skip API generation while iterating on prose docs:

```bash
SPHINXOPTS="-D extensions=sphinx.ext.autodoc,myst_parser,sphinx_copybutton,sphinx_design,sphinx_tabs.tabs" make html
```

(Remove `autoapi.extension` from the extension list.  Useful when
prose changes alone are being previewed and you do not want the
~10 s API parsing step.)

## Cross-references

Markdown cross-references between pages use the standard MyST syntax:

```markdown
See [How to add a device](add_a_device.md) for details.
```

To reference a Sphinx target (e.g. a function in the auto-generated
API):

```markdown
See {func}`id3c.devices.laser_optics.LaserOptics.move_out`.
```

To reference a section heading on another page:

```markdown
See [the cross-walk table](../tutorials/spec_to_bluesky.md#command-cross-walk).
```

## Diagrams and images

Place image files under `docs/source/_static/`.  Reference them with
standard Markdown:

```markdown
![diagram](../_static/my_diagram.png)
```

For diagrams generated as code, use `sphinx-design`'s tabs and
`mermaid` (not currently enabled; add to `conf.py`'s `extensions`
list if you need it).

## Style guidelines

- New pages default to `.md`.  Use `.rst` only when a feature
  genuinely needs it.
- Keep code examples short and runnable.  If an example needs a
  device that does not exist yet, mark it clearly.
- Prefer the `RE(plan(...))` invocation pattern in all examples; see
  [AGENTS.md > Plan invocation
  pattern](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#plan-invocation-pattern).
- Use a single-line heading-1 title at the top of every page (`#
  Title`).  The pydata theme picks this up for the page <title>.

## CI

Pushes to `main` and pull requests trigger a docs build via
`.github/workflows/docs.yml`.  The build is **advisory** -- a
failure shows on the PR but does not block merging.  Successful
builds on `main` are deployed to the `gh-pages` branch.

## See also

- [Sphinx documentation](https://www.sphinx-doc.org/).
- [MyST Markdown reference](https://myst-parser.readthedocs.io/).
- [Diátaxis framework](https://diataxis.fr/) -- the four-quadrant
  doc structure used here.
