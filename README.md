# 3-ID-C BITS instrument

[Bluesky Instrument (BITS)](https://github.com/BCDA-APS/BITS) package
for APS beamline 3-ID-C.  Built on the
[`apsbits`](https://github.com/BCDA-APS/apsbits) framework.

Where to start:

- **New to Bluesky?**  Read
  [Tutorial: Your first session](docs/source/tutorials/first_session.md).
- **Coming from SPEC?**  Read
  [SPEC → Bluesky cross-walk](docs/source/tutorials/spec_to_bluesky.md).
- **Coming from EPICS?**  Read
  [EPICS → ophyd](docs/source/tutorials/epics_to_ophyd.md).
- **Already familiar?**  Jump to the
  [cheat sheet](docs/source/reference/cheat_sheet.md) or
  [quick reference](docs/source/reference/quick_reference.md).

## Quick start

On a workstation with the conda environment already configured:

```bash
conda activate 3idc-bits
ipython
```

```python
from id3c.startup import *
RE(sim_print_plan())
```

## Plan-runner GUI

A small Tkinter GUI helps build a plan command and start a session. Run it
from the repository root (needs only the standard-library `tkinter`):

```bash
python gui/3idc_tk.py
```

**Build a command.** Tick a plan file in the left panel (it scans
`src/id3c/user/`; `setup_june_26.py` is checked by default), choose a plan,
fill in the parameters, then click **Build / Update** and **Copy** the
generated two-line `import` + `RE(...)` command to paste into IPython.

**Launch Bluesky.** *Before* clicking **▶ Launch Bluesky**, set the **Work
dir** field in the top bar to the folder where the session should start (this
is where data and logs are written; it is created if it does not exist). The
button then opens a terminal in that folder and runs `start_3idc_bluesky.sh`,
which activates the `3idc-bits` conda environment and starts IPython with the
instrument loaded (`from id3c.startup import *`).

## Contributing

Repository conventions live in
[`AGENTS.md`](AGENTS.md) at the root.  Developer-facing guidance
(pre-commit setup, opt-out, style) is in
[`docs/source/contributing.md`](docs/source/contributing.md).

## Issue tracker

<https://github.com/BCDA-APS/3idc-bits/issues>

## Initial installation

For first-time installation, see
[the developer setup section in CONTRIBUTING](docs/source/contributing.md#developer-setup).
