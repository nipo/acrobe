import asyncio
import logging
import pytest
from acrobe.adapter.ftdi import mpsse_cmd
from acrobe.adapter.ftdi.mpsse import (
    Operation, SetBitsLow, SetBitsHigh, GetBitsLow, GetBitsHigh,
    Loopback, ClockDivisor, ClockDiv5, ThreePhase,
    ClockBits, ClockBytes, ShiftBits, ShiftBytes, ShiftTms,
    MpsseEngine,
)
from acrobe.bitstring import BitString

_test_logger = logging.getLogger("test.mpsse")


class TestMpsseCmdConstants:
    def test_flags(self):
        assert mpsse_cmd.WRITE_NEG == 0x01
        assert mpsse_cmd.BITS == 0x02
        assert mpsse_cmd.READ == 0x20
        assert mpsse_cmd.TMS == 0x40
        assert mpsse_cmd.MANAGEMENT == 0x80

    def test_management_commands(self):
        assert mpsse_cmd.SET_BITS_LOW == 0x80
        assert mpsse_cmd.GET_BITS_LOW == 0x81
        assert mpsse_cmd.SEND_IMMEDIATE == 0x87
        assert mpsse_cmd.CLK_DIV == 0x86


class TestGpioOps:
    def test_set_bits_low(self):
        op = SetBitsLow(0xab, 0xff)
        cmd, rsp_len, _ = op.cmd_data()
        assert cmd == bytes([0x80, 0xab, 0xff])
        assert rsp_len == 0

    def test_set_bits_high(self):
        op = SetBitsHigh(0x12, 0x34)
        cmd, rsp_len, _ = op.cmd_data()
        assert cmd == bytes([0x82, 0x12, 0x34])
        assert rsp_len == 0

    def test_get_bits_low(self):
        op = GetBitsLow()
        cmd, rsp_len, _ = op.cmd_data()
        assert cmd == bytes([0x81])
        assert rsp_len == 1
        op.rsp_handle(bytes([0x42]))
        assert op.value == 0x42

    def test_get_bits_high(self):
        op = GetBitsHigh()
        cmd, rsp_len, _ = op.cmd_data()
        assert cmd == bytes([0x83])
        assert rsp_len == 1
        op.rsp_handle(bytes([0x99]))
        assert op.value == 0x99


class TestConfigOps:
    def test_loopback_enable(self):
        cmd, _, _ = Loopback(True).cmd_data()
        assert cmd == bytes([mpsse_cmd.LOOPBACK_ENABLE])

    def test_loopback_disable(self):
        cmd, _, _ = Loopback(False).cmd_data()
        assert cmd == bytes([mpsse_cmd.LOOPBACK_DISABLE])

    def test_clock_divisor(self):
        op = ClockDivisor(3)
        cmd, _, _ = op.cmd_data()
        # divisor-1 = 2, little-endian
        assert cmd == bytes([mpsse_cmd.CLK_DIV, 2, 0])

    def test_clock_div5(self):
        cmd, _, _ = ClockDiv5(True).cmd_data()
        assert cmd == bytes([mpsse_cmd.CLK_DIV5_ENABLE])

    def test_clock_bits(self):
        op = ClockBits(5)
        cmd, rsp_len, cycles = op.cmd_data()
        assert cmd == bytes([mpsse_cmd.CLK_BITS, 4])  # count-1
        assert rsp_len == 0
        assert cycles == 5

    def test_clock_bytes(self):
        op = ClockBytes(100)
        cmd, rsp_len, cycles = op.cmd_data()
        assert cmd[0] == mpsse_cmd.CLK_BYTES
        c = 99  # count-1
        assert cmd[1] == c & 0xff
        assert cmd[2] == c >> 8
        assert cycles == 100


class TestShiftBits:
    def test_write_only(self):
        op = ShiftBits(0xab, 5, read=False)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 0
        # cmd byte should have BITS | LSB | WRITE_NEG | WRITE
        assert cmd[0] & mpsse_cmd.BITS
        assert cmd[0] & mpsse_cmd.WRITE
        assert not (cmd[0] & mpsse_cmd.READ)
        assert cmd[1] == 4  # count-1

    def test_read_write(self):
        op = ShiftBits(0x05, 3, read=True)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 1
        assert cmd[0] & mpsse_cmd.READ
        assert cmd[0] & mpsse_cmd.WRITE

    def test_rsp_handle_lsb(self):
        op = ShiftBits(0, 3, read=True, lsb_first=True)
        # MPSSE returns data right-justified for bit mode
        op.rsp_handle(bytes([0b11100000]))  # 3 bits shifted to high bits
        assert len(op.data) == 3
        assert int(op.data) == 0b111


class TestShiftBytes:
    def test_write_bytes(self):
        op = ShiftBytes(b"\xaa\xbb", read=False)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 0
        assert cmd[0] & mpsse_cmd.WRITE
        # length = 2-1 = 1, little-endian
        assert cmd[1:3] == b"\x01\x00"
        assert cmd[3:5] == b"\xaa\xbb"

    def test_read_only(self):
        op = ShiftBytes(4, read=True)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 4
        assert cmd[0] & mpsse_cmd.READ
        assert not (cmd[0] & mpsse_cmd.WRITE)

    def test_write_read_bitstring(self):
        bs = BitString(0xaabb, 16)
        op = ShiftBytes(bs, read=True)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 2
        assert cmd[0] & mpsse_cmd.WRITE
        assert cmd[0] & mpsse_cmd.READ
        # rsp_handle should return BitString
        op.rsp_handle(b"\xcc\xdd")
        assert isinstance(op.data, BitString)
        assert int(op.data) == 0xddcc

    def test_rsp_bytes_type(self):
        op = ShiftBytes(b"\x01\x02", read=True)
        op.rsp_handle(b"\x03\x04")
        assert isinstance(op.data, bytes)
        assert op.data == b"\x03\x04"


class TestShiftTms:
    def test_basic(self):
        op = ShiftTms(0b101, 3, tdi=1)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 0  # read=False by default
        assert cmd[0] & mpsse_cmd.TMS
        assert cmd[1] == 2  # count-1
        assert cmd[2] == (0b101 & 0x7f) | (1 << 7)  # data | tdi<<7

    def test_read(self):
        op = ShiftTms(0b11, 2, read=True)
        cmd, rsp_len, _ = op.cmd_data()
        assert rsp_len == 1
        op.rsp_handle(bytes([0b11000000]))  # 2 bits in high bits
        assert len(op.data) == 2
        assert int(op.data) == 0b11

    def test_add(self):
        a = ShiftTms(0b10, 2, tdi=0)
        b = ShiftTms(0b1, 1, tdi=1)
        c = a + b
        assert len(c.tms) == 3
        assert c.tdi == 1


class MockTransport:
    def __init__(self):
        self.writes = []
        self.responses = []

    def queue_response(self, data: bytes):
        self.responses.append(data)

    async def write(self, data: bytes):
        self.writes.append(data)

    async def read(self, byte_count: int) -> bytes:
        if self.responses:
            rsp = self.responses.pop(0)
            assert len(rsp) == byte_count, \
                f"Mock response length {len(rsp)} != expected {byte_count}"
            return rsp
        return bytes(byte_count)


class TestMpsseEngine:
    @pytest.mark.asyncio
    async def test_single_op(self):
        transport = MockTransport()
        transport.queue_response(bytes([0x42]))
        engine = MpsseEngine(transport, _test_logger)
        op = GetBitsLow()
        result = await engine.post(op)
        assert result is op
        assert op.value == 0x42
        assert len(transport.writes) == 1

    @pytest.mark.asyncio
    async def test_batching(self):
        transport = MockTransport()
        # Two GetBitsLow ops: 2 response bytes
        transport.queue_response(bytes([0x11, 0x22]))
        engine = MpsseEngine(transport, _test_logger)

        op1 = GetBitsLow()
        op2 = GetBitsHigh()
        f1 = engine.post(op1)
        f2 = engine.post(op2)
        await asyncio.gather(f1, f2)

        assert op1.value == 0x11
        assert op2.value == 0x22
        # Should be a single batch (one write call)
        assert len(transport.writes) == 1

    @pytest.mark.asyncio
    async def test_write_only_ops_sync(self):
        """Write-only ops still trigger a USB transfer (with sync byte)."""
        transport = MockTransport()
        # SetBitsLow has 0 response, engine adds GET_BITS_LOW for sync
        transport.queue_response(bytes([0x00]))
        engine = MpsseEngine(transport, _test_logger)

        op = SetBitsLow(0xaa, 0xff)
        await engine.post(op)

        cmd_data = transport.writes[0]
        # Command should contain SET_BITS_LOW + GET_BITS_LOW + SEND_IMMEDIATE
        assert bytes([mpsse_cmd.SET_BITS_LOW]) in cmd_data
        assert bytes([mpsse_cmd.GET_BITS_LOW]) in cmd_data

    @pytest.mark.asyncio
    async def test_mixed_ops(self):
        transport = MockTransport()
        # SetBitsLow (0 rsp) + GetBitsLow (1 rsp) = 1 rsp byte
        transport.queue_response(bytes([0x55]))
        engine = MpsseEngine(transport, _test_logger)

        op_set = SetBitsLow(0xaa, 0xff)
        op_get = GetBitsLow()
        f1 = engine.post(op_set)
        f2 = engine.post(op_get)
        await asyncio.gather(f1, f2)

        assert op_get.value == 0x55

    @pytest.mark.asyncio
    async def test_transport_error(self):
        class FailTransport:
            async def write(self, data):
                raise IOError("USB error")
            async def read(self, byte_count):
                raise IOError("USB error")

        engine = MpsseEngine(FailTransport(), _test_logger)
        f = engine.post(GetBitsLow())
        with pytest.raises(IOError, match="USB error"):
            await f
