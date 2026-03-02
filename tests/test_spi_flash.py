import asyncio
import pytest
from crobe_async.component.spi_flash import SpiFlash
from crobe_async.protocol.spi import Cs, Shift, Interface
from crobe_async.engine import Batcher


class MockSpiAdapter(Batcher):
    """Mock SPI adapter that records ops and returns configurable data."""

    def __init__(self):
        super().__init__()
        self.ops = []
        self._read_responses = []  # queue of bytes to return for Shift reads
        self._default_response = b"\xff"

    def queue_response(self, data: bytes):
        """Queue a response for the next Shift with read_miso=True."""
        self._read_responses.append(data)

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_miso:
                if self._read_responses:
                    rsp = self._read_responses.pop(0)
                    if len(rsp) < op.byte_count:
                        op.miso = rsp + bytes(op.byte_count - len(rsp))
                    else:
                        op.miso = rsp[:op.byte_count]
                else:
                    op.miso = bytes(op.byte_count)
            future.set_result(op)


def _make_flash(adapter=None):
    """Create a SpiFlash with an Interface + Target stack backed by adapter."""
    if adapter is None:
        adapter = MockSpiAdapter()
    from crobe_async.protocol.spi import Target
    iface = Interface(adapter)
    target = Target(iface, cs=0, mode=0)
    flash = SpiFlash(target)
    return flash, adapter


class TestSpiFlashConstruction:
    def test_init(self):
        flash, _ = _make_flash()
        assert flash.jedec_id == 0
        assert flash.page_size == 256
        assert flash.ADDRESS_SIZE == 3

    def test_repr(self):
        flash, _ = _make_flash()
        assert "SpiFlash" in repr(flash)


class TestSpiFlashCommands:
    @pytest.mark.asyncio
    async def test_read_status(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        adapter.queue_response(bytes([0x00]))

        status = await flash.read_status()
        assert status == 0x00

        # Should have CS assert, Shift (command), Shift (read), CS deassert
        cs_ops = [op for op in adapter.ops if isinstance(op, Cs)]
        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]
        assert len(cs_ops) == 2
        assert cs_ops[0].value == 0  # CS assert
        assert cs_ops[1].value is None  # CS deassert

    @pytest.mark.asyncio
    async def test_read_jedec_id(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        adapter.queue_response(bytes([0xef, 0x40, 0x16]))

        jedec_id = await flash.read_jedec_id()
        assert jedec_id == 0xef4016

    @pytest.mark.asyncio
    async def test_write_enable(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)

        await flash.write_enable()

        # Should have sent CMD_WRITE_ENABLE (0x06)
        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]
        assert any(op.mosi == b"\x06" for op in shift_ops)


class TestSpiFlashRead:
    @pytest.mark.asyncio
    async def test_read_small(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        expected = bytes(range(16))
        adapter.queue_response(expected)

        data = await flash.read(0x000000, 16)
        assert data == expected

    @pytest.mark.asyncio
    async def test_read_chunked(self):
        """Reads larger than 1024 bytes are split into chunks."""
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        # Queue 2 chunks for a 1500-byte read
        adapter.queue_response(bytes(1024))
        adapter.queue_response(bytes(476))

        data = await flash.read(0x000000, 1500)
        assert len(data) == 1500

    @pytest.mark.asyncio
    async def test_read_uses_fast_read(self):
        """Read uses fast read command (0x0b) with 1 dummy byte."""
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        adapter.queue_response(bytes(4))

        await flash.read(0x000100, 4)

        # Find the command shift (Shift with read_miso=False)
        cmd_shifts = [op for op in adapter.ops
                      if isinstance(op, Shift) and not op.read_miso]
        # Command should be: 0x0b (fast read) + 3 addr bytes + 1 dummy
        assert len(cmd_shifts) >= 1
        first_cmd = cmd_shifts[0]
        assert first_cmd.mosi[0:1] == b"\x0b"


class TestSpiFlashErase:
    @pytest.mark.asyncio
    async def test_erase_sector(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        # Queue status response for _wait_ready (WIP=0)
        adapter.queue_response(bytes([0x00]))

        await flash.erase_sector(0x001000)

        # Should have: write_enable cmd, erase cmd, status read
        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]
        mosi_bytes = [op.mosi for op in shift_ops if not op.read_miso]
        # Write enable (0x06) + sector erase (0x20 + 3 addr bytes)
        assert any(m == b"\x06" for m in mosi_bytes)
        assert any(m[0:1] == b"\x20" for m in mosi_bytes)


class TestSpiFlashProgram:
    @pytest.mark.asyncio
    async def test_page_program(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        # Queue status response (WIP=0)
        adapter.queue_response(bytes([0x00]))

        await flash.page_program(0x000000, b"\xaa\xbb\xcc\xdd")

        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]
        mosi_bytes = [op.mosi for op in shift_ops if not op.read_miso]
        # Write enable (0x06)
        assert any(m == b"\x06" for m in mosi_bytes)
        # Page program (0x02 + addr + data)
        pp_cmds = [m for m in mosi_bytes if m[0:1] == b"\x02"]
        assert len(pp_cmds) == 1
        assert pp_cmds[0].endswith(b"\xaa\xbb\xcc\xdd")

    @pytest.mark.asyncio
    async def test_program_multi_page(self):
        """Programming across page boundary splits into two page programs."""
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        flash.page_size = 4
        # Two page programs, each needs WIP=0 after
        adapter.queue_response(bytes([0x00]))
        adapter.queue_response(bytes([0x00]))

        await flash.program(0x000002, b"\x01\x02\x03\x04\x05\x06")

        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]
        pp_cmds = [op.mosi for op in shift_ops
                   if not op.read_miso and op.mosi[0:1] == b"\x02"]
        # 6 bytes at addr 2 with page_size=4: first 2 bytes (to boundary), then 4 bytes
        assert len(pp_cmds) == 2

    @pytest.mark.asyncio
    async def test_verify(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        expected = b"\xaa\xbb\xcc\xdd"
        adapter.queue_response(expected)

        assert await flash.verify(0x000000, expected) is True

    @pytest.mark.asyncio
    async def test_verify_mismatch(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        adapter.queue_response(b"\x00\x00\x00\x00")

        assert await flash.verify(0x000000, b"\xaa\xbb\xcc\xdd") is False


class TestSpiFlashDetect:
    @pytest.mark.asyncio
    async def test_detect_valid(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        # Queue JEDEC ID: Winbond W25Q32 (0xef4016, capacity=0x16=22, size=4MB)
        adapter.queue_response(bytes([0xef, 0x40, 0x16]))

        await flash.detect()
        assert flash.jedec_id == 0xef4016
        assert flash.total_size == (1 << 0x16)  # 4 MB

    @pytest.mark.asyncio
    async def test_detect_bad_id(self):
        adapter = MockSpiAdapter()
        flash, _ = _make_flash(adapter)
        adapter.queue_response(bytes([0xff, 0xff, 0xff]))

        with pytest.raises(RuntimeError, match="Bad JEDEC"):
            await flash.detect()
