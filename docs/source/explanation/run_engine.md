---
orphan: false
---

# The RunEngine: why `RE(...)` and `yield from`

This page answers two questions that confuse nearly every new Bluesky
user:

1. **Why do I have to type `RE(...)` around everything?** Why isn't
   `bps.mv(motor, 5)` enough?
2. **What is `yield from` for, and when do I use it?**

If you can already answer both questions confidently, you can skip
this page.  Otherwise, read on.

## The short answer

A **plan** is a Python *generator*: a function that produces a sequence
of messages describing what should happen.  Plans do not *do* anything
by themselves.  Calling `bps.mv(motor, 5)` returns a generator object
and immediately discards it -- no PV is written, no motor moves.

The **RunEngine** (`RE` in our session) is the thing that actually
executes a plan: it iterates the generator, dispatches each message to
the appropriate device, collects readings, publishes documents to
subscribers, handles pauses and cleanup, and stops on errors.

So `RE(bps.mv(motor, 5))` means: "Hand this plan to the RunEngine for
execution."

`yield from` is the Python syntax for *composing* one generator inside
another.  You use it when one of your own plans calls another plan or
plan stub:

```python
@plan
def my_plan():
    yield from bps.mv(motor, 0)           # call a stub from inside a plan
    yield from bp.count([detector], 5)    # call another plan
```

You do **not** use `yield from` at the top level (the IPython prompt or
a script).  At the top level you use `RE(...)`.

## Why `RE(...)` instead of just `motor.move(5)`?

You can move a motor without the RunEngine.  In an IPython session,
this just works:

```python
sample_stage.x.move(12.3)     # direct ophyd call; the motor moves
```

It is a synchronous CA put.  The motor moves.  Done.

So why bother with `RE(bps.mv(sample_stage.x, 12.3))`?  Because direct
ophyd calls give you **only** the motor motion.  The RunEngine path
gives you everything that makes Bluesky useful:

| feature                              | `motor.move(5)` | `RE(bps.mv(motor, 5))` |
|--------------------------------------|:---------------:|:----------------------:|
| Motor moves                          | yes             | yes                    |
| Metadata recorded                    | no              | yes                    |
| Published as a Bluesky run           | no              | yes (when in `bp.*`)   |
| Pausable with Ctrl-C / resumable     | no              | yes                    |
| Suspended on beam-loss / shutter close| no              | yes (with suspenders)  |
| Subscribers (plots, file writers) fired| no            | yes                    |
| Plan-level error handling / cleanup  | no              | yes                    |

For a one-off "nudge this motor by 0.1 mm so I can see what the
sample looks like," `motor.move(0.1)` is fine.  For anything you want
to *record*, *reproduce*, or *include in a scan*, use the RunEngine.

(direct-ophyd-calls)=
## What about `motor.read()`, `device.get()`, `laser_optics.is_out`?

These are **not** plans.  They are direct ophyd queries.  Wrapping
them in `RE(...)` is wrong and will fail confusingly.  Use them
directly:

```python
sample_stage.x.position             # most recent setpoint (float)
sample_stage.x.user_readback.get()  # current .RBV value
sample_stage.x.read()               # dict of all 'normal' kind signals
laser_optics.is_out                 # a Python property; True/False
```

The rule:

- Returns a *generator*?  Use `RE(...)` at the top level.
  (Bluesky plans, plan stubs, our `@plan`-decorated functions, our
  `laser_optics.move_out()` method.)
- Returns *data* (a number, a dict, a Status object, a bool)?  Call
  directly.

If you are unsure, type the expression at the IPython prompt with no
wrapper.  If it returns something like `<generator object ...>` or
`<bluesky.utils.Plan ...>`, it is a plan and you need `RE(...)`.  If
it returns a value, an exception, or just runs and finishes, it was
not a plan.

## Why is Bluesky built this way?

Because separating "what should happen" (the plan) from "how to do
it" (the RunEngine) lets the RunEngine add features that would be
impossible if plans ran imperatively:

- **Pause / resume.**  Ctrl-C twice during a long scan pauses the
  RunEngine *between messages*.  The plan is paused at a well-defined
  point; you can inspect things, fix something, then call
  `RE.resume()`.  Possible only because the plan is a generator the
  RunEngine drives one step at a time.
- **Metadata threading.**  The `md=` kwarg on plans is woven through
  every document published during the run.  The RunEngine knows where
  the run starts (when the plan yields an `'open_run'` message) and
  ends (`'close_run'`).
- **Subscribers.**  Live plots, file writers, the SPEC-format writer,
  the Tiled writer -- they all subscribe to the document stream the
  RunEngine emits.  Direct ophyd calls bypass all of this; no
  subscriber sees them.
- **Suspenders.**  "Pause everything if the beam dumps; resume when
  it comes back" is implemented by the RunEngine, not by individual
  devices.
- **Recoverable failure.**  If a plan raises, the RunEngine runs the
  plan's cleanup (`finally` blocks, contextmanagers).  Direct ophyd
  calls have no such safety net.

## When *do* you use `yield from`?

Inside a plan you are writing.  Here is the rule of thumb, expressed
as three correct uses of the same `bps.mv` stub:

```python
# 1. IPython prompt, script: top level
RE(bps.mv(motor, 5))

# 2. Composing inside a plan you are writing:
@plan
def park_motors():
    yield from bps.mv(motor_a, 0, motor_b, 0)
    yield from bps.mv(motor_c, 10)

# 3. Inside a plan, using a stub on a device that has plan methods:
@plan
def setup_optics():
    yield from laser_optics.move_out()
    yield from bps.mv(shutter, "open")
```

Never:

```python
# 4. Top level, no RE -- silently does nothing:
bps.mv(motor, 5)

# 5. Inside a plan, no yield from -- silently does nothing:
@plan
def broken_plan():
    bps.mv(motor, 5)        # WRONG -- the generator is discarded
    yield from bp.count(...)
```

Cases (4) and (5) are exactly what the [`@plan`
decorator](plans_and_stubs.md#the-plan-decorator) catches.  Type
`my_plan(...)` without `RE(...)`, and a warning appears shortly
after you press Enter:

```
RuntimeWarning: plan `my_plan` was never iterated,
                did you mean to use `yield from`?
```

That warning is your cue to retype the command with `RE(...)`.

## Mental model for the SPEC user

In SPEC, `mv samx 5` *is* the act of moving the motor.  In Bluesky,
`bps.mv(sample_stage.x, 5)` is the *description* of moving the motor,
and the RunEngine is the thing that executes the description.  The
indirection feels unnecessary at first.  It is the cost of admission
for everything in the [table above](#direct-ophyd-calls).

See also the [SPEC → Bluesky cross-walk](../tutorials/spec_to_bluesky.md).

## Mental model for the EPICS user

`motor.move(5)` *is* essentially a `caput motor.VAL 5`.  It works for
the same reasons `caput` works.  The RunEngine path is what gives you
the things `caput` does not: structured metadata, document streams,
subscribers, pause/resume, suspenders.

See also [EPICS → ophyd](../tutorials/epics_to_ophyd.md).
