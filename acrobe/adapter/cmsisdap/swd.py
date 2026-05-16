"""CMSIS-DAP SWD wire interface.

Implements :class:`acrobe.protocol.swd.Interface` on top of the
CMSIS-DAP command set. Unlike a bit-bang adapter, the firmware
handles the SWD wire protocol — packet framing, ACK/parity,
AP-read pipelining (RDBUFF drain), and inter-transfer idle
cycles — so this layer just translates swd ops into DAP_*
command bytes.

Mapping summary
---------------

* ``swd.JtagToSwd`` / ``swd.LineReset`` / ``swd.Wakeup``
  → ``DAP_SWJ_Sequence`` chunks (max 256 bits per call, so
  longer sequences split across multiple commands).
* ``swd.Run(cycles)`` is folded into the ``DAP_TransferConfigure``
  idle-cycles count we set at start; explicit Run ops are
  no-ops at this layer (the firmware inserts the configured
  idles after every AP access, which is what SwDp uses Run
  for).
* ``swd.Read`` / ``swd.Write`` → ``DAP_Transfer`` (batched per
  packet; AP-read pipelining handled by the firmware so the
  user-facing future resolves to the real data without a
  trailing RDBUFF read).
"""

from __future__ import annotations

from ...protocol import swd
from . import protocol


# Idle cycles inserted by the firmware between AP accesses. Mirrors
# the SwDp.AP_IDLE_CYCLES default — keeping them aligned avoids
# WAIT responses on chips that need a beat between back-to-back
# AP transactions.
_DEFAULT_IDLE_CYCLES = 32

# DAP_TransferConfigure retry limits. The default of 16 wait
# retries / 16 match retries is generous enough that we rarely
# surface a WAIT to the caller; raising further hides genuine
# bugs without much benefit.
_DEFAULT_WAIT_RETRY  = 16
_DEFAULT_MATCH_RETRY = 16


def _swj_chunks(bits: list[int]):
    """Yield (count, bytes) tuples for DAP_SWJ_Sequence, each
    capped at 256 bits per CMSIS-DAP spec (count=0 means 256)."""
    n = len(bits)
    for off in range(0, n, 256):
        chunk = bits[off:off + 256]
        count = len(chunk)
        # Pack LSB-first into bytes.
        nb = (count + 7) // 8
        buf = bytearray(nb)
        for i, b in enumerate(chunk):
            if b:
                buf[i // 8] |= 1 << (i % 8)
        # Wire encoding: count=0 means 256 (so cap stays in u8).
        yield (0 if count == 256 else count, bytes(buf))


class CmsisDapSwdInterface(swd.Interface):
    """SWD interface backed by CMSIS-DAP DAP_Transfer + DAP_SWJ."""

    def __init__(self, transport, capabilities: int, name: str = "swd"):
        super().__init__(name=name)
        self._transport = transport
        self._capabilities = capabilities
        # Whether the session has done DAP_Connect(SWD) yet. Done
        # lazily on the first flush so simple "info adapters"-style
        # listings don't drive the wire.
        self._connected = False

    async def start(self):
        """Bring the DAP up to a known state.

        Connect (selects SWD on the firmware side), set a default
        clock and the standard SWD framing (turnaround=1, no data
        phase), and configure transfer-level retry/idle defaults
        so :class:`SwDp`'s ``Run`` ops can be no-ops at this layer."""
        if not (self._capabilities & protocol.CAP_SWD):
            raise protocol.CmsisDapError("Adapter does not advertise SWD support")

        await self._dap_connect(protocol.PORT_SWD)
        await self._dap_swj_clock(1_000_000)  # 1 MHz default
        await self._dap_swd_configure(turnaround=1, data_phase=False)
        await self._dap_transfer_configure(
            idle_cycles=_DEFAULT_IDLE_CYCLES,
            wait_retry=_DEFAULT_WAIT_RETRY,
            match_retry=_DEFAULT_MATCH_RETRY)
        self._connected = True
        await super().start()

    async def flush_ops(self, batch):
        # Two pass: first drain SWJ-style ops in order, accumulating
        # transfers; then issue DAP_Transfer for the transfer run.
        # We flush in groups so that an SWJ-sequence between two
        # Read/Write ops happens at the right point in the wire
        # ordering — common case is "JtagToSwd at start of session
        # then a long run of transfers", but we mustn't reorder.
        groups: list[tuple[str, list]] = []
        # group is ("xfer", [(swd_op, future), ...]) or
        #          ("swj",  [(swd_op, future), ...])
        for op, future in batch:
            kind = "xfer" if isinstance(op, (swd.Read, swd.Write)) else "swj"
            if not groups or groups[-1][0] != kind:
                groups.append((kind, []))
            groups[-1][1].append((op, future))

        for kind, items in groups:
            if kind == "swj":
                await self._flush_swj_group(items)
            else:
                await self._flush_xfer_group(items)

    # -- SWJ-side ops -----------------------------------------------

    async def _flush_swj_group(self, items):
        """Lower Run/Wakeup/LineReset/JtagToSwd to DAP_SWJ_Sequence."""
        bits: list[int] = []

        for op, _future in items:
            if isinstance(op, swd.Run):
                # Idle cycles are configured globally on the firmware
                # via DAP_TransferConfigure; explicit Run ops are
                # only meaningful here if they ask for *more* than
                # that. Anything ≤ idle gets folded for free.
                if op.cycles > _DEFAULT_IDLE_CYCLES:
                    bits.extend([0] * (op.cycles - _DEFAULT_IDLE_CYCLES))
                continue

            if isinstance(op, swd.Wakeup):
                bits.extend([1] * op.cycles)
                continue

            if isinstance(op, swd.LineReset):
                bits.extend([1] * 60)
                bits.extend([0] * 8)
                continue

            if isinstance(op, swd.JtagToSwd):
                # Mirrors the J-Link sequence:
                # 250x1, switch, 50x1, switch, 200x1, 16x0
                bits.extend([1] * 250)
                bits.extend(_switch_bits())
                bits.extend([1] * 50)
                bits.extend(_switch_bits())
                bits.extend([1] * 200)
                bits.extend([0] * 16)
                continue

            _future.set_exception(TypeError(
                f"CmsisDapSwdInterface SWJ slot can't lower "
                f"{type(op).__name__}"))

        # DAP_SWJ_Sequence has its own 256-bit-per-call limit; chunk.
        for count, payload in _swj_chunks(bits):
            await self._dap_swj_sequence(count, payload)

        # All these ops resolve with no result.
        for _op, future in items:
            if not future.done():
                future.set_result(None)

    # -- Transfer-side ops ------------------------------------------

    async def _flush_xfer_group(self, items):
        """Lower a run of swd.Read / swd.Write to DAP_Transfer.

        Splits across multiple DAP_Transfer calls when the request
        or response wouldn't fit one HID packet."""
        # Pre-compute each transfer's request byte + payload bytes.
        encoded = []
        for op, future in items:
            req = self._transfer_request_byte(op)
            if isinstance(op, swd.Write):
                payload = (op.data & 0xFFFFFFFF).to_bytes(4, "little")
            else:
                payload = b""
            encoded.append((op, future, req, payload))

        # Pack into per-call batches that fit in one packet. Headers:
        # request = 3 (cmd, dap_index, count) + per-transfer (1 + payload)
        # response = 3 (cmd, count, response) + 4 * read_count
        max_req = self._transport.packet_size - 3
        max_resp = self._transport.packet_size - 3

        i = 0
        while i < len(encoded):
            batch_req_size = 0
            batch_resp_size = 0
            j = i
            while j < len(encoded):
                _, _, req, pl = encoded[j]
                t_req = 1 + len(pl)
                t_resp = 4 if (req & protocol.XFER_RnW) else 0
                if (batch_req_size + t_req > max_req
                        or batch_resp_size + t_resp > max_resp):
                    break
                batch_req_size += t_req
                batch_resp_size += t_resp
                j += 1
            if j == i:
                # Single transfer larger than a packet — shouldn't
                # happen with 4-byte payloads + plenty of MPS, but
                # surface clearly if it ever does.
                op, future, _, _ = encoded[i]
                future.set_exception(protocol.CmsisDapError(
                    "Transfer too large to fit in one CMSIS-DAP packet"))
                i += 1
                continue
            await self._issue_transfer(encoded[i:j])
            i = j

    async def _issue_transfer(self, encoded_slice):
        count = len(encoded_slice)
        req = bytearray([protocol.CMD_TRANSFER, 0, count])
        for _op, _future, request_byte, payload in encoded_slice:
            req.append(request_byte)
            req.extend(payload)

        resp = await self._transport.request(bytes(req))
        if not resp or resp[0] != protocol.CMD_TRANSFER:
            for _op, fut, _, _ in encoded_slice:
                if not fut.done():
                    fut.set_exception(protocol.CmsisDapError(
                        f"DAP_Transfer bad echo: "
                        f"{resp[:2].hex() if resp else '(empty)'}"))
            return
        actual_count = resp[1]
        last_response = resp[2]
        # 4 bytes per successful read — read the data area straight.
        cursor = 3
        ack = last_response & 0b111
        flags = last_response & ~0b111

        for idx, (op, future, request_byte, _payload) in enumerate(encoded_slice):
            if future.done():
                continue
            if idx >= actual_count:
                # Firmware bailed out on this transfer (or earlier).
                # Surface the last response — whichever ACK / error
                # caused the abort.
                future.set_exception(_xfer_exception(ack, flags))
                continue
            # Successful transfer — for reads, decode the data.
            if request_byte & protocol.XFER_RnW:
                if cursor + 4 > len(resp):
                    future.set_exception(protocol.CmsisDapError(
                        "DAP_Transfer response truncated"))
                    continue
                value = int.from_bytes(resp[cursor:cursor + 4], "little")
                cursor += 4
                future.set_result(value)
            else:
                future.set_result(None)

    @staticmethod
    def _transfer_request_byte(op) -> int:
        # Bits: 0=APnDP, 1=RnW, 2=A2, 3=A3
        request = 0
        if op.ap:
            request |= protocol.XFER_APnDP
        if isinstance(op, swd.Read):
            request |= protocol.XFER_RnW
        addr = op.addr & 0xC
        if addr & 0x4:
            request |= protocol.XFER_A2
        if addr & 0x8:
            request |= protocol.XFER_A3
        return request

    # -- Low-level command helpers ----------------------------------

    async def _dap_connect(self, port: int) -> int:
        resp = await self._transport.request(
            bytes([protocol.CMD_CONNECT, port]))
        if not resp or resp[0] != protocol.CMD_CONNECT:
            raise protocol.CmsisDapError(
                f"DAP_Connect bad echo: {resp[:2].hex() if resp else '(empty)'}")
        if resp[1] == 0:
            raise protocol.CmsisDapError(
                "DAP_Connect failed (firmware reports port=0 — unsupported)")
        return resp[1]

    async def _dap_swj_clock(self, hz: int) -> None:
        resp = await self._transport.request(
            bytes([protocol.CMD_SWJ_CLOCK]) +
            int(hz).to_bytes(4, "little"))
        self._check_status(resp, protocol.CMD_SWJ_CLOCK, "DAP_SWJ_Clock")

    async def _dap_swd_configure(self, turnaround: int, data_phase: bool) -> None:
        cfg = (turnaround - 1) & 0x3
        if data_phase:
            cfg |= 0x4
        resp = await self._transport.request(
            bytes([protocol.CMD_SWD_CONFIGURE, cfg]))
        self._check_status(resp, protocol.CMD_SWD_CONFIGURE, "DAP_SWD_Configure")

    async def _dap_transfer_configure(self, idle_cycles: int,
                                      wait_retry: int,
                                      match_retry: int) -> None:
        resp = await self._transport.request(
            bytes([protocol.CMD_TRANSFER_CONFIGURE, idle_cycles & 0xFF])
            + int(wait_retry).to_bytes(2, "little")
            + int(match_retry).to_bytes(2, "little"))
        self._check_status(resp, protocol.CMD_TRANSFER_CONFIGURE,
                           "DAP_TransferConfigure")

    async def _dap_swj_sequence(self, count: int, payload: bytes) -> None:
        resp = await self._transport.request(
            bytes([protocol.CMD_SWJ_SEQUENCE, count]) + payload)
        self._check_status(resp, protocol.CMD_SWJ_SEQUENCE, "DAP_SWJ_Sequence")

    @staticmethod
    def _check_status(resp: bytes, cmd: int, label: str) -> None:
        if not resp or resp[0] != cmd:
            raise protocol.CmsisDapError(
                f"{label} bad echo: {resp[:2].hex() if resp else '(empty)'}")
        if resp[1] != protocol.DAP_OK:
            raise protocol.CmsisDapError(
                f"{label} status=0x{resp[1]:02x}")


def _switch_bits() -> list[int]:
    """16-bit JTAG-to-SWD switch sequence (0xE79E LSB-first wire =
    0x79E7 MSB-first per ARM IHI0031)."""
    sw = 0xE79E
    return [(sw >> i) & 1 for i in range(16)]


def _xfer_exception(ack: int, flags: int) -> Exception:
    """Map a CMSIS-DAP per-transfer ACK + flags to an swd-layer
    exception (so the SwDp / Dp layer above sees a familiar error
    surface regardless of which adapter is in use)."""
    if flags & protocol.ERR_PROTOCOL:
        return swd.SwdAccessFailure(f"protocol error (flags=0x{flags:02x})")
    if flags & protocol.ERR_VALUE_MISMATCH:
        return swd.SwdAccessFailure("value-match mismatch")
    if ack == protocol.ACK_OK:
        return swd.SwdAccessFailure(
            f"transfer aborted with ACK=OK and no flag (flags=0x{flags:02x})")
    if ack == protocol.ACK_WAIT:
        return swd.SwdWait("WAIT (firmware retries exhausted)")
    if ack == protocol.ACK_FAULT:
        return swd.SwdAccessFailure("FAULT")
    if ack == protocol.ACK_NO_ACK:
        return swd.SwdAccessFailure("no ACK (target unresponsive)")
    return swd.SwdAccessFailure(
        f"invalid ACK 0b{ack:03b} (flags=0x{flags:02x})")
