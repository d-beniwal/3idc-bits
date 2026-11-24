"""Device lookup shared by the OPT_Luca plans."""

from apsbits.core import instrument_init


def find_device(device):
    """Return ``device`` itself, or the registered device if it is a name (str).

    ``instrument_init.oregistry`` is looked up at call time, not imported by name:
    ``init_instrument`` (run by ``id3c.startup``) rebinds it, and a name imported
    earlier would stay ``None``.  Dotted component names such as
    ``"sample_stage.xprime"`` work.  Raises ``ComponentNotFound`` for an unknown name.
    """
    if not isinstance(device, str):
        return device
    registry = instrument_init.oregistry
    if registry is None:
        raise RuntimeError(
            f"cannot look up {device!r}: the instrument is not initialised"
            " (import id3c.startup first, or pass the device object)"
        )
    return registry.find(device)
