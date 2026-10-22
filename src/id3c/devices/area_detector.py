"""
EPICS area_detector cam definitions for 3-ID-C (and 8-ID-I, where noted).
"""

from apstools.devices import CamMixin_V34
from ophyd import ADComponent
from ophyd import EpicsSignal
from ophyd.areadetector import EigerDetectorCam
from ophyd.areadetector import PointGreyDetectorCam


class EigerDetectorCam_V34(CamMixin_V34, EigerDetectorCam):
    """Revise EigerDetectorCam for ADCore revisions."""

    initialize = ADComponent(EpicsSignal, "Initialize", kind="config")

    # These components not found on Eiger 4M at 8-ID-I
    # TODO: What about Eiger 500k at 3-ID-C?
    file_number_sync = None
    file_number_write = None
    fw_clear = None
    link_0 = None
    link_1 = None
    link_2 = None
    link_3 = None
    dcu_buff_free = None
    offset = None


class PointGreyDetectorCam_V34(CamMixin_V34, PointGreyDetectorCam):
    """Revise PointGreyDetectorCam for ADCore revisions.

    Used for the 3-ID-C alignment camera (``3IDPG3:``). ADCore version is
    assumed 3.4, matching :class:`EigerDetectorCam_V34` above; unverified
    against the live IOC (see ``.context/OPT_LUCA.md``).
    """
