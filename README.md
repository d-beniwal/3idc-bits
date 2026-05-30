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
