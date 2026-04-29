import asyncio
import pytest

from acrobe.component.gowin.gw1n import GowinFpga, Gw1n, Gw2a, Gw5a, DONE_BIT, Gw1nStatus
from acrobe.component.fpga import JtagSramFpga, SramFpga
from acrobe.protocol.jtag import (
    Tap, Chain, Shift, CaptureDr, CaptureIr, Reset, Run,
    TapInstruction, JtagInterface,
)
from acrobe.bitstring import BitString
from acrobe.node import Node, Readable


def _attach_tap(iface, base, idcode, irlen=None):
    """Build a single-tap chain under iface and return the tap."""
    chain = Chain()
    iface.child_add(chain)
    if irlen is None:
        irlen = base.irlen
    return chain.tap_add(idcode, irlen=irlen, base=base)


class _Bitstream(Node, Readable):
    def __init__(self, name, data):
        super().__init__(name)
        self._data = bytes(data)
    @property
    def size(self): return len(self._data)
    async def read(self, offset, size): return self._data[offset:offset+size]


def make_bitstream(data, **metadata):
    parent = Node("file")
    parent._metadata.update(metadata)
    leaf = _Bitstream("bitstream", data)
    parent._child_attach(leaf)
    return leaf


# -- Mock Interface --

class GowinMockInterface(JtagInterface):
    """Mock that tracks shifts and returns configurable status/usercode."""

    def __init__(self, status=0, usercode=0):
        super().__init__(name="gowin-mock")
        self.status = status
        self.usercode = usercode
        self.shifts = []

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Shift) and op.read_tdo:
                self.shifts.append(op)
                if len(op.tdi) == 32:
                    # Could be STATUS_REGISTER or USERCODE_REG - both 32-bit.
                    # We track by count: first 32-bit read per sequence.
                    op.tdo = BitString(self._next_32bit_value(), 32)
                else:
                    op.tdo = BitString(0, len(op.tdi))
            future.set_result(op)

    def _next_32bit_value(self):
        # Alternate based on shift count: USERCODE first, then STATUS reads
        count_32 = sum(1 for s in self.shifts if len(s.tdi) == 32)
        if count_32 == 1:
            return self.usercode
        return self.status


class StatusOnlyMock(JtagInterface):
    """Mock that always returns a fixed status for 32-bit reads."""

    def __init__(self, status=0):
        super().__init__(name="status-only")
        self.status = status

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Shift) and op.read_tdo:
                if len(op.tdi) == 32:
                    op.tdo = BitString(self.status, 32)
                else:
                    op.tdo = BitString(0, len(op.tdi))
            future.set_result(op)


# -- Basic Properties --

class TestGowinFpga:
    def test_class_attributes(self):
        assert GowinFpga.irlen == 8
        assert GowinFpga.USER_IR == [0x42, 0x43]

    def test_is_jtag_sram_fpga(self):
        iface = StatusOnlyMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert isinstance(tap, JtagSramFpga)
        assert isinstance(tap, SramFpga)
        assert isinstance(tap, Tap)

    def test_name_from_parts(self):
        iface = StatusOnlyMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert tap.name == "GW5A-60"

    def test_name_unknown_part(self):
        iface = StatusOnlyMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0ffff81b)
        assert "0x0ffff81b" in tap.name

    def test_has_instructions(self):
        iface = StatusOnlyMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert isinstance(tap.ISC_ENABLE, TapInstruction)
        assert isinstance(tap.ISC_DISABLE, TapInstruction)
        assert isinstance(tap.READ_STATUS, TapInstruction)
        assert isinstance(tap.USERCODE, TapInstruction)
        assert isinstance(tap.IDCODE, TapInstruction)

    def test_done_bit(self):
        assert DONE_BIT == 13

    def test_is_done(self):
        iface = StatusOnlyMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert tap.is_done(Gw1nStatus(1 << 13)) is True
        assert tap.is_done(Gw1nStatus(0)) is False
        assert tap.is_done(Gw1nStatus(0xffffffff)) is True


# -- Status Read --

class TestStatusRead:
    @pytest.mark.asyncio
    async def test_status_read(self):
        status_val = (1 << DONE_BIT) | 0x07
        iface = StatusOnlyMock(status=status_val)
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        st = await tap.status_read()
        assert int(st) == status_val
        assert st.Done is True
        assert st.CRCError is True
        assert st.BadCommand is True
        assert st.IdError is True


# -- is_configured --

class TestIsConfigured:
    @pytest.mark.asyncio
    async def test_configured(self):
        iface = StatusOnlyMock(status=(1 << DONE_BIT))
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert await tap.is_configured() is True

    @pytest.mark.asyncio
    async def test_not_configured(self):
        iface = StatusOnlyMock(status=0)
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        assert await tap.is_configured() is False


# -- Load with usercode skip --

class TestLoad:
    @pytest.mark.asyncio
    async def test_usercode_match_skips(self):
        """When usercode matches and Done is set, load() skips reprogramming."""
        status_val = 1 << DONE_BIT

        class SkipMock(JtagInterface):
            def __init__(self):
                super().__init__(name="skip-mock")
                self.shifts = []

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        if len(op.tdi) == 32:
                            # First call: USERCODE, subsequent: STATUS
                            if len(self.shifts) == 1:
                                op.tdo = BitString(0xDEADBEEF, 32)
                            else:
                                op.tdo = BitString(status_val, 32)
                        else:
                            op.tdo = BitString(0, len(op.tdi))
                    future.set_result(op)

        iface = SkipMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)

        await tap.load(make_bitstream(b'\x00' * 100, UserCode="0xDEADBEEF"))
        # Only USERCODE + STATUS reads, no large data shift
        assert len(iface.shifts) <= 3

    @pytest.mark.asyncio
    async def test_usercode_mismatch_reloads(self):
        """When usercode doesn't match, full reload happens."""

        class ReloadMock(JtagInterface):
            def __init__(self):
                super().__init__(name="reload-mock")
                self.shifts = []
                self._32bit_count = 0

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        if len(op.tdi) == 32:
                            self._32bit_count += 1
                            if self._32bit_count == 1:
                                # USERCODE read
                                op.tdo = BitString(0x12345678, 32)
                            elif self._32bit_count <= 4:
                                # Status during erase: Done=0
                                op.tdo = BitString(0, 32)
                            else:
                                # Status after configure: Done=1
                                op.tdo = BitString(1 << DONE_BIT, 32)
                        else:
                            op.tdo = BitString(0, len(op.tdi))
                    future.set_result(op)

        iface = ReloadMock()
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)

        await tap.load(make_bitstream(b'\x00' * 100, UserCode="0xDEADBEEF"))
        # Full reload has many more shifts (data transfer)
        assert len(iface.shifts) > 3


# -- Erase --

class TestErase:
    @pytest.mark.asyncio
    async def test_erase(self):
        """erase() delegates to sram_erase()."""
        # Status returns not-Done (0) so erase succeeds on first try
        iface = StatusOnlyMock(status=0)
        tap = _attach_tap(iface, GowinFpga, idcode=0x0001481b)
        await tap.erase()


# -- Device Registration via ChainSimulator --

class ChainSimulator(JtagInterface):
    def __init__(self, devices):
        super().__init__(name="sim")
        self.devices = devices
        self._reg_val = 0
        self._reg_len = 0
        self._bypass = False
        self._in_ir = False

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Reset):
                self._bypass = False
                self._in_ir = False
            elif isinstance(op, CaptureDr):
                self._in_ir = False
                if self._bypass:
                    self._reg_val = 0
                    self._reg_len = len(self.devices)
                else:
                    val, pos = 0, 0
                    for idcode, _ in self.devices:
                        val |= idcode << pos
                        pos += 32
                    self._reg_val = val
                    self._reg_len = pos
            elif isinstance(op, CaptureIr):
                self._in_ir = True
                val, pos = 0, 0
                for _, irlen in self.devices:
                    val |= 1 << pos
                    pos += irlen
                self._reg_val = val
                self._reg_len = pos
            elif isinstance(op, Shift):
                self._do_shift(op)
            future.set_result(op)

    def _do_shift(self, op):
        L = self._reg_len
        N = len(op.tdi)
        tdi_val = int(op.tdi)

        if op.read_tdo:
            if N <= L:
                op.tdo = BitString(self._reg_val & ((1 << N) - 1), N)
            else:
                tdo_val = (self._reg_val | (tdi_val << L)) & ((1 << N) - 1)
                op.tdo = BitString(tdo_val, N)

        if L > 0 and N >= L:
            new_val = (tdi_val >> (N - L)) & ((1 << L) - 1)
            self._reg_val = new_val
            if self._in_ir and new_val == (1 << L) - 1:
                self._bypass = True


class TestDeviceRegistration:
    @pytest.mark.asyncio
    async def test_gw5a_60(self):
        """GW5A-60 (0x0001481b) discovered as Gw5a."""
        sim = ChainSimulator([(0x0001481b, 8)])
        chain = Chain(); sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Gw5a)
        assert isinstance(tap, GowinFpga)
        assert "GW5A-60" in tap.name

    @pytest.mark.asyncio
    async def test_gw1n_9(self):
        """GW1N-9 (part_no=0x1005) discovered as Gw1n."""
        idcode = (0x1005 << 12) | 0x81b
        sim = ChainSimulator([(idcode, 8)])
        chain = Chain(); sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Gw1n)
        assert "GW1N-9" in tap.name

    @pytest.mark.asyncio
    async def test_gw2a_18(self):
        """GW2A-18 (part_no=0x0000) discovered as Gw2a."""
        idcode = (0x0000 << 12) | 0x81b
        sim = ChainSimulator([(idcode, 8)])
        chain = Chain(); sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Gw2a)

    @pytest.mark.asyncio
    async def test_revision_masked(self):
        """IDCODE with different revision bits still matches."""
        # GW5A-60 with revision nibble = 0xF
        idcode = 0xF001481b
        sim = ChainSimulator([(idcode, 8)])
        chain = Chain(); sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Gw5a)

    @pytest.mark.asyncio
    async def test_mixed_chain(self):
        """Gowin + Xilinx in same chain."""
        from acrobe.component.xilinx.spartan6 import Spartan6
        sim = ChainSimulator([
            (0x0001481b, 8),   # GW5A-60
            (0x04001093, 6),   # Spartan6 LX9
        ])
        chain = Chain(); sim.child_add(chain)
        await chain.discover()
        assert len(chain.children) == 2
        assert isinstance(chain.children[0], Gw5a)
        assert isinstance(chain.children[1], Spartan6)
