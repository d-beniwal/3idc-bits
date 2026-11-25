"""Offline camera with queued callbacks and asynchronous TIFF writes."""

import threading
import time

import numpy as np
from ophyd import Component as Cpt
from ophyd import Device
from ophyd import Signal

_EPOCH = 631152000


class _Acquire(Signal):
    def put(self, value, *, callback=None, **kwargs):
        parent = self.parent.parent
        if int(value) and not self.get():
            super().put(1)
            parent._stop = threading.Event()
            threading.Thread(
                target=parent._run, args=(parent._stop,), daemon=True
            ).start()
        elif not int(value):
            parent._stop.set()
            super().put(0)
        if callback:
            callback()


class _Write(Signal):
    def put(self, value, *, callback=None, **kwargs):
        if not int(value):
            return super().put(0)
        super().put(1)
        tiff, parent = self.parent, self.parent.parent
        path, name, num = (
            tiff.file_path.get(),
            tiff.file_name.get(),
            tiff.file_number.get(),
        )
        tiff.full_file_name.put(f"{path.rstrip('/')}/{name}_{num:04d}.tif")
        if tiff.auto_increment.get() == "Yes":
            tiff.file_number.put(num + 1)
        tiff.write_status.put(0)  # OK while busy, even if the write will fail later.
        key = parent.key(tiff)

        def finish():
            time.sleep(parent.write_delay)
            if parent.tiff_fails:
                tiff.write_status.put(1)
                tiff.write_message.put("cannot write file: disk full")
            else:
                parent.written.append((path, name, num))
                parent.written_keys.append(key)
            Signal.put(self, 0)
            if callback:
                callback()

        threading.Thread(target=finish, daemon=True).start()


class _Cam(Device):
    acquire = Cpt(_Acquire, value=0)
    image_mode = Cpt(Signal, value="Continuous")
    trigger_mode = Cpt(Signal, value=3)
    num_images = Cpt(Signal, value=1)
    acquire_time = Cpt(Signal, value=0.2)
    pixel_format = Cpt(Signal, value=5)
    array_counter = Cpt(Signal, value=0)
    port_name = Cpt(Signal, value="CAM")
    num_queued_arrays = Cpt(Signal, value=0)


class _Plugin(Device):
    enable = Cpt(Signal, value="Enable")
    array_counter = Cpt(Signal, value=100)
    unique_id = Cpt(Signal, value=900)
    epics_ts_sec = Cpt(Signal, value=0)
    epics_ts_nsec = Cpt(Signal, value=0)
    num_queued_arrays = Cpt(Signal, value=0)
    nd_array_port = Cpt(Signal, value="CAM")
    port_name = Cpt(Signal, value="PLUGIN")
    sort_mode = Cpt(Signal, value="Unsorted")


class _Pva(_Plugin):
    pv_name = Cpt(Signal, value="SIM:Pva1:Image")


class _Tiff(_Plugin):
    enable = Cpt(Signal, value="Disable")
    file_path = Cpt(Signal, value="/old/path/")
    file_name = Cpt(Signal, value="old_name")
    file_number = Cpt(Signal, value=42)
    auto_increment = Cpt(Signal, value="No")
    auto_save = Cpt(Signal, value="No")
    file_write_mode = Cpt(Signal, value="Stream")
    file_template = Cpt(Signal, value="%s%s_%4.4d.tif")
    write_status = Cpt(Signal, value=0)
    write_message = Cpt(Signal, value="")
    full_file_name = Cpt(Signal, value="")
    capture = Cpt(Signal, value=0)
    write_file = Cpt(_Write, value=0)


class SimMicroscope(Device):
    """Model buffer identity independently of the camera's acquisition counter."""

    cam = Cpt(_Cam)
    transform1 = Cpt(_Plugin)
    pva = Cpt(_Pva)
    tiff1 = Cpt(_Tiff)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop = threading.Event()
        self.written, self.written_keys, self.read_pvs = [], [], []
        self.write_delay = self.pipeline_delay = 0.0
        self.tiff_fails = False
        self._uid = 900
        self.pva.port_name.put("PVA")
        self.tiff1.port_name.put("TIFF")
        self.transform1.port_name.put("TRANS")
        self.pva.nd_array_port.put("TRANS")
        self.tiff1.nd_array_port.put("TRANS")
        self.pva.image = np.zeros((4, 5), dtype=np.float32)
        self.timers = []

    @staticmethod
    def key(plugin):
        """PVA uses POSIX epoch; CA plugin timestamp fields use EPICS epoch."""
        return (
            int(plugin.unique_id.get()),
            int(plugin.epics_ts_sec.get()) + _EPOCH,
            int(plugin.epics_ts_nsec.get()),
        )

    def _publish(self, uid, stamp):
        for plugin in (self.pva, self.tiff1):
            if plugin.enable.get() != "Enable":
                continue
            plugin.unique_id.put(uid)
            plugin.epics_ts_sec.put(stamp[0] - _EPOCH)
            plugin.epics_ts_nsec.put(stamp[1])
            plugin.array_counter.put(plugin.array_counter.get() + 1)
            plugin.image = np.full((4, 5), uid, dtype=np.float32)
        if self.tiff1.enable.get() == "Enable" and self.tiff1.auto_save.get() == "Yes":
            self.tiff1.write_file.put(1)

    def queue_frame(self, delay):
        """Queue a frame already produced before the acquisition baseline."""
        self._uid += 1
        uid, stamp = self._uid, divmod(time.time_ns(), 10**9)
        self.cam.array_counter.put(self.cam.array_counter.get() + 1)
        # Transforms create their own NDArrays; downstream work can remain after
        # the camera's own queue is empty. Drain the declared ancestors too.
        pending = self.transform1.num_queued_arrays
        pending.put(pending.get() + 1)

        def complete():
            self._publish(uid, stamp)
            pending.put(pending.get() - 1)

        timer = threading.Timer(delay, complete)
        self.timers.append(timer)
        timer.start()

    def _run(self, stop):
        while not stop.wait(float(self.cam.acquire_time.get())):
            self.queue_frame(self.pipeline_delay)
            if self.cam.image_mode.get() == "Single":
                Signal.put(self.cam.acquire, 0)
                return

    def close(self):
        """Stop live acquisition and join delayed callbacks before the next test."""
        self.cam.acquire.put(0)
        for timer in self.timers:
            timer.join(timeout=2)
