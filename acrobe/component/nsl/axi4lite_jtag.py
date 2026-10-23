"""NSL ``axi4lite_jtag_transactor``: a JTAG master on a memory bus.

Path syntax::

    <memory bus>/nsl_jtag(fin=<f>[,base=<offset>])

e.g. ``devmem/0x43c00000:0x100/nsl_jtag(fin=100M)/chain``.

The IP wraps ``nsl_jtag.ate.jtag_ate`` behind 32-bit registers, word
index in ``addr[7:2]``. Every op is triggered by a write:

* ``DIVISOR`` (0): TCK half-period minus one, in ``fin`` cycles,
  truncated to the IP's prescaler width. Readable.
* ``RESET`` (1), ``RTI`` (2): ``cycles - 1`` in bits ``[4:0]``.
* ``SWD_TO_JTAG`` (3), ``DR_CAPTURE`` (4), ``IR_CAPTURE`` (5): value
  ignored.
* ``SHIFT1`` .. ``SHIFT32`` (32 .. 63): shift ``n`` bits of the written
  value, LSB first, with ``n`` taken from the register index.

A read of any register but ``DIVISOR`` returns the TDO of the last
shift, LSB-aligned. The IP holds write ready and read data until it is
idle, so bus ordering alone sequences a shift and the read of its TDO.

``fin`` is the IP clock frequency and is mandatory: the IP cannot
report it. ``base`` is the IP offset on the bus (default 0).
"""

from __future__ import annotations

import math

from ...bitstring import BitString
from ...protocol import jtag, memory
from ...util.pretty import sci_parse


@memory.Interface.child_db.register("nsl_jtag")
class Axi4LiteJtagTransactor(jtag.JtagInterface):
    """JTAG interface lowering bit-level ops to register accesses on
    the parent memory bus."""

    REG_DIVISOR = 0
    REG_RESET = 1
    REG_RTI = 2
    REG_SWD_TO_JTAG = 3
    REG_DR_CAPTURE = 4
    REG_IR_CAPTURE = 5
    REG_SHIFT1 = 32

    MAX_CYCLES = 32
    MAX_SHIFT = 32

    DEFAULT_FREQ = 1e6

    # The ATE only drives 14 of the 16 SWD-to-JTAG magic bits and
    # ignores the command unless the TAP is in Test-Logic-Reset, so the
    # switch is wrapped in line resets long enough to complete the
    # sequence on the target side.
    SWD_TO_JTAG_PREAMBLE_CYCLES = 50
    SWD_TO_JTAG_TRAILER_CYCLES = 53

    def __init__(self, bus, name: str = "nsl_jtag"):
        super().__init__(name)
        self.bus = bus
        self.base = 0
        self.fin: float | None = None
        self.divisor_max: int | None = None
        self.__divisor: int | None = None
        self.__divisor_dirty = False

    def option_set(self, key, value):
        if key == "fin":
            self.fin = float(sci_parse(value))
        elif key == "base":
            self.base = int(value, 0)

    async def start(self):
        if self.fin is None:
            raise ValueError(
                f"{self.fqdn}: nsl_jtag needs the fin= option "
                f"(IP clock frequency)")
        self.bus.write32(self.__addr(self.REG_DIVISOR), 0xffffffff)
        self.divisor_max = await self.bus.read32(
            self.__addr(self.REG_DIVISOR))
        self.metadata.update(fin=self.fin, base=self.base,
                             divisor_max=self.divisor_max)
        self.freq_reapply()
        await super().start()

    def freq_update(self, freq):
        if self.divisor_max is None:
            return 0.0
        if not freq:
            freq = self.DEFAULT_FREQ
        d = math.ceil(self.fin / 2.0 / float(freq)) - 1
        d = max(0, min(d, self.divisor_max))
        if d != self.__divisor:
            self.__divisor = d
            self.__divisor_dirty = True
        return self.fin / (2 * (d + 1))

    def __addr(self, reg: int) -> int:
        return self.base + 4 * reg

    def __write(self, reg: int, value: int):
        return self.bus.write32(self.__addr(reg), value)

    def __cycles(self, reg: int, count: int) -> list:
        futures = []
        while count > 0:
            n = min(count, self.MAX_CYCLES)
            futures.append(self.__write(reg, n - 1))
            count -= n
        return futures

    def __reset(self, count: int) -> list:
        return self.__cycles(self.REG_RESET, max(5, int(count)))

    def __shift(self, op) -> tuple[list, list]:
        """Post one shift; return (all bus futures, TDO read futures
        with their bit counts)."""
        tdi = op.tdi
        total = len(tdi)
        value = int(tdi)
        futures = []
        reads = []
        offset = 0
        while offset < total:
            n = min(total - offset, self.MAX_SHIFT)
            chunk = (value >> offset) & ((1 << n) - 1)
            futures.append(self.__write(self.REG_SHIFT1 + n - 1, chunk))
            if op.read_tdo:
                rf = self.bus.read32(self.__addr(self.REG_SHIFT1))
                futures.append(rf)
                reads.append((rf, n))
            offset += n
        return futures, reads

    def __lower(self, op) -> tuple[list, list]:
        if isinstance(op, jtag.Shift):
            futures, reads = self.__shift(op)
            futures += self.__cycles(self.REG_RTI, int(op.post_run))
            return futures, reads
        if isinstance(op, jtag.Reset):
            return self.__reset(op.count), []
        if isinstance(op, jtag.Run):
            return self.__cycles(self.REG_RTI, int(op.cycles)), []
        if isinstance(op, jtag.CaptureDr):
            return [self.__write(self.REG_DR_CAPTURE, 0)], []
        if isinstance(op, jtag.CaptureIr):
            return [self.__write(self.REG_IR_CAPTURE, 0)], []
        if isinstance(op, jtag.SwdToJtag):
            futures = self.__reset(self.SWD_TO_JTAG_PREAMBLE_CYCLES)
            futures.append(self.__write(self.REG_SWD_TO_JTAG, 0))
            futures += self.__reset(self.SWD_TO_JTAG_TRAILER_CYCLES)
            return futures, []
        raise TypeError(
            f"{type(self).__name__} cannot lower {type(op).__name__}")

    async def flush_ops(self, batch):
        if self.divisor_max is None:
            raise RuntimeError(f"{self.fqdn}: not started")
        pending = []
        if self.__divisor_dirty:
            pending.append(self.__write(self.REG_DIVISOR, self.__divisor))
            self.__divisor_dirty = False
        for op, future in batch:
            try:
                futures, reads = self.__lower(op)
            except Exception as exc:
                if future is not None:
                    future.set_exception(exc)
                continue
            self.__join(pending + futures, future,
                        self.__tdo_assembler(op, reads))
            pending = []

    @staticmethod
    def __tdo_assembler(op, reads):
        if not (isinstance(op, jtag.Shift) and op.read_tdo):
            return lambda: None

        def assemble():
            value = 0
            offset = 0
            for rf, n in reads:
                value |= (rf.result() & ((1 << n) - 1)) << offset
                offset += n
            return BitString(value, offset)
        return assemble

    @staticmethod
    def __join(futures, target, result):
        """Resolve ``target`` with ``result()`` once every bus future
        is done, or with the first bus error."""
        if not futures:
            if target is not None and not target.done():
                target.set_result(result())
            return
        left = len(futures)

        def done(_f):
            nonlocal left
            left -= 1
            if left or target is None or target.done():
                return
            for f in futures:
                if f.cancelled():
                    target.cancel()
                    return
                if f.exception() is not None:
                    target.set_exception(f.exception())
                    return
            try:
                target.set_result(result())
            except BaseException as e:   # noqa: BLE001 — forwarded to the future
                target.set_exception(e)

        for f in futures:
            f.add_done_callback(done)
