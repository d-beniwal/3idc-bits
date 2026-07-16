"""User data-acquisition plan modules for 3-ID-C.

Each user / campaign keeps their ``@plan``-decorated plans in a module
here (or in a subpackage such as ``s3idc_plans``).  Nothing here is
imported by ``id3c.startup`` automatically; load what you need, e.g.::

    from id3c.user.s3idc_plans.setup_june_26 import omega_fly
    RE(omega_fly())
"""
