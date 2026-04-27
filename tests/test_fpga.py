import asyncio
import struct
import pytest

from acrobe.component.fpga import SramFpga, JtagSramFpga
from acrobe.target.fpga import FpgaTarget
from acrobe.target import Target
from acrobe.component.xilinx.config_access_port import ConfigAccessPort
from acrobe.component.xilinx.series6 import Series6
from acrobe.component.xilinx.series7 import Series7
from acrobe.component.xilinx.spartan6 import Spartan6
from acrobe.component.xilinx.artix7 import Artix7
from acrobe.component.xilinx.spartan7 import Spartan7
from acrobe.component.xilinx.kintex7 import Kintex7
from acrobe.component.xilinx.zynq import Zynq
from acrobe.protocol.jtag import (
    Tap, Chain, Shift, CaptureDr, CaptureIr, Reset, Run,
    Dr, Instruction, TapInstruction,
)
from acrobe.bitstring import BitString
from acrobe.engine import Batcher
from acrobe.node import Node, Readable
from acrobe.endian import swib_u16, swib_u32


class _Bitstream(Node, Readable):
    """Test helper: a Readable Node holding fixed bytes,
    optionally with a Node parent carrying metadata (mirrors what
    populate_format produces in real walks)."""

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self._data = bytes(data)

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        return self._data[offset:offset + size]


def make_bitstream(data: bytes, **metadata) -> _Bitstream:
    """Build a (parent, leaf) tree where leaf is a Readable
    bitstream whose parent carries `metadata` — matches the shape
    FPGA.load() expects (source.parent.metadata for hints)."""
    parent = Node("file")
    parent._metadata.update(metadata)
    leaf = _Bitstream("bitstream", data)
    parent._child_attach(leaf)
    return leaf


# -- Mock Interface with IR status support --

class FpgaMockInterface(Batcher):
    """Mock interface that tracks IR status bits per device."""

    def __init__(self, ir_status_val=0):
        super().__init__()
        self.ops = []
        self.ir_status_val = ir_status_val

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_tdo:
                op.tdo = BitString(self.ir_status_val, len(op.tdi))
            future.set_result(op)


# -- SramFpga abstract API --

class TestSramFpga:
    def test_is_component(self):
        fpga = SramFpga("test")
        assert fpga.name == "test"

    @pytest.mark.asyncio
    async def test_load_raises(self):
        fpga = SramFpga("test")
        with pytest.raises(NotImplementedError):
            await fpga.load(make_bitstream(b""))

    @pytest.mark.asyncio
    async def test_erase_raises(self):
        fpga = SramFpga("test")
        with pytest.raises(NotImplementedError):
            await fpga.erase()

    @pytest.mark.asyncio
    async def test_is_configured_raises(self):
        fpga = SramFpga("test")
        with pytest.raises(NotImplementedError):
            await fpga.is_configured()


class TestJtagSramFpga:
    def test_user_ir_default(self):
        fpga = JtagSramFpga("test")
        assert fpga.USER_IR == []


# -- FpgaTarget --

class TestFpgaTarget:
    def test_registered(self):
        """FpgaTarget is registered for SramFpga in Target explorers."""
        types = [e.component_types for e in Target._explorers]
        assert any(SramFpga in ct for ct in types)

    @pytest.mark.asyncio
    async def test_write_delegates_load(self):
        loaded = []

        class MockFpga(SramFpga):
            async def load(self, source):
                loaded.append(source)

            async def erase(self):
                pass

            async def is_configured(self):
                return True

        fpga = MockFpga("mock")
        target = FpgaTarget(fpga)
        await target.write(make_bitstream(b'\x00' * 100))
        assert len(loaded) == 1

    @pytest.mark.asyncio
    async def test_write_with_erase(self):
        erased = []

        class MockFpga(SramFpga):
            async def load(self, source):
                pass

            async def erase(self):
                erased.append(True)

            async def is_configured(self):
                return True

        fpga = MockFpga("mock")
        target = FpgaTarget(fpga)
        await target.write(make_bitstream(b'\x00'), do_erase=True)
        assert len(erased) == 1

    @pytest.mark.asyncio
    async def test_erase_all_delegates(self):
        erased = []

        class MockFpga(SramFpga):
            async def load(self, source):
                pass

            async def erase(self):
                erased.append(True)

            async def is_configured(self):
                return True

        fpga = MockFpga("mock")
        target = FpgaTarget(fpga)
        await target.erase_all()
        assert len(erased) == 1

    @pytest.mark.asyncio
    async def test_verify_delegates(self):
        class MockFpga(SramFpga):
            async def load(self, source):
                pass

            async def erase(self):
                pass

            async def is_configured(self):
                return True

        fpga = MockFpga("mock")
        target = FpgaTarget(fpga)
        assert await target.verify(make_bitstream(b"")) is True


# -- ConfigAccessPort mixin --

class TestConfigAccessPort:
    @pytest.mark.asyncio
    async def test_send_op_wait_success(self):
        """send_op_wait returns True when expected bits match."""
        # done=True means bit 5 set -> ir_status value 0b100000 = 0x20
        iface = FpgaMockInterface(ir_status_val=0x20)
        tap = Series7(iface, idcode=0x03631093)
        result = await tap.send_op_wait(-1, done=True)
        assert result is True

    @pytest.mark.asyncio
    async def test_send_op_wait_failure(self):
        """send_op_wait returns False when bits never match."""
        iface = FpgaMockInterface(ir_status_val=0x00)
        tap = Series7(iface, idcode=0x03631093)
        result = await tap.send_op_wait(-1, done=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_op_wait_with_ir_shift(self):
        """send_op_wait with a real IR value shifts that instruction first."""
        iface = FpgaMockInterface(ir_status_val=0x10)  # init=True
        tap = Series6(iface, idcode=0x04001093)
        result = await tap.send_op_wait(int(tap.IR_JPROGRAM), init=True)
        assert result is True


# -- Series6 --

class TestSeries6:
    def test_class_attributes(self):
        assert Series6.irlen == 6
        assert Series6.USER_IR == [0x02, 0x03, 0x1a, 0x1b]
        assert Series6.CFG_PREFIX == [0xaa99, 0x5566]

    def test_type1(self):
        # NOP
        assert Series6.type1(0, 0, 0) == [0x2000]
        # READ reg=8, count=1
        w = Series6.type1(1, 8, 1)[0]
        assert w == (1 << 13) | (1 << 11) | (8 << 5) | 1

    def test_cfg_conv_roundtrip(self):
        words = [0xaa99, 0x5566, 0x1234]
        blob = Series6._cfg_conv_tdi(words)
        back = Series6._cfg_conv_tdo(blob)
        assert back == words

    def test_is_jtag_sram_fpga(self):
        iface = FpgaMockInterface()
        tap = Series6(iface, idcode=0x04001093)
        assert isinstance(tap, JtagSramFpga)
        assert isinstance(tap, SramFpga)

    @pytest.mark.asyncio
    async def test_is_configured(self):
        # done bit is bit 5 = 0x20
        iface = FpgaMockInterface(ir_status_val=0x20)
        tap = Series6(iface, idcode=0x04001093)
        assert await tap.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_not_configured(self):
        iface = FpgaMockInterface(ir_status_val=0x00)
        tap = Series6(iface, idcode=0x04001093)
        assert await tap.is_configured() is False

    @pytest.mark.asyncio
    async def test_load_rejects_odd_length(self):
        iface = FpgaMockInterface(ir_status_val=0x30)
        tap = Series6(iface, idcode=0x04001093)
        with pytest.raises(ValueError, match="Odd"):
            await tap.load(make_bitstream(b'\x00' * 11))


# -- Series7 --

class TestSeries7:
    def test_class_attributes(self):
        assert Series7.irlen == 6
        assert Series7.USER_IR == [0x02, 0x03, 0x22, 0x23]
        assert Series7.CFG_PREFIX == [0xaa995566]

    def test_type1(self):
        w = Series7.type1(1, 4, 1)[0]
        assert w == (1 << 29) | (1 << 27) | (4 << 13) | 1

    def test_cfg_conv_roundtrip(self):
        words = [0xaa995566, 0x12345678, 0xdeadbeef]
        blob = Series7._cfg_conv_tdi(words)
        back = Series7._cfg_conv_tdo(blob)
        assert back == words

    @pytest.mark.asyncio
    async def test_load_rejects_non_aligned(self):
        iface = FpgaMockInterface()
        tap = Series7(iface, idcode=0x03631093)
        with pytest.raises(ValueError, match="not 32-bit aligned"):
            await tap.load(make_bitstream(b'\x00' * 13))


# -- Device Registration via ChainSimulator --

class ChainSimulator(Batcher):
    """Copy of test_jtag.ChainSimulator for FPGA device discovery tests."""

    def __init__(self, devices):
        super().__init__()
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
    async def test_spartan6_lx9(self):
        sim = ChainSimulator([(0x04001093, 6)])
        chain = Chain(sim)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Spartan6)
        assert isinstance(tap, Series6)
        assert "LX9" in tap.name

    @pytest.mark.asyncio
    async def test_artix7_100t(self):
        sim = ChainSimulator([(0x03631093, 6)])
        chain = Chain(sim)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Artix7)
        assert isinstance(tap, Series7)
        assert "XC7A100T" in tap.name

    @pytest.mark.asyncio
    async def test_spartan7_s25(self):
        sim = ChainSimulator([(0x037c4093, 6)])
        chain = Chain(sim)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Spartan7)
        assert "S25" in tap.name

    @pytest.mark.asyncio
    async def test_kintex7_160t(self):
        sim = ChainSimulator([(0x0364c093, 6)])
        chain = Chain(sim)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Kintex7)
        assert "160T" in tap.name

    @pytest.mark.asyncio
    async def test_zynq_020(self):
        sim = ChainSimulator([(0x03727093, 6)])
        chain = Chain(sim)
        await chain.discover()
        tap = chain.children[0]
        assert isinstance(tap, Zynq)
        assert "020" in tap.name

    @pytest.mark.asyncio
    async def test_multi_device_chain(self):
        """Two Xilinx devices in a chain are correctly identified."""
        sim = ChainSimulator([
            (0x04001093, 6),   # Spartan6 LX9
            (0x03631093, 6),   # Artix7 100T
        ])
        chain = Chain(sim)
        await chain.discover()
        assert len(chain.children) == 2
        assert isinstance(chain.children[0], Spartan6)
        assert isinstance(chain.children[1], Artix7)

    @pytest.mark.asyncio
    async def test_has_instructions(self):
        """Registered TAPs have expected instruction attributes."""
        iface = FpgaMockInterface()
        tap = Artix7(iface, idcode=0x03631093)
        assert isinstance(tap.BYPASS, TapInstruction)
        assert isinstance(tap.IDCODE, TapInstruction)
        assert isinstance(tap.IR_CFG_IN, TapInstruction)
        assert isinstance(tap.IR_CFG_OUT, TapInstruction)

    @pytest.mark.asyncio
    async def test_user_ir_series6(self):
        iface = FpgaMockInterface()
        tap = Spartan6(iface, idcode=0x04001093)
        assert tap.USER_IR == [0x02, 0x03, 0x1a, 0x1b]

    @pytest.mark.asyncio
    async def test_user_ir_series7(self):
        iface = FpgaMockInterface()
        tap = Zynq(iface, idcode=0x03727093)
        assert tap.USER_IR == [0x02, 0x03, 0x22, 0x23]


# -- Series6 load sequence test --

class TestSeries6LoadSequence:
    @pytest.mark.asyncio
    async def test_load_sequence(self):
        """Test the full Series6 load sequence with a mock that responds correctly."""
        call_count = [0]
        ir_status_responses = []

        class LoadMockInterface(Batcher):
            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        # For ir_status calls, return init=True first, then done=True
                        val = 0x10 if call_count[0] < 3 else 0x20
                        call_count[0] += 1
                        ir_status_responses.append(val)
                        op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = LoadMockInterface()
        tap = Series6(iface, idcode=0x04001093)

        # Build a minimal 16-bit-word bitstream (2 words = 4 bytes)
        await tap.load(make_bitstream(struct.pack(">2H", 0xaa99, 0x5566)))
        # Verify we had some IR status checks
        assert len(ir_status_responses) > 0


# -- UserID skip tests --

class UseridMockInterface(Batcher):
    """Mock that returns a configurable USERCODE and tracks reload attempts."""

    def __init__(self, usercode, done=True):
        super().__init__()
        self.usercode = usercode
        self.done = done
        self.shifts = []

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Shift) and op.read_tdo:
                self.shifts.append(op)
                # First read is USERCODE (32 bits), subsequent are ir_status (6 bits)
                if len(op.tdi) == 32:
                    op.tdo = BitString(self.usercode, 32)
                else:
                    val = 0x20 if self.done else 0x00  # done bit
                    op.tdo = BitString(val, len(op.tdi))
            future.set_result(op)


class TestSeries6UseridSkip:
    @pytest.mark.asyncio
    async def test_userid_match_skips_reload(self):
        """When userid matches and FPGA is configured, load() skips reprogramming."""
        iface = UseridMockInterface(usercode=0xCAFEBABE, done=True)
        tap = Series6(iface, idcode=0x04001093)

        bs = make_bitstream(
            struct.pack(">2H", 0xaa99, 0x5566), userid=0xCAFEBABE)
        await tap.load(bs)
        # Only USERCODE read + ir_status polls, no JPROGRAM reset
        # The JPROGRAM shift would add many more ops
        assert len(iface.shifts) <= 3

    @pytest.mark.asyncio
    async def test_userid_mismatch_reloads(self):
        """When userid doesn't match, full reload happens."""
        call_count = [0]

        class ReloadMockInterface(Batcher):
            def __init__(self):
                super().__init__()
                self.shifts = []

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        if len(op.tdi) == 32:
                            op.tdo = BitString(0xDEADBEEF, 32)
                        else:
                            val = 0x10 if call_count[0] < 3 else 0x20
                            call_count[0] += 1
                            op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = ReloadMockInterface()
        tap = Series6(iface, idcode=0x04001093)

        bs = make_bitstream(
            struct.pack(">2H", 0xaa99, 0x5566), userid=0xCAFEBABE)
        await tap.load(bs)
        # Full reload: USERCODE read + reset + cfg_shift + start
        assert len(iface.shifts) > 3

    @pytest.mark.asyncio
    async def test_no_userid_skips_check(self):
        """When program has no userid, no USERCODE read happens."""
        call_count = [0]

        class NoUseridMockInterface(Batcher):
            def __init__(self):
                super().__init__()
                self.shifts = []

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        val = 0x10 if call_count[0] < 3 else 0x20
                        call_count[0] += 1
                        op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = NoUseridMockInterface()
        tap = Series6(iface, idcode=0x04001093)

        # No userid in metadata
        await tap.load(make_bitstream(struct.pack(">2H", 0xaa99, 0x5566)))
        # No 32-bit USERCODE read
        assert all(len(s.tdi) != 32 for s in iface.shifts)

    @pytest.mark.asyncio
    async def test_userid_ffffffff_skips_check(self):
        """UserID 0xffffffff is treated as absent."""
        call_count = [0]

        class NoCheckMockInterface(Batcher):
            def __init__(self):
                super().__init__()
                self.shifts = []

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        val = 0x10 if call_count[0] < 3 else 0x20
                        call_count[0] += 1
                        op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = NoCheckMockInterface()
        tap = Series6(iface, idcode=0x04001093)

        bs = make_bitstream(
            struct.pack(">2H", 0xaa99, 0x5566), userid=0xffffffff)
        await tap.load(bs)
        # No 32-bit USERCODE read
        assert all(len(s.tdi) != 32 for s in iface.shifts)


class TestSeries7UseridSkip:
    @pytest.mark.asyncio
    async def test_userid_match_skips_reload(self):
        """When userid matches and FPGA is configured, load() skips reprogramming."""
        iface = UseridMockInterface(usercode=0xCAFEBABE, done=True)
        tap = Series7(iface, idcode=0x03631093)

        bs = make_bitstream(
            struct.pack(">2L", 0xaa995566, 0x20000000), userid=0xCAFEBABE)
        await tap.load(bs)
        assert len(iface.shifts) <= 3

    @pytest.mark.asyncio
    async def test_userid_mismatch_reloads(self):
        """When userid doesn't match, full reload happens."""
        call_count = [0]

        class ReloadMockInterface(Batcher):
            def __init__(self):
                super().__init__()
                self.shifts = []

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        self.shifts.append(op)
                        if len(op.tdi) == 32:
                            op.tdo = BitString(0xDEADBEEF, 32)
                        else:
                            val = 0x20 if call_count[0] >= 2 else 0x00
                            call_count[0] += 1
                            op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = ReloadMockInterface()
        tap = Series7(iface, idcode=0x03631093)

        bs = make_bitstream(
            struct.pack(">2L", 0xaa995566, 0x20000000), userid=0xCAFEBABE)
        await tap.load(bs)
        assert len(iface.shifts) > 3


# -- Series7 load sequence test --

class TestSeries7LoadSequence:
    @pytest.mark.asyncio
    async def test_load_sequence(self):
        """Test the full Series7 load sequence."""
        call_count = [0]

        class LoadMockInterface(Batcher):
            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        # Return done=True after a few calls
                        val = 0x20 if call_count[0] >= 2 else 0x00
                        call_count[0] += 1
                        op.tdo = BitString(val, len(op.tdi))
                    future.set_result(op)

        iface = LoadMockInterface()
        tap = Series7(iface, idcode=0x03631093)

        # Build a minimal 32-bit-word bitstream (2 words = 8 bytes)
        await tap.load(make_bitstream(
            struct.pack(">2L", 0xaa995566, 0x20000000)))
