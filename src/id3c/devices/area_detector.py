"""
EPICS area_detector definitions for ID8.
"""

from apstools.devices import CamMixin_V34
from ophyd import ADComponent
from ophyd import EpicsSignal
from ophyd.areadetector import EigerDetectorCam


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
