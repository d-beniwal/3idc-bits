# Plans, plan stubs, and the `@plan` decorator

Bluesky distinguishes between **plans** and **plan stubs**.  Both are
generator functions that yield messages for the RunEngine; the
technical difference is *what* they yield.

This page explains the distinction, when it matters, and how the
`@plan` decorator helps catch a common mistake.

## The technical distinction

| Kind        | What it does                                                                                 | Examples in `bluesky` |
|-------------|----------------------------------------------------------------------------------------------|-----------------------|
| **plan**    | Publishes Bluesky documents.  Yields `open_run` / `create` / `save` / `close_run` messages. | `bp.count`, `bp.scan`, `bp.rel_scan`, `bp.list_scan` |
| **plan stub** | Does *not* publish documents.  Yields `set`, `read`, `wait`, `mv`, `sleep`, etc.            | `bps.mv`, `bps.abs_set`, `bps.sleep`, `bps.null`     |

Plans bracket a run; the RunEngine assigns a run UID and `scan_id`,
and every document the plan yields becomes part of that run.  After
`close_run`, the RunEngine considers the run finished, fires
"stop" subscribers (file writers flush, etc.), and the catalog gains
one new entry.

Plan stubs run as part of a plan, but do not start or end a run on
their own.  They are the building blocks.  `bp.scan` is internally a
collection of `bps.mv` (move) and `bps.trigger_and_read` (acquire)
stubs wrapped in `open_run` / `close_run`.

## Why the distinction matters

For **using** Bluesky, the distinction barely matters.  You can pass
either to `RE(...)`:

```python
RE(bp.scan([detector], motor, 0, 10, 11))   # a plan
RE(bps.mv(motor, 5))                        # a plan stub
```

Both work.  The difference is what gets saved to the catalog:

- The `bp.scan` produces a complete Bluesky run -- a catalog entry
  with metadata, an event stream, and the data.
- The `bps.mv` produces no catalog entry.  The motor moves; nothing
  is recorded.

For **writing** Bluesky code, the distinction matters a lot:

- Authoring a plan stub is easy: write a generator function that
  yields stubs.  No `open_run` / `close_run` needed; the caller will
  provide those.
- Authoring a plan requires you to call `open_run` / `create` /
  `save` / `close_run` (or use a helper like
  `bluesky.preprocessors.run_decorator`).  Otherwise the data you
  acquire never gets attached to a run.

Most user code is plan stubs.  Composing them into a plan is usually
a matter of calling an existing `bp.*` plan, not writing one yourself.

## The `@plan` decorator

`bluesky.utils.plan` wraps your function so that, if you call it
without `RE(...)` (or `yield from`), Python prints a warning
shortly after you press Enter -- usually right next to the next
prompt.

It catches the common mistake of typing `my_plan(...)` at the
IPython prompt instead of `RE(my_plan(...))`.

Compare:

```python
# Without @plan
def my_plan():
    yield from bps.mv(motor, 5)

my_plan()    # silently does nothing; no warning
```

```python
from bluesky.utils import plan

@plan
def my_plan():
    yield from bps.mv(motor, 5)

my_plan()
# RuntimeWarning: plan `my_plan` was never iterated,
#                 did you mean to use `yield from`?
```

The warning shows up shortly after the prompt returns -- typically
mixed in with the next prompt line.  The traceback it prints points
at *your* call site, not at internal Bluesky code, so you can see
exactly which line you typed.

### Convention in this repo

All plans and plan stubs we author are decorated with `@plan`.  See
the [AGENTS.md > `@plan` decorator on our own
plans](https://github.com/BCDA-APS/3idc-bits/blob/main/AGENTS.md#plan-decorator-on-our-own-plans)
section.  Examples in this repo:

- `LaserOptics.move_in` / `move_out` -- plan stubs as device methods
- `sim_count_plan`, `sim_print_plan`, `sim_rel_scan_plan` -- plans

The decorator works for both plans and plan stubs; it does not
distinguish them.  It also works on instance methods (`self` is
passed through normally).

### What `@plan` does *not* do

- It does not turn a non-generator function into a plan.  The
  decorated function still has to `yield` something for the
  RunEngine to do.
- It does not validate the message stream.
- It does not prevent you from calling the function without `RE(...)`
  -- it only *warns*.  A tight loop or a script that exits quickly
  may finish before the warning is printed, so you may not see it
  in non-interactive contexts.
- It does not perform any work at decoration time; the cost is one
  `Plan` object wrapper per call.

## See also

- [The RunEngine](run_engine.md) -- why `RE(...)` exists at all.
- [Run a scan](../how_to/run_a_scan.md) -- using plans interactively.
- [Add a plan](../how_to/add_a_plan.md) -- writing your own plans, with
  the `@plan` decorator applied per repo convention.
