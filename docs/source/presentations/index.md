# Presentations

Slide decks introducing the 3-ID-C BITS instrument, written in
Markdown for [Marp](https://marp.app/) and built to HTML + PDF by
CI.  The Markdown sources are committed; HTML/PDF artifacts are
generated and deployed alongside the rest of the docs.

Three decks at increasing length and depth.  Pick the one that
matches your time budget.

| Deck | Slides | Time | Audience |
|------|--------|------|----------|
| [Terse](https://bcda-aps.github.io/3idc-bits/presentations/intro_terse.html) | 6 | 5 min | Anyone who needs to know "what is this?" right now |
| [Standard](https://bcda-aps.github.io/3idc-bits/presentations/intro_standard.html) | 12 | 15 min | Team intro at a beamline meeting; the default choice |
| [Tutorial](https://bcda-aps.github.io/3idc-bits/presentations/intro_tutorial.html) | 25 | 45-60 min | Self-paced; covers SPEC->Bluesky, EPICS->ophyd, interlocks, and what's not yet implemented |

PDF versions are at the same URLs with `.pdf` instead of `.html`:

- [intro_terse.pdf](https://bcda-aps.github.io/3idc-bits/presentations/intro_terse.pdf)
- [intro_standard.pdf](https://bcda-aps.github.io/3idc-bits/presentations/intro_standard.pdf)
- [intro_tutorial.pdf](https://bcda-aps.github.io/3idc-bits/presentations/intro_tutorial.pdf)

## Markdown sources

If GitHub Pages is not yet available, or you want to read the
sources directly:

- [intro_terse.md](https://github.com/BCDA-APS/3idc-bits/blob/main/docs/source/presentations/intro_terse.md)
- [intro_standard.md](https://github.com/BCDA-APS/3idc-bits/blob/main/docs/source/presentations/intro_standard.md)
- [intro_tutorial.md](https://github.com/BCDA-APS/3idc-bits/blob/main/docs/source/presentations/intro_tutorial.md)

The Markdown is human-readable as a fallback -- the slide breaks
are `---` lines and the rest is plain Markdown.

## Building locally

Marp is a **Node.js / npm** package, not a Python package.  Two
ways to render the sources locally without installing anything
permanently:

```bash
# One-shot HTML (uses npx):
cd docs/source/presentations
npx @marp-team/marp-cli intro_standard.md -o intro_standard.html

# One-shot PDF:
npx @marp-team/marp-cli intro_standard.md --pdf -o intro_standard.pdf
```

To preview while editing, install the
[Marp for VS Code](https://marketplace.visualstudio.com/items?itemName=marp-team.marp-vscode)
extension; it gives a live side-by-side preview.

See the [Marp CLI documentation](https://github.com/marp-team/marp-cli)
for additional options (themes, watch mode, server mode, PowerPoint
export, etc.).

## Editing

The slide format is plain Markdown with:

- Front-matter at the top (`marp: true`, `theme: default`, etc.)
- Slide breaks: `---` on its own line
- Standard Markdown elsewhere (tables, code blocks, images)

See the [Marp Markdown
reference](https://marpit.marp.app/markdown).

Each push to `main` triggers a CI rebuild via
`.github/workflows/docs.yml`; the regenerated HTML and PDF
artifacts land at the URLs in the table above.
