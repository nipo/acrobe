import asyncio
import pytest

from acrobe.protocol.datagram import Send, Recv
from acrobe.component.nsl.bnoc.framed import Framed, JtagFramed
from acrobe.component.nsl.bnoc.fifo import JtagFifo
from acrobe.component.nsl.transactor.spi import SpiTransactor
from acrobe.component.jtag_spi_bridge import jtag_spi_bridge
from acrobe.protocol.spi import Cs, Shift, Interface, Target
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol.jtag import Chain, JtagInterface
from acrobe.bitstring import BitString


# -- MockChannel: in-memory Framed for SpiTransactor testing --

class MockChannel(Framed):
    """In-memory Framed that captures command frames and returns canned responses."""

    def __init__(self, response_fn=None, name: str = "mock-channel"):
        super().__init__(name)
        self.sent_frames = []
        self._response_fn = response_fn or self._default_response

    @staticmethod
    def _default_response(cmd):
        """Default: 1 status byte (0x00) per command byte that starts a command."""
        return bytes(len(cmd))

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.sent_frames.append(op.data)
                future.set_result(None)
            elif isinstance(op, Recv):
                cmd = self.sent_frames[-1] if self.sent_frames else b''
                future.set_result((self._response_fn(cmd), None))


# -- SpiTransactor tests --

class TestSpiTransactor:
    def _make(self, response_fn=None):
        ch = MockChannel(response_fn)
        tr = SpiTransactor(ch, base_freq=30e6)
        return ch, tr

    @pytest.mark.asyncio
    async def test_cs_select(self):
        ch, tr = self._make()

        def respond(cmd):
            return bytes(len(cmd))

        ch._response_fn = respond
        await tr.post(Cs(0, mode=0))
        # Divisor (first flush) + CS select
        assert len(ch.sent_frames) == 1
        cmd = ch.sent_frames[0]
        # First byte: divisor, second: CS select
        assert cmd[1] == 0x00  # SELECT slave 0, mode 0

    @pytest.mark.asyncio
    async def test_cs_deselect(self):
        ch, tr = self._make()
        ch._response_fn = lambda cmd: bytes(len(cmd))

        await tr.post(Cs(0, mode=0))
        ch.sent_frames.clear()
        await tr.post(Cs(None))

        cmd = ch.sent_frames[0]
        assert cmd[0] == 0x07  # UNSELECT

    @pytest.mark.asyncio
    async def test_shift_out(self):
        """Write-only shift: CMD_SHIFT_OUT | (count-1) + mosi bytes."""
        ch, tr = self._make(lambda cmd: bytes(len(cmd)))

        await tr.post(Cs(0, mode=0))  # consume divisor emission
        ch.sent_frames.clear()

        result = await tr.post(Shift(b'\xAA\xBB', read_miso=False))
        cmd = ch.sent_frames[0]
        assert cmd[0] == (0x80 | 1)  # SHIFT_OUT, 2 bytes (count-1=1)
        assert cmd[1:3] == b'\xAA\xBB'
        assert result.miso is None

    @pytest.mark.asyncio
    async def test_shift_read(self):
        """Read shift uses SHIFT_INOUT (spi.Shift normalizes int mosi to bytes)."""
        def respond(cmd):
            rsp = bytearray()
            i = 0
            while i < len(cmd):
                opcode = cmd[i]
                if opcode & 0xC0 == 0xC0:  # SHIFT_INOUT
                    count = (opcode & 0x3f) + 1
                    rsp.append(0x00)  # status
                    rsp.extend(bytes(range(count)))
                    i += 1 + count
                elif opcode & 0xE0 == 0x20:  # DIVISOR
                    rsp.append(0x00)
                    i += 1
                else:
                    rsp.append(0x00)
                    i += 1
            return bytes(rsp)

        ch, tr = self._make(respond)
        await tr.post(Cs(0, mode=0))
        ch.sent_frames.clear()

        result = await tr.post(Shift(3, read_miso=True))
        assert result.miso == bytes([0, 1, 2])

    @pytest.mark.asyncio
    async def test_shift_inout(self):
        """Full-duplex: CMD_SHIFT_INOUT | (count-1) + mosi, response has MISO."""
        def respond(cmd):
            rsp = bytearray()
            i = 0
            while i < len(cmd):
                opcode = cmd[i]
                if opcode & 0xC0 == 0xC0:  # SHIFT_INOUT
                    count = (opcode & 0x3f) + 1
                    rsp.append(0x00)  # status
                    rsp.extend(b'\xFF' * count)
                    i += 1 + count
                elif opcode & 0xE0 == 0x20:
                    rsp.append(0x00)
                    i += 1
                else:
                    rsp.append(0x00)
                    i += 1
            return bytes(rsp)

        ch, tr = self._make(respond)
        await tr.post(Cs(0, mode=0))
        ch.sent_frames.clear()

        result = await tr.post(Shift(b'\x12\x34', read_miso=True))
        assert result.miso == b'\xFF\xFF'

    @pytest.mark.asyncio
    async def test_chunk_splitting(self):
        """Shifts > 64 bytes are split into chunks."""
        ch, tr = self._make(lambda cmd: bytes(len(cmd)))

        await tr.post(Cs(0, mode=0))
        ch.sent_frames.clear()

        big_data = bytes(range(256)) * 1  # 256 bytes... use 100
        big_data = bytes(100)
        result = await tr.post(Shift(big_data, read_miso=False))

        cmd = ch.sent_frames[0]
        # Should have two SHIFT_OUT commands: 64 + 36
        assert cmd[0] == (0x80 | 63)  # first chunk: 64 bytes
        assert cmd[65] == (0x80 | 35)  # second chunk: 36 bytes

    @pytest.mark.asyncio
    async def test_divisor_first_flush_only(self):
        """Divisor is emitted on first flush, not on subsequent ones."""
        ch, tr = self._make(lambda cmd: bytes(len(cmd)))

        # First flush: divisor + CS
        await tr.post(Cs(0, mode=0))
        first_cmd = ch.sent_frames[0]
        assert first_cmd[0] & 0xE0 == 0x20  # CMD_DIVISOR

        ch.sent_frames.clear()

        # Second flush: no divisor
        await tr.post(Cs(None))
        second_cmd = ch.sent_frames[0]
        assert second_cmd[0] & 0xE0 != 0x20  # no divisor

    @pytest.mark.asyncio
    async def test_divisor_after_freq_update(self):
        """Divisor is re-emitted after freq_update()."""
        ch, tr = self._make(lambda cmd: bytes(len(cmd)))

        await tr.post(Cs(0, mode=0))  # emits divisor
        ch.sent_frames.clear()

        tr.freq_update(1e6)  # change freq
        await tr.post(Cs(None))
        cmd = ch.sent_frames[0]
        assert cmd[0] & 0xE0 == 0x20  # divisor re-emitted


# -- FifoSimulator --

class FifoSimulator(Batcher, Node):
    """Mock Batcher simulating JTAG interface with FIFO firmware.

    Handles _DynamicInstruction calls (which go through Tap.post → flush_ops).
    Maintains internal tx/rx FIFOs and optionally simulates status register.
    """

    def __init__(self, status_ir=None, name: str = "fifo-sim"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.rx_fifo = []       # words to send TO the host (device → host)
        self.tx_captured = []   # words received FROM the host
        self._status_ir = status_ir
        self._last_ir = None
        self.irlen = 8
        self.idcode = 0

    def load_rx(self, frames):
        """Load frames into the device's RX FIFO (these will be sent to host)."""
        for frame in frames:
            self.rx_fifo.extend(Framed.encode(frame))

    async def flush_ops(self, batch):
        for op, future in batch:
            ir_value = getattr(op, 'ir_value', None)
            tdi = getattr(op, 'tdi', None)

            if ir_value is not None and tdi is not None:
                tdi_val = int(tdi)

                if self._status_ir is not None and ir_value == self._status_ir:
                    # Status register read
                    out_free = 128
                    in_ready = len(self.rx_fifo)
                    status = (out_free << 16) | in_ready
                    result = BitString(status, 32)
                    future.set_result(result)
                else:
                    # Data register shift (FIFO)
                    valid_in = bool(tdi_val & JtagFifo.VALID)
                    ready_in = bool(tdi_val & JtagFifo.READY)
                    data_in = tdi_val & JtagFifo.DMASK

                    # Build TDO response
                    tdo = 0
                    # Always ready to receive
                    tdo |= JtagFifo.READY

                    if valid_in:
                        self.tx_captured.append(data_in)

                    if ready_in and self.rx_fifo:
                        word = self.rx_fifo.pop(0)
                        tdo |= JtagFifo.VALID | word

                    result = BitString(tdo, JtagFifo.DR_LEN)
                    future.set_result(result)
            elif hasattr(op, 'cycles'):
                # Run op
                future.set_result(None)
            else:
                future.set_result(None)

    # Tap interface stubs for JtagFifo
    def ir(self, value, dr_length=None):
        from acrobe.protocol.jtag import _DynamicInstruction
        return _DynamicInstruction(self, value, dr_length)

    def run(self, cycles=1):
        from acrobe.protocol.jtag import _TapRun
        return self.post(_TapRun(cycles))

    @property
    def USER_IR(self):
        if self._status_ir is not None:
            return [0x42, self._status_ir]
        return [0x42, 0x43]


# -- JtagFifo tests --

class TestJtagFifo:
    @pytest.mark.asyncio
    async def test_single_word_exchange(self):
        """Single-word mode (no status register)."""
        sim = FifoSimulator(status_ir=None)
        sim.load_rx([b'\xAA'])

        fifo = JtagFifo(sim, 0x42)
        rx = await fifo.exchange([0x55 | Framed.LAST], expect_frames=1)

        assert len(rx) >= 1
        data = Framed.decode(rx)
        assert data == b'\xAA'
        # Check TX was captured
        assert 0x55 | Framed.LAST in sim.tx_captured

    @pytest.mark.asyncio
    async def test_speculative_exchange(self):
        """Speculative mode (with status register)."""
        sim = FifoSimulator(status_ir=0x43)
        sim.load_rx([b'\xBB\xCC'])

        fifo = JtagFifo(sim, 0x42, status_ir=0x43)
        rx = await fifo.exchange([0x11 | Framed.LAST], expect_frames=1)

        data = Framed.decode(rx)
        assert data == b'\xBB\xCC'

    @pytest.mark.asyncio
    async def test_multi_frame_receive(self):
        """expect_frames > 1."""
        sim = FifoSimulator(status_ir=None)
        sim.load_rx([b'\x01', b'\x02'])

        fifo = JtagFifo(sim, 0x42)
        rx = await fifo.exchange([], expect_frames=2)

        frames = Framed.split_frames(rx)
        assert len(frames) == 2
        assert Framed.decode(frames[0]) == b'\x01'
        assert Framed.decode(frames[1]) == b'\x02'

    @pytest.mark.asyncio
    async def test_last_bit_terminates_frame(self):
        """LAST bit in rx word terminates frame."""
        sim = FifoSimulator(status_ir=None)
        sim.load_rx([b'\x10\x20\x30'])

        fifo = JtagFifo(sim, 0x42)
        rx = await fifo.exchange([], expect_frames=1)

        frames = Framed.split_frames(rx)
        assert len(frames) == 1
        assert Framed.decode(frames[0]) == b'\x10\x20\x30'


# -- Integration tests --

class TestJtagFramedIntegration:
    @pytest.mark.asyncio
    async def test_end_to_end_frame_exchange(self):
        """Full stack: JtagFifo → JtagFramed."""
        sim = FifoSimulator(status_ir=None)
        sim.load_rx([b'\xDE\xAD'])

        fifo = JtagFifo(sim, 0x42)
        framed = JtagFramed(fifo)

        framed.send(b'\xBE\xEF')
        data, _ = await framed.recv()

        assert data == b'\xDE\xAD'

    @pytest.mark.asyncio
    async def test_spi_transaction_through_stack(self):
        """SPI Target → Interface → SpiTransactor → JtagFramed → JtagFifo."""
        sim = FifoSimulator(status_ir=None)

        fifo = JtagFifo(sim, 0x42)
        framed = JtagFramed(fifo)

        def spi_respond(cmd_words):
            """Simulate SPI transactor firmware response.
            For each command: 1 status byte. For SHIFT_IN/INOUT: + MISO bytes.
            """
            rsp = bytearray()
            i = 0
            cmd = Framed.decode(cmd_words)
            while i < len(cmd):
                opcode = cmd[i]
                if opcode & 0xC0 == 0xC0:  # SHIFT_INOUT
                    count = (opcode & 0x3f) + 1
                    rsp.append(0x00)
                    rsp.extend(b'\x9F' * count)  # fake JEDEC ID
                    i += 1 + count
                elif opcode & 0xC0 == 0x40:  # SHIFT_IN
                    count = (opcode & 0x3f) + 1
                    rsp.append(0x00)
                    rsp.extend(b'\x9F' * count)
                    i += 1
                elif opcode & 0xC0 == 0x80:  # SHIFT_OUT
                    count = (opcode & 0x3f) + 1
                    rsp.append(0x00)
                    i += 1 + count
                else:
                    rsp.append(0x00)
                    i += 1
            return rsp

        # Pre-load FIFO with a response frame
        # First we need to know what the transactor will send...
        # For simplicity, load a generic response that covers:
        # divisor(1 status) + cs_select(1 status) + shift_inout 1B (1 status + 1 miso) + cs_deselect(1 status)
        sim.load_rx([bytes([0x00, 0x00, 0x00, 0x9F, 0x00])])

        adapter = SpiTransactor(framed, base_freq=30e6)
        interface = Interface(adapter, name="spi")
        target = Target(interface, cs=0, mode=0, name="cs0")
        interface.child_add(target)

        result = await target.transaction(Shift(b'\x9F', read_miso=True))
        shift_result = result[0]
        assert shift_result.miso == b'\x9F'


class TestJtagSpiBridge:
    def test_bridge_returns_interface(self):
        """jtag_spi_bridge() returns spi.Interface with correct tree."""
        sim = FifoSimulator(status_ir=0x43)
        iface = jtag_spi_bridge(sim, base_freq=30e6)

        assert isinstance(iface, Interface)
        assert len(iface.children) == 1
        assert isinstance(iface.children[0], Target)
        assert iface.children[0].cs == 0


class _GowinLoadMock(JtagInterface):
    """Mock that handles the JTAG shifts for GowinFpga.load().

    Returns usercode=0 (forces reload), status=0 during erase,
    status=Done after configure.
    """
    DONE_BIT = 13

    def __init__(self):
        super().__init__(name="gowin-load-mock")
        self._32bit_count = 0

    async def flush_ops(self, batch):
        from acrobe.protocol.jtag import Shift as JtagShift
        for op, future in batch:
            if isinstance(op, JtagShift) and op.read_tdo:
                if len(op.tdi) == 32:
                    self._32bit_count += 1
                    if self._32bit_count == 1:
                        future.set_result(BitString(0, 32))
                    elif self._32bit_count <= 4:
                        future.set_result(BitString(0, 32))
                    else:
                        future.set_result(BitString(1 << self.DONE_BIT, 32))
                else:
                    future.set_result(BitString(0, len(op.tdi)))
            else:
                future.set_result(None)


class TestGowinChildSpawn:
    @pytest.mark.asyncio
    async def test_child_spawn_spi_loads_bitstream(self):
        """child_spawn("spi") loads bridge bitstream then returns SPI interface."""
        from acrobe.component.gowin.gw1n import GowinFpga
        from acrobe.protocol.spi import Interface as SpiInterface

        # GW5A-25 has a firmware file (0x0001281b_jtag_spi.fs.gz)
        iface = _GowinLoadMock()
        chain = Chain()
        iface.child_add(chain)
        tap = chain.tap_add(0x0001281b, irlen=8, base=GowinFpga)
        result = await tap._child_spawn_mro("spi")
        assert isinstance(result, SpiInterface)

    @pytest.mark.asyncio
    async def test_child_spawn_spi_no_firmware(self):
        """child_spawn("spi") raises NoMatch for parts without firmware."""
        from acrobe.component.gowin.gw1n import GowinFpga
        from acrobe.db import NoMatch

        # GW2A-18 (0x0000081b) has no firmware file
        iface = _GowinLoadMock()
        chain = Chain()
        iface.child_add(chain)
        tap = chain.tap_add(0x0000081b, irlen=8, base=GowinFpga)
        with pytest.raises(NoMatch):
            await tap._child_spawn_mro("spi")

    @pytest.mark.asyncio
    async def test_child_summon_spi(self):
        """child_summon("spi") attaches and starts the SPI interface."""
        from acrobe.component.gowin.gw1n import GowinFpga
        from acrobe.protocol.spi import Interface as SpiInterface

        iface = _GowinLoadMock()
        chain = Chain()
        iface.child_add(chain)
        tap = chain.tap_add(0x0001281b, irlen=8, base=GowinFpga)
        result = await tap.child_summon("spi")
        assert isinstance(result, SpiInterface)
        assert result in tap.children
        assert result.started
        assert result.name == "spi"

    @pytest.mark.asyncio
    async def test_child_spawn_unknown(self):
        from acrobe.component.gowin.gw1n import GowinFpga
        from acrobe.db import NoMatch

        iface = _GowinLoadMock()
        chain = Chain()
        iface.child_add(chain)
        tap = chain.tap_add(0x0001281b, irlen=8, base=GowinFpga)
        with pytest.raises(NoMatch):
            await tap._child_spawn_mro("unknown")
