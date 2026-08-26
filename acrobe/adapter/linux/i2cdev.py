"""I²C master through the kernel's ``i2c-dev`` character device.

One ``/dev/i2c-N`` node is one bus, so it maps to one
`i2c.Interface`. Slaves are summoned by name off it exactly as on any
other I²C adapter -- ``i2c-1/i2c/24lc64(saddr=0x50)``.

``I2C_RDWR`` puts repeated STARTs between its messages and a single
STOP at the end, which is precisely one `i2c.Transfer`. A multi-item
`i2c.Transaction` is therefore a *sequence* of ioctls, not one ioctl
with more messages -- fusing them would drop the STOPs between items
and silently change what reaches the wire.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import glob
import os
import time

from ...db import NoMatch
from ...engine import BackgroundLowering
from ...protocol import i2c
from ...util.pretty import metric
from ..model import Adapter, Enumerator, enumerator_db
from .char_device import CharDevice
from .ioctl import Ioctl  # noqa: F401 — carries the platform guard


# linux/i2c-dev.h. Plain numbers, not _IOC-encoded.
I2C_RETRIES = 0x0701
I2C_TIMEOUT = 0x0702
I2C_FUNCS = 0x0705
I2C_RDWR = 0x0707

# linux/i2c.h
I2C_M_RD = 0x0001
I2C_FUNC_I2C = 0x00000001

# i2c_msg.len is __u16.
MAX_MSG_LEN = 0xFFFF

SYSFS_CLASS_DIR = "/sys/class/i2c-dev"
SYSFS_DEVICES_DIR = "/sys/bus/i2c/devices"


class I2cMsg(ctypes.Structure):
    """``struct i2c_msg``."""

    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.c_void_p),
    ]


class I2cRdwrIoctlData(ctypes.Structure):
    """``struct i2c_rdwr_ioctl_data``."""

    _fields_ = [
        ("msgs", ctypes.c_void_p),
        ("nmsgs", ctypes.c_uint32),
    ]


# 12 bytes on 32-bit, 16 on 64-bit: three u16, a pad, and a pointer.
assert ctypes.sizeof(I2cMsg) == 8 + ctypes.sizeof(ctypes.c_void_p)


class I2cBusInfo:
    """What sysfs says about one ``/dev/i2c-N``.

    Both fields are genuinely optional -- an ACPI x86 host has no
    device-tree node and so no declared clock -- so a missing file
    means "unknown", not a failure to paper over.
    """

    def __init__(self, path: str, *, sysfs_dir: str = SYSFS_CLASS_DIR,
                 devices_dir: str = SYSFS_DEVICES_DIR):
        self.path = path
        self.bus = os.path.basename(path)
        self.__sysfs_dir = sysfs_dir
        self.__devices_dir = devices_dir

    @property
    def name(self) -> str | None:
        """The driver's own label, e.g. ``bcm2835 (i2c@7e804000)``."""
        try:
            with open(os.path.join(self.__sysfs_dir, self.bus, "name")) as f:
                return f.read().strip()
        except OSError:
            return None

    @property
    def clock_hz(self) -> int | None:
        """The device tree's ``clock-frequency``, a big-endian u32."""
        try:
            with open(os.path.join(self.__devices_dir, self.bus,
                                   "of_node", "clock-frequency"), "rb") as f:
                raw = f.read(4)
        except OSError:
            return None
        if len(raw) != 4:
            return None
        return int.from_bytes(raw, "big")


class NormalizedTransaction:
    """A `i2c.Transaction` plus whether the caller posted a naked item."""

    __slots__ = ("transaction", "single")

    def __init__(self, transaction, single: bool):
        self.transaction = transaction
        self.single = single

    def __repr__(self):
        return repr(self.transaction)


class I2cdevInterface(i2c.Interface, BackgroundLowering):
    """I²C bus behind one ``/dev/i2c-N`` node."""

    # Ack-poll cadence when a WaitAck names none.
    DEFAULT_WAIT_INTERVAL = 0.001

    def __init__(self, path: str, name: str = "i2c", *, device=None,
                 info=None):
        # adapter=None: this interface owns the wire.
        i2c.Interface.__init__(self, None, name)
        self.__dev = device if device is not None else CharDevice(path)
        self.__info = info if info is not None else I2cBusInfo(path)
        self.__funcs = 0
        self.__clock_hz = None
        self.__retries = None
        self.__timeout_s = None
        self.__probe_kind = "zlen"
        self.__stopping = False

    @property
    def path(self) -> str:
        return self.__dev.path

    def option_set(self, key, value):
        if key == "retries":
            self.__retries = int(str(value), 0)
        elif key == "timeout":
            self.__timeout_s = float(value)
        elif key == "wait_probe":
            if value not in ("zlen", "read"):
                raise ValueError(
                    f"wait_probe must be zlen or read, got {value!r}")
            self.__probe_kind = value

    # --- Lifecycle ---

    async def start(self):
        await self.__dev.open()

        funcs = ctypes.c_ulong()
        self.__dev.call(I2C_FUNCS, funcs)
        self.__funcs = funcs.value
        if not self.__funcs & I2C_FUNC_I2C:
            raise RuntimeError(
                f"{self.path}: adapter reports no I2C_FUNC_I2C "
                f"(funcs 0x{self.__funcs:08x}); it is an SMBus-only "
                f"controller and I2C_RDWR is unavailable")

        if self.__retries is not None:
            self.__dev.call(I2C_RETRIES, self.__retries)
        if self.__timeout_s is not None:
            # The kernel counts in units of 10 ms.
            self.__dev.call(I2C_TIMEOUT, max(1, round(self.__timeout_s * 100)))

        self.__clock_hz = self.__info.clock_hz
        bus_name = self.__info.name
        self.metadata.update(path=self.path, name=bus_name,
                             funcs=self.__funcs, clock_hz=self.__clock_hz)
        self.logger.note(
            "%s: %s, funcs 0x%08x, clock %s", self.path,
            bus_name or "unnamed",  self.__funcs,
            metric(self.__clock_hz, "Hz") if self.__clock_hz else "unknown")
        self.freq_reapply()

    async def stop(self):
        self.__stopping = True
        await self.__dev.close()

    def freq_update(self, freq):
        # i2c-dev has no rate control at all -- the bus clock comes
        # from the controller's device tree. Reporting the requested
        # frequency would be a fiction, so report what the bus runs at
        # (or nothing) and say so when a cap cannot be honoured.
        if (freq is not None and self.__clock_hz is not None
                and freq < self.__clock_hz):
            self.logger.warning(
                "%s: bus clock is fixed at %s by the kernel driver; "
                "cap of %s ignored", self.path,
                metric(self.__clock_hz, "Hz"), metric(freq, "Hz"))
        return self.__clock_hz

    # --- Lowering ---

    async def flush_ops(self, batch):
        self.dispatch([(NormalizedTransaction(*self.normalize(op)), future)
                       for op, future in batch])

    async def run_ops(self, batch):
        outcomes = await asyncio.to_thread(self.__execute, batch)
        for (entry, future), outcome in zip(batch, outcomes):
            if future is None or future.done():
                continue
            if isinstance(outcome, Exception):
                future.set_exception(outcome)
            else:
                future.set_result(outcome[0] if entry.single else outcome)

    def __execute(self, batch):
        """Run every transaction in the batch; returns one outcome each.

        An outcome is the per-item result tuple, or the exception that
        stopped that transaction. A failure is confined to its own
        transaction: an I²C transaction ends with a STOP, so a NACK
        leaves the bus in a defined state and its siblings are still
        safe to run.
        """
        return [self.__transaction(entry.transaction) for entry, _ in batch]

    def __transaction(self, transaction):
        results = []
        for item in transaction.items:
            try:
                results.append(self.__item(item))
            except Exception as exc:
                # Cancel-on-failure: later items of this transaction
                # were predicated on this one.
                return exc
        return tuple(results)

    def __item(self, item):
        if isinstance(item, i2c.Transfer):
            return self.__transfer(item)
        if isinstance(item, i2c.WaitAck):
            return self.__wait_ack(item)
        raise TypeError(f"I2cdevInterface cannot lower {type(item).__name__}")

    def __transfer(self, transfer):
        if len(transfer.data_w) > MAX_MSG_LEN or transfer.size_r > MAX_MSG_LEN:
            raise ValueError(
                f"{self.path}: i2c_msg length is 16-bit; "
                f"{transfer!r} does not fit")
        msgs = []
        read_buffer = None
        if transfer.data_w:
            write_buffer = ctypes.create_string_buffer(
                transfer.data_w, len(transfer.data_w))
            msgs.append((0, write_buffer))
        if transfer.size_r:
            read_buffer = ctypes.create_string_buffer(transfer.size_r)
            msgs.append((I2C_M_RD, read_buffer))
        self.__rdwr(transfer.addr, msgs)
        return read_buffer.raw if read_buffer is not None else None

    def __rdwr(self, addr: int, msgs):
        """Issue one I2C_RDWR carrying `msgs` as [(flags, buffer), ...]."""
        array = (I2cMsg * len(msgs))()
        for index, (flags, buffer) in enumerate(msgs):
            array[index].addr = addr
            array[index].flags = flags
            array[index].len = len(buffer)
            array[index].buf = ctypes.addressof(buffer)
        request = I2cRdwrIoctlData(msgs=ctypes.addressof(array),
                                   nmsgs=len(msgs))
        try:
            done = self.__dev.call(I2C_RDWR, request)
        except OSError as exc:
            raise self.__translate(exc, addr) from exc
        if done != len(msgs):
            # i2c_transfer reports how many messages landed. Nothing
            # through means the addressing failed.
            raise (i2c.AddressNack(addr) if done == 0
                   else i2c.DataNack(addr))

    def __translate(self, exc: OSError, addr: int) -> Exception:
        # Which of the two a controller reports for an address NACK is
        # driver convention, not ABI: i2c-designware says ENXIO where
        # i2c-bcm2835 says EREMOTEIO for the same event.
        if exc.errno == errno.ENXIO:
            return i2c.AddressNack(addr)
        if exc.errno == errno.EREMOTEIO:
            return i2c.DataNack(addr)
        return OSError(exc.errno,
                       f"{self.path}: I2C_RDWR at 0x{addr:02x}: "
                       f"{exc.strerror}")

    # --- Ack polling ---

    def __wait_ack(self, wait):
        deadline = time.monotonic() + wait.timeout_s
        interval = (self.DEFAULT_WAIT_INTERVAL if wait.interval_s is None
                    else wait.interval_s)
        while True:
            if self.__probe(wait.addr):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise i2c.WaitAckTimeout(wait.addr, wait.timeout_s)
            if self.__stopping:
                raise RuntimeError(
                    f"{self.path}: closed while waiting for "
                    f"0x{wait.addr:02x} to acknowledge")
            time.sleep(min(interval, remaining))

    def __probe(self, addr: int) -> bool:
        """One address probe. True when the slave acknowledged."""
        while True:
            try:
                self.__rdwr(addr, self.__probe_msgs())
                return True
            except (i2c.AddressNack, i2c.DataNack):
                return False
            except OSError as exc:
                if (exc.errno != errno.EOPNOTSUPP
                        or self.__probe_kind != "zlen"):
                    raise
                # A zero-length write is the exact primitive, but some
                # controllers (i2c-designware and friends) declare
                # I2C_AQ_NO_ZERO_LEN, and that quirk is not visible in
                # I2C_FUNCS or anywhere else in userspace. Trying is
                # the only way to find out, so remember the answer.
                self.__probe_kind = "read"
                self.logger.note(
                    "%s: controller rejects zero-length writes; "
                    "probing with a 1-byte read instead", self.path)

    def __probe_msgs(self):
        if self.__probe_kind == "zlen":
            return [(0, ctypes.create_string_buffer(0))]
        return [(I2C_M_RD, ctypes.create_string_buffer(1))]


class I2cdevAdapter(Adapter):
    """One ``/dev/i2c-N`` bus.

    Holds no fd: the interface child owns it, so listing a desktop's
    dozen display-DDC buses opens nothing.
    """

    def __init__(self, name: str, path: str, info=None):
        super().__init__(name)
        self.__path = path
        self.__info = info if info is not None else I2cBusInfo(path)

    @property
    def ident(self):
        # The driver's label is what tells a board's real bus apart
        # from the GPU's DDC channels in `info adapters`.
        name = self.__info.name
        return f"{self.__path} ({name})" if name else self.__path

    def child_hints(self):
        return ["i2c"]

    async def child_spawn(self, name):
        if name.lower() == "i2c":
            return I2cdevInterface(self.__path, name="i2c", info=self.__info)
        raise NoMatch("interface", name)


@enumerator_db.register("i2cdev")
class I2cdevEnumerator(Enumerator):
    """Attaches one `I2cdevAdapter` per ``/dev/i2c-*`` bus.

    Every bus is listed, display-DDC channels included: which of them
    is interesting is the user's call, and `ident` carries the
    driver's own label so the listing says which is which.
    """

    def __init__(self, dev_glob: str = "/dev/i2c-*",
                 sysfs_dir: str = SYSFS_CLASS_DIR,
                 devices_dir: str = SYSFS_DEVICES_DIR):
        self.__dev_glob = dev_glob
        self.__sysfs_dir = sysfs_dir
        self.__devices_dir = devices_dir

    def paths(self) -> list[str]:
        # /dev/i2c-10 must not sort before /dev/i2c-2.
        return sorted(glob.glob(self.__dev_glob), key=self.__order)

    @staticmethod
    def __order(path: str):
        suffix = os.path.basename(path).rsplit("-", 1)[-1]
        return (0, int(suffix)) if suffix.isdigit() else (1, suffix)

    async def populate(self, hw_root):
        for path in self.paths():
            name = os.path.basename(path)
            if hw_root.has_child(name):
                continue
            info = I2cBusInfo(path, sysfs_dir=self.__sysfs_dir,
                              devices_dir=self.__devices_dir)
            hw_root.child_add(I2cdevAdapter(name, path, info=info))
