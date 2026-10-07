"""GUI-facing copies of 3-ID-C library plans.

The plans shipped in ``id3c.plans`` are maintained upstream (BCDA-APS) and
document their arguments in stock NumPy style.  The plan-runner GUIs
(B-PILOT, and the older ``gui/3idc_tk.py``) build their parameter forms by
AST-parsing a stricter docstring grammar::

    Parameters
    ----------
    <name> : <dtype>[ [<units>]]
        <short label> :: <long tooltip>

Rather than change the upstream files, this subpackage holds GUI-facing
counterparts that carry that grammar:

* ``flyscan_3idc`` -- a hand-maintained *copy* of ``id3c.plans.flyscan_3idc``
  whose ``flyscan`` is re-decorated ``@plan`` and re-documented.  Re-sync it
  by hand if the library version changes.
* ``sim_plans`` -- thin ``@plan`` *wrappers* around ``id3c.plans.sim_plans``.
  These delegate rather than copy, so the simulator logic has exactly one
  home and only the docstrings live here.
* ``setup_june_26`` -- a hand-maintained *copy* of
  ``id3c.user.s3idc_plans.setup_june_26``, which already documents its
  plans in this grammar.  Nothing is reformatted; the copy exists purely so
  these plans are selectable from this subpackage.  Re-sync it by hand if
  the ``s3idc_plans`` version changes.

Nothing here is imported by ``id3c.startup`` automatically::

    from id3c.user.s3idc_gui.sim_plans import sim_count
    RE(sim_count(num=5))
"""
