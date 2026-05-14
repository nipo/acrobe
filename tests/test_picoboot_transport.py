"""Tests for `PicobootUsbTransport` — command framing, status
parsing, and the bulk OUT/IN sequencing for each command type.

The mock USB layer replaces the bulk endpoints and control method
with bookkeeping callables. Real hardware testing is out of scope
here; these tests verify that what we'd send on the wire matches
the PICOBOOT protocol as documented in RP2040 datasheet 2.8.5.
"""

import asyncio
import struct

import pytest

from acrobe.component.raspberry.picoboot import (
    PicobootPuppet, PicobootTransport,
)
from acrobe.target.region import Ram
from acrobe.component.raspberry.picoboot_transport import (
    PicobootUsbTransport, PicobootError,
    build_command, parse_status,
    PICOBOOT_MAGIC,
    CMD_READ, CMD_WRITE, CMD_EXEC, CMD_FLASH_ERASE,
    CMD_EXIT_XIP, CMD_REBOOT,
    CTRL_RESET_INTERFACE, CTRL_GET_COMMAND_STATUS,
    STATUS_OK, STATUS_INVALID_ADDRESS,
    RequestTypeType, RequestTypeRecipient,
)


# ---------------------------------------------------------------- framing


class TestBuildCommand:
    def test_basic_layout(self):
        blob = build_command(
            token=1, cmd_id=CMD_WRITE, transfer_length=4, args=b"\x00")
        assert len(blob) == 32
        magic, token, cmd_id, arg_size, _, transfer_length = struct.unpack(
            "<IIBBHI", blob[:16])
        assert magic == PICOBOOT_MAGIC
        assert token == 1
        assert cmd_id == CMD_WRITE
        assert arg_size == 1
        assert transfer_length == 4
        # Args are placed at offset 16 and zero-padded out to 32.
        assert blob[16] == 0x00
        assert blob[17:32] == bytes(15)

    def test_rejects_overlong_args(self):
        with pytest.raises(ValueError):
            build_command(0, CMD_WRITE, 0, b"\x00" * 17)

    def test_read_cmd_has_high_bit(self):
        blob = build_command(0, CMD_READ, 64, b"")
        assert blob[8] == 0x84
        assert blob[8] & 0x80


class TestParseStatus:
    def test_round_trip(self):
        blob = struct.pack(
            "<IIBB6s", 7, STATUS_INVALID_ADDRESS, CMD_WRITE, 0,
            b"\x00" * 6)
        token, status, cmd, in_progress = parse_status(blob)
        assert token == 7
        assert status == STATUS_INVALID_ADDRESS
        assert cmd == CMD_WRITE
        assert in_progress == 0

    def test_short_blob_rejected(self):
        with pytest.raises(ValueError):
            parse_status(b"\x00" * 8)


# ---------------------------------------------------------------- mock USB


class MockBulkOut:
    def __init__(self):
        self.writes: list[bytes] = []

    async def write(self, data):
        self.writes.append(bytes(data))


class MockBulkIn:
    def __init__(self, mps=64):
        self._queue: list[bytes] = []
        self.reads: list[int] = []
        self.mps = mps

    def queue(self, *chunks):
        self._queue.extend(bytes(c) for c in chunks)

    async def read(self, size):
        self.reads.append(size)
        if not self._queue:
            raise AssertionError("MockBulkIn empty — test under-queued data")
        return self._queue.pop(0)


class MockDevice:
    """Substitute for ausb's Device — only the methods the transport
    actually calls. Logs every control transfer and feeds back
    queued IN-direction responses."""

    def __init__(self):
        self.control_calls: list[tuple] = []
        # Queued IN responses for control IN requests (b"" for OUT).
        self._control_responses: list[bytes] = []

    def queue_status(self, token=0, status=STATUS_OK, cmd_id=0,
                     in_progress=0):
        blob = struct.pack(
            "<IIBB6s", token, status, cmd_id, in_progress, bytes(6))
        self._control_responses.append(blob)

    async def control(self, type_, recipient, request, value, index,
                      data_or_length):
        self.control_calls.append(
            (type_, recipient, request, value, index, data_or_length))
        if isinstance(data_or_length, int):
            if not self._control_responses:
                return b"\x00" * data_or_length
            blob = self._control_responses.pop(0)
            return blob[:data_or_length]
        return b""


def make_transport(*, mps=64):
    import logging
    device = MockDevice()
    ep_out = MockBulkOut()
    ep_in = MockBulkIn(mps=mps)
    t = PicobootUsbTransport(
        device, interface_index=2, ep_out=ep_out, ep_in=ep_in,
        mps=mps, logger=logging.getLogger("test.picoboot"))
    return t, device, ep_out, ep_in


# ---------------------------------------------------------------- transport


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_interface(self):
        t, device, _, _ = make_transport()
        await t.reset_interface()
        assert device.control_calls == [(
            RequestTypeType.Vendor, RequestTypeRecipient.Interface,
            CTRL_RESET_INTERFACE, 0, 2, b"")]


class TestExec:
    @pytest.mark.asyncio
    async def test_exec_sends_command_and_reads_ack(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"")  # bootrom's "function returned" ZLP
        await t.exec(0x20000001)
        # One bulk-OUT: the 32-byte command.
        assert len(ep_out.writes) == 1
        cmd = ep_out.writes[0]
        assert len(cmd) == 32
        assert struct.unpack("<I", cmd[:4])[0] == PICOBOOT_MAGIC
        assert cmd[8] == CMD_EXEC
        assert cmd[9] == 4  # arg_size
        # Args: 4-byte address with Thumb bit set.
        assert struct.unpack("<I", cmd[16:20])[0] == 0x20000001

    @pytest.mark.asyncio
    async def test_exec_forces_thumb_bit(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"")
        # Caller passed an even address — transport must OR in the
        # Thumb bit before sending.
        await t.exec(0x20000000)
        assert struct.unpack("<I", ep_out.writes[0][16:20])[0] == 0x20000001


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_sends_command_then_data(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"")  # ack ZLP
        await t.write(0x20000100, b"\xaa\xbb\xcc\xdd")
        assert len(ep_out.writes) == 2
        cmd, data = ep_out.writes
        assert cmd[8] == CMD_WRITE
        assert cmd[9] == 8  # arg_size = addr+size
        assert struct.unpack("<II", cmd[16:24]) == (0x20000100, 4)
        assert struct.unpack("<I", cmd[12:16])[0] == 4  # transfer_length
        assert data == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_empty_write_is_noop(self):
        t, _, ep_out, ep_in = make_transport()
        await t.write(0x20000000, b"")
        assert ep_out.writes == []


class TestRead:
    @pytest.mark.asyncio
    async def test_read_sends_command_then_reads_data_then_zlp_ack(self):
        t, _, ep_out, ep_in = make_transport(mps=8)
        # Queue 16 bytes of "data" returned by the bootrom — two
        # MPS-sized chunks.
        payload = bytes(range(16))
        ep_in.queue(payload[:8], payload[8:])
        data = await t.read(0x10000000, 16)
        assert data == payload
        # Two bulk-OUTs: the command + the trailing ZLP ack.
        assert len(ep_out.writes) == 2
        cmd, ack = ep_out.writes
        assert cmd[8] == CMD_READ
        assert struct.unpack("<II", cmd[16:24]) == (0x10000000, 16)
        assert struct.unpack("<I", cmd[12:16])[0] == 16
        assert ack == b""  # ZLP

    @pytest.mark.asyncio
    async def test_read_short_packet_terminates_early(self):
        t, _, _, ep_in = make_transport(mps=8)
        # 5 bytes (< MPS) — short packet marks end of frame even
        # though the request was for 16.
        ep_in.queue(b"hello")
        data = await t.read(0x10000000, 16)
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_empty_read_is_noop(self):
        t, _, ep_out, _ = make_transport()
        result = await t.read(0x10000000, 0)
        assert result == b""
        assert ep_out.writes == []


class TestFlashErase:
    @pytest.mark.asyncio
    async def test_alignment_rejected(self):
        t, _, _, _ = make_transport()
        with pytest.raises(ValueError):
            await t.flash_erase(0x10000100, 0x1000)
        with pytest.raises(ValueError):
            await t.flash_erase(0x10000000, 0x800)

    @pytest.mark.asyncio
    async def test_aligned_flash_erase(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"")
        await t.flash_erase(0x10001000, 0x2000)
        cmd = ep_out.writes[0]
        assert cmd[8] == CMD_FLASH_ERASE
        assert struct.unpack("<II", cmd[16:24]) == (0x10001000, 0x2000)


class TestExitXip:
    @pytest.mark.asyncio
    async def test_exit_xip_no_args_no_data(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"")
        await t.exit_xip()
        cmd = ep_out.writes[0]
        assert cmd[8] == CMD_EXIT_XIP
        assert cmd[9] == 0
        assert struct.unpack("<I", cmd[12:16])[0] == 0


class TestErrorSurfacing:
    @pytest.mark.asyncio
    async def test_stall_reads_status_and_raises(self):
        from ausb.exception import TransferStalled
        t, device, ep_out, ep_in = make_transport()
        # Queue: command write succeeds, but the IN ack stalls.
        async def stalling_read(size):
            ep_in.reads.append(size)
            raise TransferStalled("stalled")
        ep_in.read = stalling_read
        device.queue_status(
            token=1, status=STATUS_INVALID_ADDRESS,
            cmd_id=CMD_WRITE, in_progress=0)
        with pytest.raises(PicobootError) as ei:
            await t.write(0x12345678, b"\x00\x00\x00\x00")
        assert ei.value.status == STATUS_INVALID_ADDRESS
        assert ei.value.command == CMD_WRITE
        # The transport read the status and then reset the interface.
        reset_calls = [c for c in device.control_calls
                       if c[2] == CTRL_RESET_INTERFACE]
        status_calls = [c for c in device.control_calls
                        if c[2] == CTRL_GET_COMMAND_STATUS]
        assert len(status_calls) >= 1
        assert len(reset_calls) >= 1


class TestTokenIncrements:
    @pytest.mark.asyncio
    async def test_tokens_are_sequential(self):
        t, _, ep_out, ep_in = make_transport()
        ep_in.queue(b"", b"")
        await t.write(0x20000000, b"\x00\x00\x00\x00")
        await t.write(0x20000004, b"\x00\x00\x00\x00")
        token0 = struct.unpack("<I", ep_out.writes[0][4:8])[0]
        token1 = struct.unpack("<I", ep_out.writes[2][4:8])[0]
        assert token1 == token0 + 1


# -------------------------------------------------- integration with Puppet


class TestPuppetOnUsbTransport:
    """End-to-end: a `PicobootPuppet` over the USB transport (with
    mocked endpoints). Verifies the puppet's expectation of the
    transport surface matches what `PicobootUsbTransport` provides."""

    @pytest.mark.asyncio
    async def test_satisfies_picoboot_transport_protocol(self):
        t, _, _, _ = make_transport()
        assert isinstance(t, PicobootTransport)

    @pytest.mark.asyncio
    async def test_puppet_call_through_usb_transport(self):
        """The puppet `prepare`+`run`+`wait` flow translates into one
        WRITE (data area), one EXEC, one READ (result slot)."""
        t, _, ep_out, ep_in = make_transport(mps=64)
        # Sequence of ZLPs and read payloads.
        # 1. WRITE thunk → ZLP ack
        # 2. WRITE data → ZLP ack
        # 3. EXEC      → ZLP ack
        # 4. READ result → 4 bytes (we'll have the test write the
        #    result back into the queue manually because the mock
        #    transport doesn't actually run the thunk).
        ep_in.queue(b"", b"", b"", struct.pack("<I", 0xcafebabe))

        ram = Ram("sram", 0x20000000, 0x10000)
        puppet = PicobootPuppet("p", ram, t)
        result = await puppet.call(0x20008000, 1, 2, 3, 4)
        assert result == 0xcafebabe
