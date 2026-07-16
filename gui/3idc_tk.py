#!/usr/bin/env python3
"""3-ID-C Bluesky Plan Runner GUI (docstring-driven).

File browser (``src/id3c/user/``), plan dropdown, parameter form, and a
generated two-line command ready to paste::

    from id3c.user.s3idc_plans.setup_june_26 import omega_fly
    RE(omega_fly(file_name='test', ...))

Every parameter form is built directly from each plan's **docstring +
signature** (no hardcoded table), so any new ``@plan`` is picked up
automatically.  Nothing is imported -- the plan file is read with the ``ast``
module only (importing would pull in ``oregistry``/hardware).

Docstring grammar every plan must follow (see the plan module docstrings)::

    Parameters
    ----------
    <name> : <dtype>[ [<units>]]
        <short name> :: <long description>

* dtype in {str, int, float, bool, choice{a, b, ...}, positions}
* units optional, e.g. [deg], [mm], [s], [1/deg]
* body split on the first ' :: ' -> short label / long tooltip
* default + required come from the signature (no default => required;
  a None default => optional, blank omits the argument)
* args not listed in Parameters (e.g. md) are hidden

Usage (from the repo root):
    python gui/3idc_tk.py

Requirements: Python 3.9+ tkinter (stdlib).
Clipboard: xclip or xsel on Linux.
"""

import ast
import os
import re
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from collections import namedtuple
from tkinter import ttk

# ── Paths ──────────────────────────────────────────────────────────────────────
# This GUI lives at <repo>/gui/.  It scans ONLY <repo>/src/id3c/user for plans.

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.normpath(os.path.join(_GUI_DIR, "..", "src"))
USER_DIR = os.path.normpath(os.path.join(SRC_DIR, "id3c", "user"))

# Startup script lives at the repo root (next to gui/).
_STARTUP_SCRIPT = os.path.normpath(
    os.path.join(_GUI_DIR, "..", "start_3idc_bluesky.sh")
)

# File in USER_DIR checked by default on startup.
_DEFAULT_PLAN_FILE = "setup_june_26.py"

# Default working directory used by the Launch-Bluesky button.
_DEFAULT_LAUNCH_DIR = "/home/beams/S3BLUE"


# ── Docstring / signature parser (AST only — never imports the plan module) ────

# One parsed argument.  default/required/blank_omits come from the SIGNATURE;
# dtype/units/short/long come from the DOCSTRING.
ParamSpec = namedtuple(
    "ParamSpec",
    "name dtype units short long default required choices blank_omits",
)

_NODEFAULT = object()  # sentinel: signature arg with no default (=> required)

_KNOWN_DTYPES = {"str", "int", "float", "bool", "choice", "positions"}


def _literal(node: ast.AST):
    """Best-effort literal value of a default node (no code execution)."""
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        try:
            return ast.unparse(node)  # py3.9+
        except Exception:  # noqa: BLE001
            return None


def _has_plan_decorator(node) -> bool:
    for dec in node.decorator_list:
        if (isinstance(dec, ast.Name) and dec.id == "plan") or (
            isinstance(dec, ast.Attribute) and dec.attr == "plan"
        ):
            return True
    return False


def _signature(node) -> list[tuple[str, object]]:
    """Ordered (name, default-or-_NODEFAULT) for every argument of `node`."""
    a = node.args
    out: list[tuple[str, object]] = []

    positional = list(getattr(a, "posonlyargs", [])) + list(a.args)
    defaults = list(a.defaults)
    n, nd = len(positional), len(defaults)
    for i, arg in enumerate(positional):
        if i >= n - nd:
            out.append((arg.arg, _literal(defaults[i - (n - nd)])))
        else:
            out.append((arg.arg, _NODEFAULT))

    for arg, dnode in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        out.append((arg.arg, _NODEFAULT if dnode is None else _literal(dnode)))

    return out


def _first_paragraph(doc: str) -> str:
    """Docstring summary: first paragraph, whitespace-collapsed."""
    lines: list[str] = []
    for line in doc.strip().splitlines():
        if not line.strip():
            break
        lines.append(line.strip())
    return " ".join(lines)


def _parse_parameters(doc: str) -> dict[str, dict]:
    """Parse the NumPy ``Parameters`` section into {name: {typespec, body}}.

    Returns {} when the docstring has no Parameters section.
    """
    lines = doc.splitlines()

    # locate the "Parameters" title + dashed underline
    start = None
    for i in range(len(lines) - 1):
        under = lines[i + 1].strip()
        if lines[i].strip() == "Parameters" and under and set(under) == {"-"}:
            start = i + 2
            break
    if start is None:
        return {}

    # collect body lines until the next section (dashed header) or an
    # ``Example::``-style block at column 0
    body: list[str] = []
    for j in range(start, len(lines)):
        line = lines[j]
        nxt = lines[j + 1].strip() if j + 1 < len(lines) else ""
        if line.strip() and nxt and set(nxt) == {"-"}:
            break  # this line is the title of the next section
        if line and not line[0].isspace() and line.strip().endswith("::"):
            break  # e.g. "Example::"
        body.append(line)

    # split body into per-argument entries (header at col 0, body indented)
    entries: dict[str, dict] = {}
    cur: dict | None = None
    for line in body:
        if line and not line[0].isspace():
            m = re.match(r"^(\w+)\s*:\s*(.+?)\s*$", line)
            if m:
                cur = {"typespec": m.group(2), "body": []}
                entries[m.group(1)] = cur
            else:
                cur = None  # a col-0 line that is not "name : type"
        elif cur is not None and line.strip():
            cur["body"].append(line.strip())
    return entries


def _parse_typespec(typespec: str) -> tuple[str, str, list[str]]:
    """'float [deg]' -> ('float', 'deg', []);  'choice{a, b}' -> ('choice','',[a,b])."""
    units = ""
    m = re.search(r"\[([^\]]*)\]\s*$", typespec)
    if m:
        units = m.group(1).strip()
        typespec = typespec[: m.start()].strip()

    dtype = typespec.strip()
    choices: list[str] = []
    cm = re.match(r"choice\s*\{(.*)\}$", dtype)
    if cm:
        choices = [c.strip() for c in cm.group(1).split(",") if c.strip()]
        dtype = "choice"
    return dtype, units, choices


def _parse_body(body_lines: list[str]) -> tuple[str, str]:
    """Join body lines, split on the first ' :: ' into (short, long)."""
    text = " ".join(body_lines).strip()
    if "::" in text:
        short, long = text.split("::", 1)
        return short.strip(), long.strip()
    return text, ""


def find_plan_specs(filepath: str) -> dict[str, dict]:
    """AST-parse a .py file; return {plan_name: {summary, params, documented}}.

    ``params`` is an ordered list of :class:`ParamSpec` (signature order,
    documented args only).  Never imports the module.
    """
    try:
        with open(filepath, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
    except (SyntaxError, OSError):
        return {}

    specs: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _has_plan_decorator(node):
            continue

        doc = ast.get_docstring(node) or ""
        doc_meta = _parse_parameters(doc)

        params: list[ParamSpec] = []
        for name, default in _signature(node):
            if name not in doc_meta:
                continue  # undocumented (e.g. md) => hidden
            dtype, units, choices = _parse_typespec(doc_meta[name]["typespec"])
            short, long = _parse_body(doc_meta[name]["body"])
            required = default is _NODEFAULT
            blank_omits = (not required) and default is None
            params.append(
                ParamSpec(
                    name=name,
                    dtype=dtype,
                    units=units,
                    short=short or name,
                    long=long,
                    default=default,
                    required=required,
                    choices=choices,
                    blank_omits=blank_omits,
                )
            )

        specs[node.name] = {
            "summary": _first_paragraph(doc),
            "params": params,
            "documented": bool(doc_meta),
        }
    return specs


# ── File-browser utilities ────────────────────────────────────────────────────


def file_to_module(filepath: str) -> str:
    """Module path for the pasted import line (relative to src/, so a nested
    subpackage like id3c.user.s3idc_plans.setup_june_26 resolves correctly)."""
    rel = os.path.relpath(filepath, SRC_DIR)
    return rel.replace(os.sep, ".").removesuffix(".py")


def scan_user_dir(user_dir: str) -> list[tuple]:
    """Shallow scan; returns (display_name, kind, abs_path, indent_px)."""
    rows: list[tuple] = []
    try:
        entries = sorted(
            os.scandir(user_dir),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except OSError:
        return rows
    for entry in entries:
        if entry.name.startswith("__"):
            continue
        if entry.is_dir():
            rows.append((entry.name + "/", "dir", entry.path, 4))
            try:
                for sub in sorted(os.scandir(entry.path), key=lambda e: e.name.lower()):
                    if (
                        sub.is_file()
                        and sub.name.endswith(".py")
                        and not sub.name.startswith("__")
                    ):
                        rows.append((sub.name, "file", sub.path, 20))
            except OSError:
                pass
        elif entry.is_file() and entry.name.endswith(".py"):
            rows.append((entry.name, "file", entry.path, 4))
    return rows


# ── Tooltip (hover help for the long description) ──────────────────────────────


class _Tooltip:
    """Lightweight hover tooltip for a widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event: tk.Event) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 24
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#ffffe0",
            foreground="#000000",
            relief="solid",
            borderwidth=1,
            wraplength=400,
            padx=6,
            pady=4,
        ).pack()

    def _hide(self, _event: tk.Event) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ── Application ───────────────────────────────────────────────────────────────


class BlueSkyPlanGUI:
    """Main window: file browser, plan dropdown, parameter form, command output."""

    def __init__(self, root: tk.Tk) -> None:
        """Build the UI and populate the file browser + plan dropdown."""
        self.root = root
        self.root.title("3-ID-C Bluesky Plan Runner (docstring-driven)")
        self.root.geometry("1060x860")
        self.root.minsize(780, 500)

        self._setup_styles()
        self._file_vars: dict[str, tk.BooleanVar] = {}
        self._plan_origins: dict[str, str] = {}
        self._plan_specs: dict[str, dict] = {}
        self._plan_list: list[str] = []
        self._param_widgets: dict[str, tuple] = {}
        self._current_params: list[ParamSpec] = []

        # Named font objects — updating them redisplays every widget that uses them
        self._fnt_mono = tkfont.Font(family="Monospace", size=15)
        self._fnt_bold = tkfont.Font(family="TkDefaultFont", size=15, weight="bold")
        self._size_var = tk.IntVar(value=15)
        self._apply_font_size(15)  # also sync system fonts used by ttk widgets

        self._build_ui()
        self._populate_file_browser()
        self._refresh_plan_dropdown(preserve_selection=False)

    def _setup_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

    def _apply_font_size(self, size: int | float) -> None:
        """Update all fonts to `size` and force a full geometry pass."""
        size = max(8, min(28, int(float(size))))
        self._size_var.set(size)

        # 1. Named Font objects — tk.Text widgets that hold these resize automatically.
        self._fnt_mono.configure(size=size)
        self._fnt_bold.configure(size=size, weight="bold")

        # 2. System fonts — basic tk widgets pick these up immediately.
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkFixedFont",
            "TkHeadingFont",
            "TkSmallCaptionFont",
        ):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass

        # 3. ttk style engine — ttk caches font metrics per-style; without this
        #    explicit update, ttk Entry/Combobox/Label heights stay fixed even
        #    after the system font changes.
        style = ttk.Style()
        fspec = ("TkDefaultFont", size)
        for s in (
            ".",
            "TLabel",
            "TButton",
            "TEntry",
            "TCombobox",
            "TCheckbutton",
            "TLabelframe",
            "TLabelframe.Label",
            "TMenubutton",
            "TSpinbox",
        ):
            try:
                style.configure(s, font=fspec)
            except tk.TclError:
                pass

        # 4. Process all pending geometry events so every widget reflows.
        self.root.update_idletasks()

        # 5. The parameter-form canvas scroll region may have grown/shrunk.
        if hasattr(self, "_param_canvas"):
            self._param_canvas.configure(scrollregion=self._param_canvas.bbox("all"))

    # ── Top-level layout ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Top bar: font-size slider + Bluesky launcher ──────────────────────
        top_bar = ttk.Frame(self.root, padding=(8, 4))
        top_bar.pack(fill="x")

        # Font-size controls (left side)
        ttk.Label(top_bar, text="Font size:").pack(side="left")
        ttk.Scale(
            top_bar,
            from_=8,
            to=28,
            orient="horizontal",
            length=180,
            variable=self._size_var,
            command=self._apply_font_size,
        ).pack(side="left", padx=(4, 2))
        ttk.Label(top_bar, textvariable=self._size_var, width=3).pack(side="left")

        ttk.Separator(top_bar, orient="vertical").pack(
            side="left", fill="y", padx=12, pady=2
        )

        # Launch-Bluesky controls (right of separator)
        ttk.Label(top_bar, text="Work dir:").pack(side="left")
        self._launch_dir_var = tk.StringVar(value=_DEFAULT_LAUNCH_DIR)
        ttk.Entry(top_bar, textvariable=self._launch_dir_var, width=32).pack(
            side="left", padx=(4, 6)
        )
        self._launch_btn = ttk.Button(
            top_bar, text="▶  Launch Bluesky", command=self._launch_bluesky
        )
        self._launch_btn.pack(side="left", padx=(0, 4))
        self._launch_status_lbl = ttk.Label(top_bar, text="", foreground="#555")
        self._launch_status_lbl.pack(side="left", padx=4)

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        hpaned = ttk.PanedWindow(self.root, orient="horizontal")
        hpaned.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(hpaned)
        hpaned.add(left, weight=0)
        self._build_file_browser(left)

        right = ttk.Frame(hpaned)
        hpaned.add(right, weight=1)
        self._build_plan_ui(right)

    # ── Launch-Bluesky button ─────────────────────────────────────────────────

    def _launch_bluesky(self) -> None:
        """Create the work directory if needed, then run start_3idc_bluesky.sh there."""
        work_dir = self._launch_dir_var.get().strip() or _DEFAULT_LAUNCH_DIR

        # Validate / create the directory.
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as exc:
            self._set_launch_status(f"Cannot create {work_dir}: {exc}", error=True)
            return

        if not os.path.isfile(_STARTUP_SCRIPT):
            self._set_launch_status(f"Script not found: {_STARTUP_SCRIPT}", error=True)
            return

        # Try common terminal emulators in preference order.
        # Each entry is the argv to pass subprocess.Popen.
        # The script is run with bash so it works even if the execute bit
        # is missing on the workstation.
        terminals = [
            [
                "gnome-terminal",
                "--working-directory",
                work_dir,
                "--",
                "bash",
                _STARTUP_SCRIPT,
            ],
            ["xterm", "-e", f"cd {work_dir!r} && bash {_STARTUP_SCRIPT!r}"],
            ["konsole", "--workdir", work_dir, "-e", "bash", _STARTUP_SCRIPT],
            [
                "xfce4-terminal",
                "--working-directory",
                work_dir,
                "-e",
                f"bash {_STARTUP_SCRIPT!r}",
            ],
        ]

        launched = False
        for argv in terminals:
            try:
                subprocess.Popen(argv, cwd=work_dir)
                launched = True
                break
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._set_launch_status(f"Launch error: {exc}", error=True)
                return

        if launched:
            self._set_launch_status(f"Launched in {work_dir}", error=False)
        else:
            # Fall back: run the script in the background without a terminal
            # window.  Useful for headless / SSH sessions.
            try:
                subprocess.Popen(
                    ["bash", _STARTUP_SCRIPT],
                    cwd=work_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._set_launch_status(
                    f"Launched (no terminal found) in {work_dir}", error=False
                )
            except OSError as exc:
                self._set_launch_status(f"Launch failed: {exc}", error=True)

    def _set_launch_status(self, msg: str, *, error: bool) -> None:
        colour = "#b71c1c" if error else "#1b5e20"
        self._launch_status_lbl.config(text=msg, foreground=colour)
        self.root.after(
            6000,
            self._launch_status_lbl.config,
            {"text": "", "foreground": "#555"},
        )

    # ── Left panel: file browser ──────────────────────────────────────────────

    def _build_file_browser(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="User files", font=self._fnt_bold).pack(
            anchor="w", padx=6, pady=(4, 2)
        )
        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=2)

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self._fb_canvas = tk.Canvas(wrap, width=200, highlightthickness=0)
        fb_vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._fb_canvas.yview)
        self._fb_canvas.configure(yscrollcommand=fb_vsb.set)
        fb_vsb.pack(side="right", fill="y")
        self._fb_canvas.pack(side="left", fill="both", expand=True)
        self._fb_frame = ttk.Frame(self._fb_canvas)
        self._fb_frame.bind(
            "<Configure>",
            lambda e: self._fb_canvas.configure(
                scrollregion=self._fb_canvas.bbox("all")
            ),
        )
        self._fb_canvas.create_window((0, 0), window=self._fb_frame, anchor="nw")
        for w in (self._fb_canvas, self._fb_frame):
            w.bind("<Button-4>", lambda e: self._fb_canvas.yview_scroll(-1, "units"))
            w.bind("<Button-5>", lambda e: self._fb_canvas.yview_scroll(1, "units"))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=2, pady=(2, 0))
        ttk.Button(parent, text="Refresh", command=self._refresh_files).pack(
            fill="x", padx=6, pady=4
        )

    def _populate_file_browser(self) -> None:
        for w in self._fb_frame.winfo_children():
            w.destroy()
        old_vars = dict(self._file_vars)
        self._file_vars.clear()

        for display_name, kind, abs_path, indent in scan_user_dir(USER_DIR):
            if kind == "dir":
                ttk.Label(
                    self._fb_frame, text=f"📁 {display_name}", foreground="#666"
                ).pack(anchor="w", padx=indent, pady=(5, 1))
            else:
                prev = old_vars.get(abs_path)
                default_on = (
                    prev.get()
                    if prev is not None
                    else (display_name == _DEFAULT_PLAN_FILE)
                )
                var = tk.BooleanVar(value=default_on)
                self._file_vars[abs_path] = var
                ttk.Checkbutton(
                    self._fb_frame,
                    text=display_name,
                    variable=var,
                    command=self._on_file_toggle,
                ).pack(anchor="w", padx=indent, pady=1)

        self._fb_canvas.update_idletasks()
        self._fb_canvas.configure(scrollregion=self._fb_canvas.bbox("all"))

    def _refresh_files(self) -> None:
        self._populate_file_browser()
        self._refresh_plan_dropdown(preserve_selection=True)

    def _on_file_toggle(self) -> None:
        self._refresh_plan_dropdown(preserve_selection=True)

    # ── Plan dropdown ─────────────────────────────────────────────────────────

    def _refresh_plan_dropdown(self, preserve_selection: bool = True) -> None:
        old = self._plan_var.get() if preserve_selection else ""
        self._plan_origins.clear()
        self._plan_specs.clear()
        self._plan_list.clear()

        for abs_path, var in self._file_vars.items():
            if not var.get():
                continue
            module = file_to_module(abs_path)
            for name, spec in find_plan_specs(abs_path).items():
                if name not in self._plan_specs:
                    self._plan_specs[name] = spec
                    self._plan_origins[name] = module
                    self._plan_list.append(name)

        self._plan_cb["values"] = self._plan_list
        if self._plan_list:
            new_sel = (
                old
                if (preserve_selection and old in self._plan_list)
                else self._plan_list[0]
            )
            self._plan_var.set(new_sel)
            self._on_plan_change()
        else:
            self._plan_var.set("")
            self._doc_var.set("(no plans — check a .py file in the left panel)")
            self._rebuild_param_form([])
            self._set_cmd_text("(no plan selected)")

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_plan_ui(self, parent: ttk.Frame) -> None:
        # Plan selector
        top = ttk.Frame(parent, padding=(8, 6, 8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Plan:", font=self._fnt_bold).pack(side="left", padx=(0, 6))
        self._plan_var = tk.StringVar()
        self._plan_cb = ttk.Combobox(
            top, textvariable=self._plan_var, state="readonly", width=30
        )
        self._plan_cb.pack(side="left")
        self._plan_cb.bind("<<ComboboxSelected>>", self._on_plan_change)
        self._doc_var = tk.StringVar()
        ttk.Label(
            top,
            textvariable=self._doc_var,
            foreground="#555",
            wraplength=430,
            justify="left",
        ).pack(side="left", padx=10)

        ttk.Separator(parent, orient="horizontal").pack(fill="x")

        # Scrollable parameter form
        pf = ttk.LabelFrame(parent, text="Parameters", padding=(6, 4))
        pf.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self._param_canvas = tk.Canvas(pf, highlightthickness=0)
        pf_vsb = ttk.Scrollbar(pf, orient="vertical", command=self._param_canvas.yview)
        self._param_canvas.configure(yscrollcommand=pf_vsb.set)
        pf_vsb.pack(side="right", fill="y")
        self._param_canvas.pack(side="left", fill="both", expand=True)
        self._param_frame = ttk.Frame(self._param_canvas)
        self._param_frame.bind(
            "<Configure>",
            lambda e: self._param_canvas.configure(
                scrollregion=self._param_canvas.bbox("all")
            ),
        )
        self._param_win = self._param_canvas.create_window(
            (0, 0), window=self._param_frame, anchor="nw"
        )
        self._param_canvas.bind(
            "<Configure>",
            lambda e: self._param_canvas.itemconfig(self._param_win, width=e.width),
        )
        for w in (self._param_canvas, self._param_frame):
            w.bind("<Button-4>", lambda e: self._param_canvas.yview_scroll(-1, "units"))
            w.bind("<Button-5>", lambda e: self._param_canvas.yview_scroll(1, "units"))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=(4, 0))

        # Command display
        cmd_frame = ttk.LabelFrame(
            parent, text="Command  (paste into IPython)", padding=(8, 6)
        )
        cmd_frame.pack(fill="x", padx=6, pady=6)

        self._cmd_display = tk.Text(
            cmd_frame,
            height=4,
            font=self._fnt_mono,
            background="#f0f0f0",
            foreground="#000000",
            relief="flat",
            borderwidth=0,
            wrap="word",
        )
        self._cmd_display.pack(fill="x", pady=(0, 6))
        self._cmd_display.tag_configure("import_tag", foreground="#1565c0")
        self._cmd_display.bind("<Key>", self._block_edit)

        btn_row = ttk.Frame(cmd_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Build / Update", command=self._update_command).pack(
            side="left", padx=2
        )
        ttk.Button(btn_row, text="Copy", command=self._copy_command).pack(
            side="left", padx=2
        )
        self._status_lbl = ttk.Label(btn_row, text="", foreground="#555")
        self._status_lbl.pack(side="left", padx=10)

    # ── Plan / parameter form ─────────────────────────────────────────────────

    def _on_plan_change(self, *_) -> None:
        plan_name = self._plan_var.get()
        if not plan_name:
            return
        spec = self._plan_specs.get(plan_name)
        module = self._plan_origins.get(plan_name, "")
        if spec and spec["documented"]:
            self._doc_var.set(spec["summary"] or (f"from {module}" if module else ""))
            self._current_params = spec["params"]
            self._rebuild_param_form(spec["params"])
        else:
            summary = spec["summary"] if spec else ""
            self._doc_var.set(summary or (f"from {module}" if module else ""))
            self._current_params = []
            self._rebuild_generic_form()
        self._set_cmd_text("(fill in parameters, then click  Build / Update)")

    def _make_label(self, spec: ParamSpec) -> str:
        label = spec.short or spec.name
        if spec.units:
            label += f"  ({spec.units})"
        if spec.required:
            label += "  ★"
        return label

    def _rebuild_param_form(self, params: list[ParamSpec]) -> None:
        for w in self._param_frame.winfo_children():
            w.destroy()
        self._param_widgets.clear()

        for row, spec in enumerate(params):
            lbl = ttk.Label(
                self._param_frame,
                text=self._make_label(spec),
                anchor="nw",
                justify="left",
                wraplength=260,
            )
            lbl.grid(row=row, column=0, sticky="nw", padx=(6, 10), pady=4)
            if spec.long:
                _Tooltip(lbl, spec.long)

            if spec.dtype == "positions":
                frm = ttk.Frame(self._param_frame)
                frm.grid(row=row, column=1, sticky="ew", padx=4, pady=3)
                txt = tk.Text(
                    frm, height=5, font=self._fnt_mono, relief="solid", borderwidth=1
                )
                txt.pack(fill="x")
                ttk.Label(
                    frm,
                    text="e.g.  100, 0, 50\n      150, 0, 50",
                    foreground="#888",
                    font=self._fnt_mono,
                ).pack(anchor="w")
                if spec.long:
                    _Tooltip(txt, spec.long)
                self._param_widgets[spec.name] = (spec, txt)
            elif spec.dtype == "bool":
                var = tk.BooleanVar(value=bool(spec.default))
                cb = ttk.Checkbutton(self._param_frame, variable=var)
                cb.grid(row=row, column=1, sticky="w", padx=4, pady=4)
                if spec.long:
                    _Tooltip(cb, spec.long)
                self._param_widgets[spec.name] = (spec, var)
            elif spec.dtype == "choice":
                opts = spec.choices or (
                    [str(spec.default)] if spec.default is not None else []
                )
                init = (
                    str(spec.default)
                    if spec.default is not None
                    else (opts[0] if opts else "")
                )
                var = tk.StringVar(value=init)
                cbx = ttk.Combobox(
                    self._param_frame,
                    textvariable=var,
                    values=opts,
                    state="readonly",
                    width=22,
                )
                cbx.grid(row=row, column=1, sticky="w", padx=4, pady=4)
                if spec.long:
                    _Tooltip(cbx, spec.long)
                self._param_widgets[spec.name] = (spec, var)
            else:  # str / int / float / unknown -> text entry
                init = "" if spec.default in (None, _NODEFAULT) else str(spec.default)
                var = tk.StringVar(value=init)
                ent = ttk.Entry(self._param_frame, textvariable=var, width=48)
                ent.grid(row=row, column=1, sticky="ew", padx=4, pady=4)
                if spec.long:
                    _Tooltip(ent, spec.long)
                self._param_widgets[spec.name] = (spec, var)

        self._param_frame.columnconfigure(1, weight=1)

    def _rebuild_generic_form(self) -> None:
        for w in self._param_frame.winfo_children():
            w.destroy()
        self._param_widgets.clear()
        ttk.Label(
            self._param_frame, text="Arguments  (Python syntax, comma-separated):"
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 2))
        txt = tk.Text(
            self._param_frame,
            height=5,
            font=self._fnt_mono,
            relief="solid",
            borderwidth=1,
        )
        txt.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        ttk.Label(
            self._param_frame,
            text="e.g.  file_name='test', p_start=-5, p_end=5",
            foreground="#888",
            font=self._fnt_mono,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6)
        self._param_widgets["__args__"] = ("generic", txt)
        self._param_frame.columnconfigure(0, weight=1)

    # ── Parameter parsing ─────────────────────────────────────────────────────

    def _parse_params(self) -> tuple[dict | None, list[str]]:
        plan_name = self._plan_var.get()
        if not plan_name:
            return None, ["No plan selected."]
        if "__args__" in self._param_widgets:
            _, txt = self._param_widgets["__args__"]
            return {"__args__": txt.get("1.0", "end").strip()}, []

        values: dict = {}
        errors: list[str] = []

        for spec in self._current_params:
            widget = self._param_widgets[spec.name][1]
            short = spec.short or spec.name

            if spec.dtype == "positions":
                raw = widget.get("1.0", "end").strip()
                if not raw:
                    if spec.required:
                        errors.append(f"{short}: required")
                    continue
                triples = []
                for i, line in enumerate(raw.splitlines(), 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parts = [float(x.strip()) for x in line.split(",")]
                        if len(parts) != 3:
                            raise ValueError(f"expected 3 values, got {len(parts)}")
                        triples.append(tuple(parts))
                    except ValueError as exc:
                        errors.append(f"{short} line {i}: {exc}")
                if triples:
                    values[spec.name] = triples
            elif spec.dtype == "bool":
                values[spec.name] = widget.get()
            elif spec.dtype == "choice":
                val = widget.get().strip()
                if val:
                    values[spec.name] = val
                elif spec.default not in (None, _NODEFAULT):
                    values[spec.name] = spec.default
            elif spec.dtype == "float":
                self._read_number(spec, widget, values, errors, short, float, "number")
            elif spec.dtype == "int":
                self._read_number(spec, widget, values, errors, short, int, "integer")
            else:  # str / unknown -> text
                raw = widget.get().strip()
                if raw:
                    values[spec.name] = raw
                elif spec.blank_omits:
                    pass
                elif spec.required:
                    errors.append(f"{short}: required")
                elif spec.default not in (None, _NODEFAULT):
                    values[spec.name] = spec.default

        return values, errors

    @staticmethod
    def _read_number(spec, widget, values, errors, short, caster, kind) -> None:
        raw = widget.get().strip()
        if raw:
            try:
                values[spec.name] = caster(raw)
            except ValueError:
                errors.append(f"{short}: not a valid {kind}")
        elif spec.blank_omits:
            pass
        elif spec.required:
            errors.append(f"{short}: required")
        elif spec.default not in (None, _NODEFAULT):
            values[spec.name] = spec.default

    # ── Command generation ────────────────────────────────────────────────────

    def _make_import_line(self, plan_name: str) -> str:
        module = self._plan_origins.get(plan_name, "id3c.user.db_bps")
        return f"from {module} import {plan_name}"

    def _make_re_line(self, plan_name: str, values: dict) -> str:
        if "__args__" in values:
            return f"RE({plan_name}({values['__args__']}))"
        args = []
        for spec in self._current_params:
            if spec.name not in values:
                continue
            args.append(f"{spec.name}={values[spec.name]!r}")
        return f"RE({plan_name}({', '.join(args)}))"

    def _update_command(self, *_) -> tuple[str, str] | tuple[None, None]:
        plan_name = self._plan_var.get()
        if not plan_name:
            self._set_cmd_text("(no plan selected)")
            return None, None
        values, errors = self._parse_params()
        if errors:
            self._set_cmd_text("Errors:\n" + "\n".join(f"  • {e}" for e in errors))
            return None, None
        import_line = self._make_import_line(plan_name)
        re_line = self._make_re_line(plan_name, values)
        self._set_cmd_text(
            import_line + "\n" + re_line, import_len=len(import_line) + 1
        )
        return import_line, re_line

    def _set_cmd_text(self, text: str, import_len: int = 0) -> None:
        self._cmd_display.delete("1.0", "end")
        self._cmd_display.insert("1.0", text)
        if import_len:
            self._cmd_display.tag_add("import_tag", "1.0", f"1.0 + {import_len} chars")

    # ── Copy ─────────────────────────────────────────────────────────────────

    def _copy_command(self) -> None:
        import_line, re_line = self._update_command()
        if not re_line:
            return
        full = f"{import_line}\n{re_line}"
        copied = False
        for args in (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                subprocess.run(
                    args,
                    input=full,
                    text=True,
                    check=True,
                    capture_output=True,
                    timeout=2,
                )
                copied = True
                break
            except (
                FileNotFoundError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ):
                pass
        if not copied:
            self.root.clipboard_clear()
            self.root.clipboard_append(full)
        self._status_lbl.config(text="Copied.")
        self.root.after(3000, self._status_lbl.config, {"text": ""})

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _block_edit(event: tk.Event) -> str | None:
        """Allow Ctrl+C / Ctrl+A on read-only text widgets; block everything else."""
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return None
        return "break"


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the Bluesky Plan Runner GUI."""
    root = tk.Tk()
    BlueSkyPlanGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
