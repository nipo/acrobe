"""nsl_jtag on a memory bus: lowering of JTAG ops to the register map
of NSL's axi4lite_jtag_transactor.

The fake bus models the IP's register side: DIVISOR is truncated to
the prescaler width, and a read of any other register returns the TDI
of the last shift (a TDI-to-TDO loopback), LSB-aligned.
"""

import pytest

from acrobe.bitstring import BitString
from acrobe.component.nsl.axi4lite_jtag import Axi4LiteJtagTransactor
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol import jtag, memory


class FakeJtagIp(memory.Interface, Batcher, Node):
    ops = memory.Interface.REGISTER_OPS

    def __init__(self, base=0, prescaler_width=18):
        Batcher.__init__(self)
        Node.__init__(self, "bus")
        self.base = base
        self.divisor_mask = (1 << prescaler_width) - 1
        self.divisor = self.divisor_mask
        self.last_shift = 0
        self.log = []           # ("R"/"W", reg index, value)
        self.fail = None

    async def flush_ops(self, batch):
        for op, future in batch:
            if self.fail is not None:
                future.set_exception(self.fail)
                continue
            reg = (op.addr - self.base) // 4
            assert 0 <= op.addr - self.base < 0x100
            if isinstance(op, memory.Write32):
                self.log.append(("W", reg, op.data))
                if reg == 0:
                    self.divisor = op.data & self.divisor_mask
                elif reg >= 32:
                    n = reg - 31
                    self.last_shift = op.data & ((1 << n) - 1)
                future.set_result(None)
            elif isinstance(op, memory.Read32):
                self.log.append(("R", reg, None))
                future.set_result(self.divisor if reg == 0
                                  else self.last_shift)
            else:
                raise AssertionError(op)

    def writes(self):
        return [(reg, value) for kind, reg, value in self.log
                if kind == "W"]


async def make(fin="100M", base=None, fmax=None, **kw):
    bus = FakeJtagIp(base=base or 0, **kw)
    iface = Axi4LiteJtagTransactor(bus)
    if fin is not None:
        iface.option_set("fin", fin)
    if base is not None:
        iface.option_set("base", hex(base))
    if fmax is not None:
        iface.freq_cap("user", fmax)
    await iface.start()
    # Drop the probe and the initial divisor write.
    await iface.post(jtag.Run(0))
    bus.log.clear()
    return bus, iface


class TestStart:
    async def test_fin_required(self):
        iface = Axi4LiteJtagTransactor(FakeJtagIp())
        with pytest.raises(ValueError, match="fin"):
            await iface.start()

    async def test_divisor_probe(self):
        bus = FakeJtagIp(prescaler_width=12)
        iface = Axi4LiteJtagTransactor(bus)
        iface.option_set("fin", "100M")
        await iface.start()
        assert bus.log[:2] == [("W", 0, 0xffffffff), ("R", 0, None)]
        assert iface.divisor_max == 0xfff

    async def test_default_freq(self):
        bus, iface = await make()
        assert iface.freq == 1e6
        assert bus.divisor == 49


class TestFrequency:
    @pytest.mark.parametrize("fmax, divisor, actual", [
        (10e6, 4, 10e6),
        (3e6, 16, 100e6 / 34),
        (50e6, 0, 50e6),
        (200e6, 0, 50e6),
        (1, 0x3ffff, 100e6 / (2 * 0x40000)),
    ])
    async def test_divisor(self, fmax, divisor, actual):
        bus, iface = await make(fmax=fmax)
        assert bus.divisor == divisor
        assert iface.freq == pytest.approx(actual)

    async def test_change_applies_before_next_op(self):
        bus, iface = await make()
        iface.freq_cap("user", 10e6)
        await iface.post(jtag.CaptureDr())
        assert bus.writes() == [(0, 4), (4, 0)]


class TestLowering:
    async def test_reset_split(self):
        bus, iface = await make()
        await iface.post(jtag.Reset(70))
        assert bus.writes() == [(1, 31), (1, 31), (1, 5)]

    async def test_reset_minimum(self):
        bus, iface = await make()
        await iface.post(jtag.Reset(2))
        assert bus.writes() == [(1, 4)]

    async def test_run_split(self):
        bus, iface = await make()
        await iface.post(jtag.Run(33))
        assert bus.writes() == [(2, 31), (2, 0)]

    async def test_run_zero(self):
        bus, iface = await make()
        await iface.post(jtag.Run(0))
        assert bus.log == []

    async def test_captures(self):
        bus, iface = await make()
        await iface.post(jtag.CaptureDr())
        await iface.post(jtag.CaptureIr())
        assert bus.writes() == [(4, 0), (5, 0)]

    async def test_swd_to_jtag(self):
        bus, iface = await make()
        await iface.post(jtag.SwdToJtag())
        assert bus.writes() == [
            (1, 31), (1, 17), (3, 0), (1, 31), (1, 20)]

    async def test_shift_chunks_and_tdo(self):
        bus, iface = await make()
        value = 0x2a_1234_5678_9abc_def0
        tdo = await iface.post(jtag.Shift(BitString(value, 70)))
        assert bus.log == [
            ("W", 63, 0x9abcdef0), ("R", 32, None),
            ("W", 63, 0x12345678), ("R", 32, None),
            ("W", 37, 0x2a), ("R", 32, None),
        ]
        assert tdo == BitString(value, 70)

    async def test_shift_without_tdo(self):
        bus, iface = await make()
        tdo = await iface.post(
            jtag.Shift(BitString(5, 3), read_tdo=False, post_run=2))
        assert tdo is None
        assert bus.log == [("W", 34, 5), ("W", 2, 1)]

    async def test_base_offset(self):
        bus, iface = await make(base=0x400)
        await iface.post(jtag.Shift(BitString(1, 1)))
        await iface.post(jtag.Reset(5))
        assert bus.log == [("W", 32, 1), ("R", 32, None), ("W", 1, 4)]

    async def test_bus_error_propagates(self):
        bus, iface = await make()
        bus.fail = OSError("bus fault")
        with pytest.raises(OSError, match="bus fault"):
            await iface.post(jtag.Shift(BitString(1, 1)))
        with pytest.raises(OSError, match="bus fault"):
            await iface.post(jtag.Reset(5))


class TestRegistration:
    def test_registered_on_memory_bus(self):
        assert "nsl_jtag" in memory.Interface.child_db.registry
