"""Sphinx configuration for the 3-ID-C BITS instrument documentation.

Build locally:

    cd docs && make html

Output:

    docs/build/html/index.html

The API reference under ``docs/source/api/`` is generated at build time
by ``sphinx-autoapi`` from the source under ``../../src/id3c/`` and is
not committed to the repository.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

# -- Path setup --------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# Make the package importable for any extensions that import it (e.g. for
# version discovery).  sphinx-autoapi itself parses the source statically
# and does not need imports to succeed.
sys.path.insert(0, str(SRC))

# -- Project information -----------------------------------------------------

project = "3-ID-C BITS"
author = "APS / BCDA"
copyright = f"2014-{datetime.date.today().year}, APS"

# Pull version from package metadata if installed; otherwise fall back.
try:
    from importlib.metadata import version as _pkg_version

    release = _pkg_version("3idc-bits")
except Exception:  # pragma: no cover
    release = "0.0.0"
version = release

# -- General configuration ---------------------------------------------------

extensions = [
    # Core Sphinx
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",  # numpy/google docstring styles
    # Markdown + Jupyter notebook source support.
    # myst_nb extends myst_parser; do not list myst_parser separately.
    # See nb_execution_mode below for notebook-execution policy.
    "myst_nb",
    # Auto API reference, generated from src/id3c at build time
    "autoapi.extension",
    # Niceties
    "sphinx_copybutton",
    "sphinx_design",
    "sphinx_tabs.tabs",
]

# File types Sphinx will read.
#   .md     -- MyST Markdown (parser: myst-nb, which extends myst_parser)
#   .ipynb  -- Jupyter notebook (parser: myst-nb)
#   .rst    -- reStructuredText (parser: built-in)
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "myst-nb",
    ".ipynb": "myst-nb",
}

# Files / dirs Sphinx should ignore.
#
# Note: the Marp slide-deck source files under presentations/intro_*.md
# are excluded.  Sphinx would otherwise try to parse them as MyST
# Markdown and reject the Marp front-matter.  The presentations/index.md
# page (which IS a Sphinx page) links out to the HTML/PDF artifacts
# that CI builds from those sources.
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "presentations/intro_*.md",
    "**/.ipynb_checkpoints",
]

# Where the top of the toctree lives.
root_doc = "index"

# -- MyST (Markdown) configuration -------------------------------------------

myst_enable_extensions = [
    "colon_fence",   # ::: directives, sometimes friendlier than ```
    "deflist",       # term/definition lists
    "tasklist",      # GitHub-style [ ] / [x] checkboxes
    "linkify",       # auto-link bare URLs
    "attrs_inline",  # {.class #id} inline attributes
    "attrs_block",   # block-level attributes
    "dollarmath",    # $...$ and $$...$$ for inline / display math
]

# Don't auto-generate header anchors for h4+, keeps cross-references clean.
myst_heading_anchors = 3

# -- myst-nb (Jupyter notebooks) ---------------------------------------------

# Never execute notebooks at build time.  This repo is developed off-network;
# many of our notebooks call into EPICS-backed devices that will not connect
# in CI or on a developer laptop.  Notebooks are committed with their cached
# outputs and rendered as-is.  To refresh outputs, re-execute the notebook
# manually on the beamline workstation and commit the updated file.
nb_execution_mode = "off"

# -- sphinx-autoapi configuration --------------------------------------------

autoapi_type = "python"
autoapi_dirs = [str(SRC / "id3c")]
autoapi_root = "api"                       # output under docs/source/api/
autoapi_keep_files = False                 # don't litter the source tree
autoapi_add_toctree_entry = True           # autoapi adds its own toctree
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    "imported-members",
]
autoapi_python_class_content = "both"      # render both class and __init__ docstrings

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "ophyd": ("https://blueskyproject.io/ophyd/", None),
    # bluesky and apstools sites do not currently publish an
    # objects.inv at a stable URL.  Re-enable when known good:
    #   "bluesky": ("https://blueskyproject.io/bluesky/", None),
    #   "apstools": ("https://bcda-aps.github.io/apstools/main/", None),
}

# -- HTML theme --------------------------------------------------------------

html_theme = "pydata_sphinx_theme"
html_title = f"{project} {release}"
html_static_path = ["_static"]

html_theme_options = {
    "github_url": "https://github.com/BCDA-APS/3idc-bits",
    "use_edit_page_button": True,
    "navigation_with_keys": True,
    "show_toc_level": 2,
    "icon_links": [
        {
            "name": "Issue tracker",
            "url": "https://github.com/BCDA-APS/3idc-bits/issues",
            "icon": "fas fa-bug",
        },
    ],
}

html_context = {
    "github_user": "BCDA-APS",
    "github_repo": "3idc-bits",
    "github_version": "main",
    "doc_path": "docs/source",
}

# -- Copybutton --------------------------------------------------------------

# Strip prompts so users can paste cleanly.
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: "
copybutton_prompt_is_regexp = True

# -- Suppress noisy warnings -------------------------------------------------

# autoapi commonly emits these when it encounters re-exports or ophyd
# components whose docstrings live on the descriptor, not the attribute.
# autosummary.import_cycle covers the autosummary runtime-import warnings
# emitted when optional runtime deps (apsbits, pyepics) are absent at
# doc-build time (e.g. on a developer laptop without EPICS support).
suppress_warnings = [
    "autoapi.python_import_resolution",
    "autosummary",
]
