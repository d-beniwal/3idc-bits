# How to add a new plan

This page covers writing and registering a custom Bluesky plan or
plan stub for the `id3c` instrument.

For the difference between a plan and a plan stub, see
[Plans and stubs](../explanation/plans_and_stubs.md).

## Where things live

- `src/id3c/plans/` -- plan modules.  One file per topic; group
  related plans in one file.
- `src/id3c/startup.py` -- imports plans at session end so they
  become available at the IPython prompt.

## Skeleton

```python
# src/id3c/plans/my_plans.py
"""Plans for ___ at 3-ID-C."""

import logging

from bluesky import plan_stubs as bps
from bluesky import plans as bp
from bluesky.utils import plan

logger = logging.getLogger(__name__)


@plan
def park_and_count(detectors, num=1, md=None):
    """Park the sample stage, then count.

    Parameters
    ----------
    detectors : list
        Detectors to read in the count.
    num : int
        Number of counts.
    md : dict, optional
        Extra metadata to attach to the run.
    """
    md = md or {}
    md = {"purpose": "park-then-count", **md}
    yield from bps.mv(sample_stage.x, 0, sample_stage.y, 0)
    yield from bp.count(detectors, num=num, md=md)
```

Then register it in `startup.py` so the prompt sees it:

```python
# src/id3c/startup.py  (at the bottom, with the other plan imports)
from .plans.my_plans import park_and_count   # noqa: E402, F401
```

Restart the IPython session; `park_and_count` is now available:

```python
RE(park_and_count([scaler], num=5))
```

## Repository conventions

These are the established conventions from
[AGENTS.md](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md);
follow them in new plans.

### Decorate with `@plan`

Always:

```python
from bluesky.utils import plan

@plan
def my_plan(...):
    ...
```

`@plan` wraps the generator so that discarding it without iteration
(the common new-user mistake of `my_plan()` instead of
`RE(my_plan())`) emits a `RuntimeWarning` pointing at the user's call
site.  See [The `@plan`
decorator](../explanation/plans_and_stubs.md#the-plan-decorator).

### Examples in docstrings use `RE(...)`

Any code example in a plan's docstring should use
`RE(my_plan(...))`.  This is the pattern users see in the rest of
the docs and the help() output should match.  Direct
`yield from my_plan(...)` examples are appropriate only when
showing composition of one plan inside another.

### Plan stubs vs. plans

If your function publishes documents (uses `bp.*` plans, or
explicit `bps.open_run` / `bps.close_run`), it is a **plan**.
If it does not (only `bps.mv`, `bps.sleep`, etc.), it is a
**plan stub**.

The decorator is the same; the difference is what your function
should be composed *into*.  Plans are typically run by the user
at the top level (`RE(my_plan())`).  Plan stubs are typically
called from inside another plan
(`yield from my_stub(...)`), though both work either way.

### Metadata

Accept a `md` kwarg in plans that publish documents, merge it with
your defaults, and pass it through:

```python
@plan
def my_plan(detectors, md=None):
    md = md or {}
    md = {"plan_name": "my_plan", **md}
    yield from bp.count(detectors, md=md)
```

This lets users attach their own metadata without your plan
silently discarding it.

## Composing plan stubs

Inside a plan you are writing, use `yield from` to call another
plan or stub:

```python
@plan
def align_then_scan(detectors):
    yield from align_sample()              # another plan you wrote
    yield from bps.sleep(0.5)
    yield from bp.scan(detectors, sample_stage.x, 0, 10, 11)
```

You do **not** use `RE(...)` inside another plan.  `RE(...)` is the
top-level invocation only.

If you forget `yield from`, the inner generator is created and
discarded -- the call does nothing.  This is the same bug
[`@plan`](../explanation/plans_and_stubs.md#the-plan-decorator) is
designed to catch; decorating both the outer and inner plan turns
the silent no-op into a visible warning.

## Testing without EPICS

This repo is developed on a host that cannot reach the beamline
EPICS PVs.  For an offline check that a plan is at least *shaped*
right:

```python
from id3c.plans.my_plans import park_and_count

# 1. Decorator marker present?
park_and_count._is_plan_     # True if @plan applied

# 2. Calling it returns a Plan, not None?
park_and_count([], num=1)    # <Plan object ...>

# 3. Smoke-iterate to surface obvious errors (will raise on the first
#    real device access, which is expected off-network):
gen = park_and_count([], num=1)
next(gen)   # the first yielded message
```

For full validation, run the plan on a workstation that can reach
the IOCs.

## See also

- [Plans and stubs](../explanation/plans_and_stubs.md) -- the
  conceptual background.
- [The RunEngine](../explanation/run_engine.md) -- why `yield from`
  works and `RE(...)` exists.
- [How to run a scan](run_a_scan.md) -- the built-in plans your
  custom plans usually compose.
