"""High-level libaji-faithful client.

Built on :class:`.link.AjiLink` and the wire layer in :mod:`.wire`.
Mirrors the public surface of libaji's ``AJI_CLIENT`` and
``AJI_OPEN_JS`` for the operations we need to use ``jtagd`` as a
JTAG transport from acrobe.

The intent is byte-for-byte compatibility with real ``jtagd``, so
where libaji's C++ does something subtle we follow it. Field
ordering, command sizing, and the use of mux 4 (the first FIFO) for
ACCESS_DR data follow ``jtag_client_chain.cpp`` and
``jtag_client_open.cpp``.

Operations covered:

* ``get_hardware``                 – list the cables/boards the
  server knows about.
* ``lock_chain`` / ``unlock_chain``
* ``scan_chain``                   – discovers and returns the TAP
  list on a chain.
* ``open_device`` / ``close_device``
* ``lock_device`` / ``unlock_device``
* ``access_ir``                    – shift instruction register.
* ``access_dr``                    – shift data register, with the
  bits transported on the FIFO 0 mux channel as libaji expects.
* ``run_test_idle``
* ``test_logic_reset``

What's deliberately *not* yet here: claim lists for OPEN_DEVICE
(the server would refuse to let us use any non-shared IR codes,
which is fine for plain JTAG access), service-channel claims like
SDM_CONFIGURE, watcher streams, anything tied to programming
parameters.
"""

import dataclasses
import logging
import struct
from typing import Iterable, Self

from .wire import (
    AJI_CURRENT_VERSION,
    Command,
    JTAG_PORT,
    MUX_FIFO_MIN,
    MessageBuilder,
    MessageReader,
)
from .link import AjiLink


_logger = logging.getLogger("aji.client")


# AJI_ERROR codes from libaji_client/src/h/aji.h. We don't need all
# of them — these are the ones a programmer-class flow can hit.
AJI_NO_ERROR              = 0
AJI_FAILURE               = 1
AJI_TIMEOUT               = 2
AJI_UNKNOWN_HARDWARE      = 32
AJI_INVALID_CHAIN_ID      = 33
AJI_LOCKED                = 34
AJI_NOT_LOCKED            = 35
AJI_CHAIN_IN_USE          = 36
AJI_NO_DEVICES            = 37
AJI_BAD_TAP_POSITION      = 39
AJI_INVALID_OPEN_ID       = 44
AJI_INVALID_PARAMETER     = 45
AJI_BAD_TAP_STATE         = 46


class AjiError(Exception):
    """Raised when a server response carries a non-OK status."""

    def __init__(self, code: int, op: str = "") -> None:
        self.code = code
        self.op = op
        super().__init__(f"AJI op {op!r} failed: status {code}")


def _expect_ok(status: int, op: str) -> None:
    if status != AJI_NO_ERROR:
        raise AjiError(status, op)


# --- Public dataclasses ----------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Hardware:
    """One ``Hardware`` record as returned by GET_HARDWARE."""
    chain_id: int
    hw_name: str
    port: str
    chain_type: int
    device_name: str
    features: int


@dataclasses.dataclass(frozen=True, slots=True)
class Device:
    """One TAP on a chain as returned by SCAN_CHAIN+READ_CHAIN."""
    idcode: int
    irlen: int
    features: int
    name: str = ""


# AJI_DR_FLAGS bits from aji.h. For read-only DR shifts, set CAPTURE.
DR_FLAG_CAPTURE = 1 << 0
DR_FLAG_NO_TDI  = 1 << 1
DR_FLAG_DR_LENGTH_EXACT = 1 << 2

# AJI_IR_FLAGS bits.
IR_FLAG_CAPTURE = 1 << 0


# --- Client ----------------------------------------------------------------


class AjiClient:
    """High-level libaji client. Use :meth:`connect`."""

    def __init__(self, link: AjiLink) -> None:
        self._link = link

    @property
    def server_version(self) -> int:
        return self._link.server_version

    @property
    def server_version_info(self) -> str:
        return self._link.server_version_info

    @classmethod
    async def connect(
        cls,
        host: str = "localhost",
        port: int = JTAG_PORT,
        *,
        password: str | None = None,
        client_version: int = AJI_CURRENT_VERSION,
    ) -> Self:
        link = await AjiLink.connect(host, port, password=password,
                                     client_version=client_version)
        return cls(link)

    async def close(self) -> None:
        await self._link.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # --- Hardware / chains ---

    async def get_hardware(self) -> list[Hardware]:
        """Enumerate hardware entries the server has claimed.

        Mirrors ``AJI_CLIENT::get_hardware_from_server``: send
        ``GET_HARDWARE`` with our protocol version, parse the response
        ``(count, fifo_len)``, and read the per-record blob from the
        first FIFO frame the server pushes.
        """
        request = (MessageBuilder()
                   .add_command(Command.GET_HARDWARE)
                   .add_int(self.server_version)
                   .build())
        response = await self._link.send_receive(request)
        rdr = MessageReader(response)
        status = rdr.next_block()
        _expect_ok(status, "GET_HARDWARE")
        count = rdr.read_int()
        fifo_len = rdr.read_int()

        if count == 0 or fifo_len == 0:
            return []

        fifo_data = self._collect_fifo_bytes(fifo_len)
        return self._parse_hardware_records(fifo_data, count)

    def _collect_fifo_bytes(self, expected: int) -> bytes:
        """Concatenate FIFO frames until we have ``expected`` bytes."""
        out = bytearray()
        for mux, payload in self._link.drain_fifos():
            if mux != MUX_FIFO_MIN:
                _logger.warning("ignoring FIFO data on mux %d", mux)
                continue
            out.extend(payload)
        if len(out) < expected:
            raise AjiError(AJI_FAILURE,
                           f"FIFO short: got {len(out)} of {expected} bytes")
        return bytes(out[:expected])

    def _parse_hardware_records(
        self, data: bytes, count: int) -> list[Hardware]:
        """Parse ``count`` Hardware records from a FIFO blob.

        Layout (one per record):
            int chain_id, string hw_name, string port, int chain_type,
            string device_name, int features (only when server >= v2)
        """
        # Wrap as a single MESSAGE block for MessageReader's API.
        framed = bytearray()
        framed.append(0)
        framed.append(0)
        framed.extend(struct.pack(">H", 4 + len(data)))
        framed.extend(data)
        rdr = MessageReader(bytes(framed))
        rdr.next_block()

        out = []
        for _ in range(count):
            chain_id = rdr.read_int()
            hw_name = rdr.read_string()
            port = rdr.read_string()
            chain_type = rdr.read_int()
            device_name = rdr.read_string()
            features = rdr.read_int() if self.server_version >= 2 else 0
            out.append(Hardware(
                chain_id=chain_id,
                hw_name=hw_name,
                port=port,
                chain_type=chain_type,
                device_name=device_name,
                features=features,
            ))
        return out

    async def lock_chain(self, chain_id: int, timeout_ms: int = 10000) -> None:
        request = (MessageBuilder()
                   .add_command(Command.LOCK_CHAIN)
                   .add_int(chain_id)
                   .add_int(timeout_ms)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "LOCK_CHAIN")

    async def unlock_chain(self, chain_id: int) -> None:
        request = (MessageBuilder()
                   .add_command(Command.UNLOCK_CHAIN)
                   .add_int(chain_id)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "UNLOCK_CHAIN")

    # --- Chain parameters -------------------------------------------------

    async def set_parameter(self, chain_id: int, name: str,
                            value: int) -> None:
        """Set a chain-scoped server parameter.

        Standard names (from libaji): ``"JtagClock"`` (TCK rate in Hz),
        ``"JtagClockAutoAdjust"`` (1/0). Mirrors
        ``AJI_CHAIN_JS::set_parameter``.

        Note: requires the chain to be locked (LOCK_CHAIN), which
        callers already do for any per-chain workflow.
        """
        request = (MessageBuilder()
                   .add_command(Command.SET_PARAMETER)
                   .add_int(chain_id)
                   .add_string(name)
                   .add_int(value)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "SET_PARAMETER")

    async def get_parameter(self, chain_id: int, name: str) -> int:
        """Read a chain-scoped server parameter as a 32-bit int."""
        request = (MessageBuilder()
                   .add_command(Command.GET_PARAMETER)
                   .add_int(chain_id)
                   .add_string(name)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "GET_PARAMETER")
        return rdr.read_int()

    async def scan_chain(self, chain_id: int,
                         timeout_ms: int = 10000) -> list[Device]:
        """Scan the chain and return its TAPs.

        Two-step: SCAN_CHAIN primes a fresh scan with ``scan_tag=0``;
        READ_CHAIN drains the resulting FIFO + answer.
        """
        # SCAN_CHAIN primes the scan (mirrors libaji refresh_chain).
        scan_req = (MessageBuilder()
                    .add_command(Command.SCAN_CHAIN)
                    .add_int(chain_id)
                    .add_int(timeout_ms)
                    .build())
        rdr = MessageReader(await self._link.send_receive(scan_req))
        _expect_ok(rdr.next_block(), "SCAN_CHAIN")
        # libaji optionally reads a scan_tag here; we don't keep it.

        # READ_CHAIN with scan_tag=0 + pack_style=1 (PACK_STYLE_OPTIMIZE).
        read_req = (MessageBuilder()
                    .add_command(Command.READ_CHAIN)
                    .add_int(chain_id)
                    .add_int(0)  # scan_tag (0 = "give me the latest scan")
                    .add_int(1)  # pack_style
                    .build())
        response = await self._link.send_receive(read_req)
        rdr = MessageReader(response)
        _expect_ok(rdr.next_block(), "READ_CHAIN")
        scan_tag = rdr.read_int()
        device_count = rdr.read_int()
        fifo_len = rdr.read_int()

        if device_count == 0 or fifo_len == 0:
            return []

        return self._parse_device_records(
            self._collect_fifo_bytes(fifo_len), device_count)

    def _parse_device_records(self, data: bytes, count: int) -> list[Device]:
        """Layout per device, from libaji's READ_CHAIN reply:
            int idcode, int irlen, int features, raw[8] reserved,
            string device_name
        """
        framed = bytearray()
        framed.append(0)
        framed.append(0)
        framed.extend(struct.pack(">H", 4 + len(data)))
        framed.extend(data)
        rdr = MessageReader(bytes(framed))
        rdr.next_block()

        out = []
        for _ in range(count):
            idcode = rdr.read_int()
            irlen = rdr.read_int()
            features = rdr.read_int()
            rdr.read_raw(8)  # 8 reserved bytes (always zero in jtagd)
            name = rdr.read_string()
            out.append(Device(
                idcode=idcode, irlen=irlen,
                features=features, name=name))
        return out

    # --- Devices ---

    async def open_device(
        self,
        chain_id: int,
        tap_position: int,
        claims: Iterable[tuple[int, int, int]] = (),
        *,
        application_name: str = "acrobe",
    ) -> int:
        """Open a TAP for IR/DR access.

        ``claims`` is a sequence of ``(claim_type, length, value)``
        triples (matching libaji's v13 layout). For plain JTAG access
        leave it empty.

        The wire format follows ``AJI_CHAIN_JS::open_device`` in
        ``jtag_client_chain.cpp`` and depends on server version:

        * v ≥ 13: claim_count, then per claim ``(type, length,
          value(long))``, then ``application_name`` string.
        * v 2..12: claim_count, then per claim ``(type, value(int))``,
          then ``application_name`` string.
        * v < 2: instruction_n, 0, then per claim ``value(int)``,
          then ``application_name`` string.

        The trailing ``application_name`` is required — leaving it
        out makes ``jtagd`` reject with ``INTERNAL_ERROR``.
        """
        builder = (MessageBuilder()
                   .add_command(Command.OPEN_DEVICE)
                   .add_int(chain_id)
                   .add_int(tap_position))
        claim_list = list(claims)
        version = self.server_version
        if version >= 13:
            builder.add_int(len(claim_list))
            for claim_type, length, value in claim_list:
                builder.add_int(claim_type)
                builder.add_int(length)
                builder.add_long(value)
        elif version >= 2:
            builder.add_int(len(claim_list))
            for claim_type, _length, value in claim_list:
                builder.add_int(claim_type)
                builder.add_int(value & 0xFFFFFFFF)
        else:
            builder.add_int(len(claim_list))
            builder.add_int(0)
            for _claim_type, _length, value in claim_list:
                builder.add_int(value & 0xFFFFFFFF)
        builder.add_string(application_name)

        request = builder.build()
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "OPEN_DEVICE")
        open_id = rdr.read_int()
        return open_id

    async def close_device(self, open_id: int) -> None:
        request = (MessageBuilder()
                   .add_command(Command.CLOSE_DEVICE)
                   .add_int(open_id)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "CLOSE_DEVICE")

    async def lock_device(self, open_id: int, timeout_ms: int = 10000) -> None:
        request = (MessageBuilder()
                   .add_command(Command.LOCK_DEVICE)
                   .add_int(open_id)
                   .add_int(timeout_ms)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "LOCK_DEVICE")

    async def unlock_device(self, open_id: int) -> None:
        request = (MessageBuilder()
                   .add_command(Command.UNLOCK_DEVICE)
                   .add_int(open_id)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "UNLOCK_DEVICE")

    # --- JTAG ops ---

    async def access_ir(self, open_id: int, instruction: int,
                        flags: int = 0) -> int | None:
        """Shift IR. Returns the captured IR if ``IR_FLAG_CAPTURE`` is
        set in ``flags``, else ``None``.
        """
        request = (MessageBuilder()
                   .add_command(Command.ACCESS_IR)
                   .add_int(open_id)
                   .add_int(instruction)
                   .add_int(flags)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "ACCESS_IR")
        if flags & IR_FLAG_CAPTURE:
            return rdr.read_int()
        return None

    async def access_dr(
        self,
        open_id: int,
        length_dr: int,
        write_bits: bytes = b"",
        flags: int = 0,
        *,
        write_length: int | None = None,
        read_length: int | None = None,
        read_offset: int = 0,
        write_offset: int = 0,
    ) -> bytes:
        """Shift DR. Mirrors ``AJI_OPEN_JS::access_dr``.

        ``length_dr`` is the total chain DR length in bits. ``write_bits``
        is the byte buffer holding the bits to shift in (LSB of byte 0
        first); its length must be at least ``ceil(write_length/8)``.

        ``write_length`` defaults to ``length_dr`` (i.e. shift the whole
        register). It must satisfy
        ``write_offset + write_length <= length_dr`` — exceeding it
        makes ``jtagd`` shift past the end of the DR chain and crashes
        with heap corruption ("corrupted size vs. prev_size").

        ``read_length`` defaults to ``length_dr`` when
        ``DR_FLAG_CAPTURE`` is set, else 0.

        DR data flows over FIFO 0 (mux 4). For sub-4096-byte payloads
        we send a single FIFO frame ahead of the command and read at
        most one FIFO frame for the answer.
        """
        if write_length is None:
            write_length = length_dr if write_bits else 0
        if read_length is None:
            read_length = length_dr if (flags & DR_FLAG_CAPTURE) else 0

        # libaji constraints (jtagd validates these too, but leniently):
        if write_offset + write_length > length_dr:
            raise ValueError(
                f"write_offset+write_length ({write_offset + write_length}) "
                f"exceeds length_dr ({length_dr})")
        if read_offset + read_length > length_dr:
            raise ValueError(
                f"read_offset+read_length ({read_offset + read_length}) "
                f"exceeds length_dr ({length_dr})")
        # Buffer must hold at least the bits we'll shift in.
        need_bytes = (write_length + 7) // 8
        if len(write_bits) < need_bytes:
            raise ValueError(
                f"write_bits buffer too small: {len(write_bits)} < {need_bytes}")
        # Trim padding bytes the caller may have included.
        if len(write_bits) > need_bytes:
            write_bits = write_bits[:need_bytes]

        version = self.server_version
        builder = (MessageBuilder()
                   .add_command(Command.ACCESS_DR)
                   .add_int(open_id)
                   .add_int(length_dr)
                   .add_int(flags)
                   .add_int(write_offset)
                   .add_int(write_length)
                   .add_int(read_offset)
                   .add_int(read_length))
        if version >= 5:
            builder.add_int(1)  # batch
        request = builder.build()

        # libaji sends the command frame on mux 0 *first*, then the
        # FIFO data on mux 4. The server reads the command, learns
        # how many FIFO bytes to expect, and waits for them. Sending
        # FIFO before command makes jtagd block (the FIFO bytes have
        # nowhere to go since no ACCESS_DR is in flight yet).
        response = await self._link.send_receive_with_fifo(
            request, fifo_after=write_bits)
        rdr = MessageReader(response)
        _expect_ok(rdr.next_block(), "ACCESS_DR")

        if read_length == 0:
            return b""

        # Read response data from FIFO 0.
        nbytes = (read_length + 7) // 8
        return self._collect_fifo_bytes(nbytes)

    async def run_test_idle(self, open_id: int, num_clocks: int,
                            flags: int = 0) -> None:
        builder = (MessageBuilder()
                   .add_command(Command.RUN_TEST_IDLE)
                   .add_int(open_id)
                   .add_int(num_clocks))
        # libaji adds a flags int from version 5 onwards (the
        # `server_supports_flags` branch in jtag_client_open.cpp:1556).
        if self.server_version >= 5:
            builder.add_int(flags)
        request = builder.build()
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "RUN_TEST_IDLE")

    async def test_logic_reset(self, open_id: int) -> None:
        request = (MessageBuilder()
                   .add_command(Command.TEST_LOGIC_RESET)
                   .add_int(open_id)
                   .build())
        rdr = MessageReader(await self._link.send_receive(request))
        _expect_ok(rdr.next_block(), "TEST_LOGIC_RESET")
