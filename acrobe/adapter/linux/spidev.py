"""SPI master through the kernel's ``spidev`` character device.

The kernel demultiplexes chip select before it creates the node: a
``/dev/spidevB.C`` *is* one slave on bus B at select C. So one node
maps to one `spi.Interface` carrying exactly one `spi.Target` at
``cs0``. Re-aggregating ``/dev/spidevB.*`` into a synthetic
multi-select interface would invent a topology the kernel deliberately
split, and would claim an atomicity across selects that two
independent ``spi_device`` s do not have.

Shifts posted with no bracketing `Cs` are still honoured: they go out
in their own message with ``SPI_NO_CS`` set. Whether chip select
actually stays put is the controller's call -- the SPI core accepts
the bit generically, but a controller driving CS in hardware per word
may toggle it anyway. Pass ``no_cs=false`` to stop asking.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import glob
import os

from ...db import NoMatch
from ...engine import BackgroundLowering
from ...protocol import spi
from ...util.pretty import bool_parse
from ..model import Adapter, Enumerator, enumerator_db
from .char_device import CharDevice
from .ioctl import Ioctl


SPI_IOC_MAGIC = "k"

# SPI_IOC_MESSAGE(n) is built where it is issued: its size field
# carries the transfer count.
SPI_IOC_RD_MAX_SPEED_HZ = Ioctl.read(SPI_IOC_MAGIC, 4, 4)
SPI_IOC_RD_MODE32 = Ioctl.read(SPI_IOC_MAGIC, 5, 4)
SPI_IOC_WR_MODE32 = Ioctl.write(SPI_IOC_MAGIC, 5, 4)

# linux/spi/spi.h. CPOL|CPHA is acrobe's Cs.mode verbatim.
SPI_CPHA = 0x01
SPI_CPOL = 0x02
SPI_MODE_MASK = SPI_CPOL | SPI_CPHA
SPI_CS_HIGH = 0x04
SPI_LSB_FIRST = 0x08
SPI_NO_CS = 0x40

BUFSIZ_PARAM = "/sys/module/spidev/parameters/bufsiz"


class SpiIocTransfer(ctypes.Structure):
    """``struct spi_ioc_transfer``.

    The header guarantees one layout for 32- and 64-bit userspace, so
    natural alignment is right on both.
    """

    _fields_ = [
        ("tx_buf", ctypes.c_uint64),
        ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16),
        ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8),
        ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


# SPI_IOC_MESSAGE(n) puts n * sizeof(struct spi_ioc_transfer) in the
# request's size field, so a wrong size would address a different
# ioctl rather than fail.
assert ctypes.sizeof(SpiIocTransfer) == 32, ctypes.sizeof(SpiIocTransfer)

# 14-bit size field; this is the kernel's SPI_MSGSIZE cliff.
MAX_TRANSFERS = ((1 << Ioctl.SIZEBITS) - 1) // ctypes.sizeof(SpiIocTransfer)


class SpiMessage:
    """One ``SPI_IOC_MESSAGE``: a transfer array plus its buffers.

    MOSI for every transfer is staged into one contiguous buffer and
    MISO into another, so a message costs two allocations however many
    transfers it holds.
    """

    def __init__(self, mode: int):
        self.mode = mode
        self.total = 0
        self.transfers = None
        self.rx = None
        self.__mosi = []
        self.__reads = []

    def __len__(self):
        return len(self.__mosi)

    def append(self, mosi: bytes, read: bool) -> int:
        """Add one transfer; returns its offset in the MISO buffer."""
        offset = self.total
        self.__mosi.append(mosi)
        self.__reads.append(read)
        self.total += len(mosi)
        return offset

    def hold_cs(self):
        """Keep chip select asserted past the end of this message.

        The SPI core reads ``cs_change`` on a message's last transfer
        as "stay selected", which is what lets one CS-held group span
        several ioctls when it outgrows ``bufsiz``.
        """
        self.transfers[len(self) - 1].cs_change = 1

    @property
    def holds_cs(self) -> bool:
        return bool(self.transfers[len(self) - 1].cs_change)

    def build(self, speed_hz: int):
        # Both buffers stay referenced by the message for as long as
        # the kernel may read or write through tx_buf / rx_buf.
        self.__tx = ctypes.create_string_buffer(b"".join(self.__mosi),
                                                self.total)
        self.rx = ctypes.create_string_buffer(self.total)
        self.transfers = (SpiIocTransfer * len(self))()
        tx_base = ctypes.addressof(self.__tx)
        rx_base = ctypes.addressof(self.rx)
        offset = 0
        for index, mosi in enumerate(self.__mosi):
            xfer = self.transfers[index]
            xfer.tx_buf = tx_base + offset
            xfer.rx_buf = rx_base + offset if self.__reads[index] else 0
            xfer.len = len(mosi)
            xfer.speed_hz = speed_hz
            # Pinned rather than left at the device-tree default: any
            # other word size silently reinterprets a byte-wise Shift.
            xfer.bits_per_word = 8
            offset += len(mosi)

    @property
    def request(self) -> int:
        return Ioctl.write(SPI_IOC_MAGIC, 0,
                           len(self) * ctypes.sizeof(SpiIocTransfer))


class SetMode:
    """Planned step writing the device mode word before a message."""

    def __init__(self, mode: int):
        self.mode = mode


class PlannedShift:
    """One batch `Shift` and the message fragments carrying its MISO."""

    __slots__ = ("op", "future", "mosi", "frags")

    def __init__(self, op, future, mosi):
        self.op = op
        self.future = future
        self.mosi = mosi
        self.frags = [] if op.read_miso else None


class ChipSelectRun:
    """Shifts that share one chip-select assertion and one mode."""

    def __init__(self, mode: int, holds_cs: bool):
        self.mode = mode
        self.holds_cs = holds_cs
        self.shifts = []


class SpidevInterface(spi.Interface, BackgroundLowering):
    """SPI bus behind one ``/dev/spidevB.C`` node."""

    DEFAULT_BUFSIZ = 4096

    def __init__(self, path: str, name: str = "spi", *, device=None):
        # adapter=None: this interface owns the wire.
        spi.Interface.__init__(self, None, name)
        self.__dev = device if device is not None else CharDevice(path)
        self.__target = spi.Target(self, cs=0, mode=0, name="cs0")
        self.child_add(self.__target)
        self.__cs_high = None
        self.__no_cs = True
        self.__base_mode = 0
        self.__wire_mode = None
        self.__group_mode = None
        self.__cs_held = False
        self.__speed_hz = 0
        self.__bufsiz = self.DEFAULT_BUFSIZ

    @property
    def path(self) -> str:
        return self.__dev.path

    def option_set(self, key, value):
        if key == "mode":
            mode = int(str(value), 0)
            if not 0 <= mode <= 3:
                raise ValueError(f"spi mode must be 0..3, got {mode}")
            self.__target.mode = mode
        elif key == "cs_high":
            self.__cs_high = bool_parse(value)
        elif key == "no_cs":
            self.__no_cs = bool_parse(value)

    # --- Lifecycle ---

    async def start(self):
        await self.__dev.open()

        mode = ctypes.c_uint32()
        self.__dev.call(SPI_IOC_RD_MODE32, mode)
        # Keep what the device tree set (SPI_3WIRE, SPI_CS_HIGH, ...)
        # and own only the bits a Cs op decides.
        self.__base_mode = mode.value & ~(SPI_MODE_MASK | SPI_NO_CS
                                          | SPI_LSB_FIRST)
        if self.__cs_high is not None:
            self.__base_mode = (self.__base_mode | SPI_CS_HIGH
                                if self.__cs_high
                                else self.__base_mode & ~SPI_CS_HIGH)
        self.__wire_mode = None

        speed = ctypes.c_uint32()
        self.__dev.call(SPI_IOC_RD_MAX_SPEED_HZ, speed)
        if speed.value:
            self.freq_cap("device", speed.value)

        self.__bufsiz = self.read_bufsiz()

        self.metadata.update(path=self.path, bufsiz=self.__bufsiz,
                             max_speed_hz=speed.value,
                             base_mode=self.__base_mode)
        self.logger.note("%s: max %d Hz, bufsiz %d B, mode bits 0x%02x",
                         self.path, speed.value, self.__bufsiz,
                         self.__base_mode)
        # An fmax= path option was recorded before the fd existed.
        self.freq_reapply()

    async def stop(self):
        if self.__cs_held:
            self.logger.warning(
                "%s: stopping with chip select asserted; a Cs() group "
                "was left open", self.path)
        await self.__dev.close()

    @staticmethod
    def read_bufsiz() -> int:
        with open(BUFSIZ_PARAM) as f:
            return int(f.read().strip())

    def freq_update(self, freq):
        # Stamped into every transfer rather than written with
        # SPI_IOC_WR_MAX_SPEED_HZ, so there is no ordering dependency
        # on the fd; 0 means "the device's own default" to spidev.
        # Returned unchanged because the controller rounds down
        # internally and no ioctl reports the rate it settled on.
        self.__speed_hz = 0 if freq is None else int(freq)
        return freq

    # --- Lowering ---

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        runs, plain = self.__group(batch)
        steps = self.__plan(runs)
        if steps:
            await asyncio.to_thread(self.__execute, steps)
        for run in runs:
            for shift in run.shifts:
                miso = (None if shift.frags is None
                        else b"".join(bytes(msg.rx[at:at + size])
                                      for msg, at, size in shift.frags))
                shift.op.miso = miso
                if shift.future is not None and not shift.future.done():
                    shift.future.set_result(miso)
        for future in plain:
            if future is not None and not future.done():
                future.set_result(None)

    def __group(self, batch):
        """Split a batch into chip-select runs.

        Validation happens here, before any ioctl. A group rejected
        halfway would leave the slave mid-transaction with no way back,
        so a malformed batch fails whole -- stricter than `SpiMpsse`,
        which can reject one op in place.

        A group left open by the previous batch continues into this
        one; `Cs` state is interface state, not batch state.
        """
        runs = []
        plain = []
        current = None
        for op, future in batch:
            if isinstance(op, spi.Cs):
                if current is not None:
                    current.holds_cs = False
                    current = None
                if op.value is None:
                    self.__group_mode = None
                elif op.value != 0:
                    raise ValueError(
                        f"{self.path} exposes chip select 0 only, "
                        f"got Cs({op.value})")
                else:
                    self.__group_mode = (self.__base_mode
                                         | (op.mode & SPI_MODE_MASK))
                    current = ChipSelectRun(self.__group_mode, holds_cs=True)
                    runs.append(current)
                plain.append(future)
                continue

            if not isinstance(op, spi.Shift):
                raise TypeError(
                    f"SpidevInterface cannot lower {type(op).__name__}")

            if current is None:
                if self.__group_mode is not None:
                    current = ChipSelectRun(self.__group_mode, holds_cs=True)
                else:
                    mode = self.__base_mode | (self.__target.mode
                                               & SPI_MODE_MASK)
                    if self.__no_cs:
                        mode |= SPI_NO_CS
                    current = ChipSelectRun(mode, holds_cs=False)
                runs.append(current)
            current.shifts.append(
                PlannedShift(op, future, self.__mosi_bytes(op)))
        return runs, plain

    def __mosi_bytes(self, op) -> bytes:
        mosi = op.mosi
        if isinstance(mosi, (bytes, bytearray)):
            return bytes(mosi)
        if len(mosi) % 8:
            raise ValueError(
                f"{self.path} shifts whole bytes; got {len(mosi)} bits")
        return bytes(mosi)

    def __plan(self, runs):
        """Turn runs into an ordered list of SetMode / SpiMessage steps."""
        steps = []
        mode = self.__wire_mode
        for run in runs:
            messages = self.__messages(run)
            if not messages:
                if run.holds_cs:
                    self.logger.warning(
                        "%s: chip-select assertion carrying no data is "
                        "not expressible through spidev; ignored",
                        self.path)
                continue
            if run.mode != mode:
                steps.append(SetMode(run.mode))
                mode = run.mode
            for index, message in enumerate(messages):
                message.build(self.__speed_hz)
                # Every message but the last hands CS to its successor;
                # the last one only holds if the group is still open.
                if index < len(messages) - 1 or run.holds_cs:
                    message.hold_cs()
            steps.extend(messages)
        return steps

    def __messages(self, run):
        """Chunk a run's shifts into bufsiz-bounded messages.

        spidev rejects a message whose transfers sum past ``bufsiz``,
        and the ioctl number itself cannot describe more than
        `MAX_TRANSFERS`, so both bound a message here.
        """
        messages = []
        message = SpiMessage(run.mode)
        for shift in run.shifts:
            position = 0
            while position < len(shift.mosi):
                room = self.__bufsiz - message.total
                if room == 0 or len(message) == MAX_TRANSFERS:
                    messages.append(message)
                    message = SpiMessage(run.mode)
                    room = self.__bufsiz
                size = min(room, len(shift.mosi) - position)
                at = message.append(
                    shift.mosi[position:position + size], shift.op.read_miso)
                if shift.frags is not None:
                    shift.frags.append((message, at, size))
                position += size
        if len(message):
            messages.append(message)
        return messages

    # --- Wire (worker thread) ---

    def __execute(self, steps):
        for step in steps:
            if isinstance(step, SetMode):
                self.__set_mode(step.mode)
            else:
                self.__send(step)

    def __set_mode(self, mode: int):
        if mode == self.__wire_mode:
            return
        try:
            self.__dev.call(SPI_IOC_WR_MODE32, ctypes.c_uint32(mode))
        except OSError:
            if not mode & SPI_NO_CS:
                raise
            # Whether a controller implements SPI_NO_CS is not
            # queryable from userspace; asking and watching it fail is
            # the only way to find out, so remember and stop asking.
            self.__no_cs = False
            self.logger.note(
                "%s: controller rejects SPI_NO_CS; shifts posted "
                "without Cs() will toggle chip select", self.path)
            mode &= ~SPI_NO_CS
            if mode == self.__wire_mode:
                return
            self.__dev.call(SPI_IOC_WR_MODE32, ctypes.c_uint32(mode))
        self.__wire_mode = mode

    def __send(self, message: SpiMessage):
        try:
            self.__dev.call(message.request, message.transfers)
        except OSError as exc:
            if exc.errno == errno.EMSGSIZE:
                raise OSError(
                    exc.errno,
                    f"{self.path}: {message.total} bytes in "
                    f"{len(message)} transfers exceeds spidev bufsiz "
                    f"{self.__bufsiz}") from exc
            raise OSError(
                exc.errno,
                f"{self.path}: SPI_IOC_MESSAGE({len(message)}): "
                f"{exc.strerror}") from exc
        self.__cs_held = message.holds_cs


class SpidevAdapter(Adapter):
    """One ``/dev/spidevB.C`` node.

    Holds no descriptor: the fd belongs to the interface child, so
    enumerating a board's spidev nodes opens nothing.
    """

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.__path = path

    @property
    def ident(self):
        return self.__path

    def child_hints(self):
        return ["spi"]

    async def child_spawn(self, name):
        if name.lower() == "spi":
            return SpidevInterface(self.__path, name="spi")
        raise NoMatch("interface", name)


@enumerator_db.register("spidev")
class SpidevEnumerator(Enumerator):
    """Attaches one `SpidevAdapter` per ``/dev/spidev*`` node."""

    def __init__(self, dev_glob: str = "/dev/spidev*"):
        self.__dev_glob = dev_glob

    def paths(self) -> list[str]:
        return sorted(glob.glob(self.__dev_glob))

    async def populate(self, hw_root):
        for path in self.paths():
            name = os.path.basename(path)
            if hw_root.has_child(name):
                continue
            hw_root.child_add(SpidevAdapter(name, path))
