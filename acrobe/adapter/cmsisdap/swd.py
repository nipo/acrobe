"""CMSIS-DAP SWD-mode Debug Port.

CMSIS-DAP probes expose a hardened **DAP-level** command surface
(``DAP_Transfer``), not a raw SWD wire. One ``DAP_Transfer``
request carries N DP/AP transactions; the firmware on the probe
handles the wire-level framing — packet building, ACK/parity,
the AP-read posted-result pipeline (`RDBUFF` insertion as
needed), inter-transfer idle cycles, WAIT retries.

We slot in as a :class:`Dp` specialization, parallel to ST-Link.
Each ``DpRead`` / ``DpWrite`` / ``ApRead`` / ``ApWrite`` becomes
one DAP_Transfer slot; the firmware delivers each read's actual
value inline. ``Run`` ops fold into the global idle-cycles
configuration set at start; ``Abort`` becomes a DAP_WriteAbort
command (the firmware-side ABORT primitive).

What we deliberately don't support on this transport:

* **Multidrop** (SWD TARGETSEL). The CMSIS-DAP command set has
  no native multidrop primitive. Trying to bit-bang a TARGETSEL
  via ``DAP_SWJ_Sequence`` works for the wire but the firmware
  has no way to thread it into subsequent ``DAP_Transfer`` calls
  with the right per-transaction selector.
* **ADIv6**. The firmware speaks the ADIv5 DPACC layout; DPv3
  registers above 4 bytes aren't reachable through DAP_Transfer.

Either of those needs a bit-bang adapter (FTDI, J-Link) instead.
"""

from __future__ import annotations

import asyncio

from ...component.arm import dp as dpmod
from . import protocol


# Idle cycles inserted by the firmware between AP accesses. The
# firmware drives them around each transfer; mirrors what
# SwDp.AP_IDLE_CYCLES does on the bit-bang side.
_DEFAULT_IDLE_CYCLES = 32

# DAP_TransferConfigure retry limits. Generous enough that we
# rarely surface a WAIT to the caller; raising further hides
# real bugs.
_DEFAULT_WAIT_RETRY  = 16
_DEFAULT_MATCH_RETRY = 16


def _swj_chunks(bits: list[int]):
    """Yield ``(count, bytes)`` tuples for ``DAP_SWJ_Sequence``,
    each capped at 256 bits per CMSIS-DAP spec (count=0 means
    256)."""
    n = len(bits)
    for off in range(0, n, 256):
        chunk = bits[off:off + 256]
        count = len(chunk)
        nb = (count + 7) // 8
        buf = bytearray(nb)
        for i, b in enumerate(chunk):
            if b:
                buf[i // 8] |= 1 << (i % 8)
        yield (0 if count == 256 else count, bytes(buf))


def _switch_bits() -> list[int]:
    """16-bit JTAG-to-SWD switch (0xE79E LSB-first wire = 0x79E7
    MSB-first per ARM IHI0031)."""
    sw = 0xE79E
    return [(sw >> i) & 1 for i in range(16)]


class CmsisDapSwDp(dpmod.Dp):
    """ARM Debug Port over CMSIS-DAP in SWD mode.

    Standard :meth:`Dp.start` lifecycle (DPIDR read, power up,
    AP enumeration) works unchanged: the inherited base posts
    DpRead/DpWrite/ApRead/ApWrite/Abort/Run ops, our
    :meth:`flush_ops` lowers each into ``DAP_Transfer`` slots,
    and the firmware does the wire."""

    SELECT_REG = 0x08

    def __init__(self, transport, capabilities: int, name: str = "swd"):
        super().__init__(name=name)
        self.__transport = transport
        self.__capabilities = capabilities
        self.__connected = False
        # DP-side SELECT cache. The firmware doesn't track it for
        # us; we maintain the same logic SwDp uses. Initial value
        # 0 matches the DP's reset state.
        self.__select: int = 0

    def __select_for(self, op) -> int:
        """SELECT value required to access ``op``'s register.

        AP ops carry ``(apsel << 24) | reg_offset``; APSEL lands in
        SELECT[31:24], APBANKSEL in SELECT[7:4]. DP ops only set
        DPBANKSEL (low nibble); APSEL / APBANKSEL stick from the
        last AP access."""
        cur = self.__select
        if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
            apsel = (op.addr >> 24) & 0xff
            apbank = (op.addr >> 4) & 0xf
            return (apsel << 24) | (apbank << 4) | (cur & 0xf)
        return (cur & 0xFFFFFFF0) | ((op.addr >> 4) & 0xf)

    async def start(self):
        """Bring the wire up via the CMSIS-DAP command set, then
        run the standard :class:`Dp.start` (DPIDR + power-up + AP
        enumeration)."""
        if not (self.__capabilities & protocol.CAP_SWD):
            raise protocol.CmsisDapError(
                "Adapter does not advertise SWD support")

        await self.__dap_connect(protocol.PORT_SWD)
        await self.__dap_swj_clock(1_000_000)
        await self.__dap_swd_configure(turnaround=1, data_phase=False)
        await self.__dap_transfer_configure(
            idle_cycles=_DEFAULT_IDLE_CYCLES,
            wait_retry=_DEFAULT_WAIT_RETRY,
            match_retry=_DEFAULT_MATCH_RETRY)
        # JTAG-to-SWD switch sequence: 250×1, switch, 50×1,
        # switch, 200×1, 16×0. Same bit pattern J-Link / FTDI
        # bit-bang; CMSIS-DAP issues it via DAP_SWJ_Sequence.
        bits: list[int] = []
        bits.extend([1] * 250)
        bits.extend(_switch_bits())
        bits.extend([1] * 50)
        bits.extend(_switch_bits())
        bits.extend([1] * 200)
        bits.extend([0] * 16)
        for count, payload in _swj_chunks(bits):
            await self.__dap_swj_sequence(count, payload)
        self.__connected = True

        await super().start()

    async def stop(self):
        if self.__connected:
            try:
                await self.__transport.request(
                    bytes([protocol.CMD_DISCONNECT]))
            except Exception:
                pass
            self.__connected = False

    def option_set(self, key, value):
        if key in ("targetsel", "multidrop"):
            raise ValueError(
                f"swd: {key}=… not supported on CMSIS-DAP (the "
                "firmware command set has no native multidrop "
                "primitive); use a bit-bang adapter (FTDI / "
                "J-Link) for multidrop targets.")

    # --- DP lowering ----------------------------------------------

    async def flush_ops(self, batch):
        """Lower a batch of DP/AP ops to DAP_Transfer calls.

        AP-read pipelining is firmware-side: each read's actual
        value comes back inline in the response — no host-side
        RDBUFF insertion needed.

        SELECT-cache management is host-side: the firmware just
        relays DP/AP register accesses without knowing about
        DPBANKSEL / APSEL / APBANKSEL. We insert a DP write to
        SELECT whenever a per-op decode needs a different value."""
        # Group: contiguous runs of "transfer-shaped" ops are
        # issued via DAP_Transfer; ABORTs and runs that need
        # special handling break the run.
        encoded: list[tuple] = []  # (op, future, request_byte, payload)
        select = self.__select

        async def flush_run():
            nonlocal encoded
            if not encoded:
                return
            await self.__flush_xfer_group(encoded)
            encoded = []

        def emit_select_if_changed(op):
            """Compare the op's required SELECT against the cache;
            queue a DP write to SELECT if they differ. The DP write
            future is fire-and-forget — wire faults still surface
            via the user-facing op's own DAP_Transfer slot."""
            nonlocal select
            new_select = self.__select_for(op)
            if select == new_select:
                return
            loop = asyncio.get_running_loop()
            placeholder = loop.create_future()
            # Consume any exception so asyncio doesn't warn about
            # an unretrieved Future exception — wire faults still
            # surface via the user op that triggered this SELECT
            # write.
            placeholder.add_done_callback(lambda f: f.exception())
            req, payload = self.__encode_transfer_raw(
                ap=False, read=False, addr=self.SELECT_REG,
                data=new_select)
            encoded.append((None, placeholder, req, payload))
            select = new_select

        for op, future in batch:
            if isinstance(op, dpmod.Run):
                # The firmware already inserts idle cycles between
                # transfers via DAP_TransferConfigure; an explicit
                # Run is meaningful only if it asks for *more* than
                # the configured idle. Anything ≤ idle gets folded
                # for free.
                if op.cycles > _DEFAULT_IDLE_CYCLES:
                    await flush_run()
                    extra = op.cycles - _DEFAULT_IDLE_CYCLES
                    bits = [0] * extra
                    for count, payload in _swj_chunks(bits):
                        await self.__dap_swj_sequence(count, payload)
                future.set_result(None)
                continue

            if isinstance(op, dpmod.Abort):
                # DAP_WriteAbort is its own command — break the
                # transfer run.
                await flush_run()
                try:
                    await self.__dap_write_abort(op.what)
                    if not future.done():
                        future.set_result(None)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                continue

            if isinstance(op, (dpmod.DpRead, dpmod.DpWrite,
                                dpmod.ApRead, dpmod.ApWrite)):
                emit_select_if_changed(op)
                req, payload = self.__encode_transfer(op)
                encoded.append((op, future, req, payload))
                continue

            if not future.done():
                future.set_exception(TypeError(
                    f"CmsisDapSwDp can't lower {type(op).__name__}"))

        await flush_run()
        self.__select = select

    # --- DAP_Transfer encoding ------------------------------------

    @classmethod
    def __encode_transfer(cls, op) -> tuple[int, bytes]:
        """Build the (request_byte, payload) pair for one
        DAP_Transfer slot."""
        is_ap = isinstance(op, (dpmod.ApRead, dpmod.ApWrite))
        is_read = isinstance(op, (dpmod.DpRead, dpmod.ApRead))
        data = 0 if is_read else op.data
        return cls.__encode_transfer_raw(
            ap=is_ap, read=is_read, addr=op.addr, data=data)

    @staticmethod
    def __encode_transfer_raw(*, ap: bool, read: bool,
                              addr: int, data: int) -> tuple[int, bytes]:
        """Pack one DAP_Transfer slot. ``request_byte`` bits:

        * 0 = APnDP
        * 1 = RnW
        * 2 = A2
        * 3 = A3
        """
        request = 0
        if ap:
            request |= protocol.XFER_APnDP
        if read:
            request |= protocol.XFER_RnW
        # Both DP and AP register addresses fit the same A[3:2]
        # field. For AP ops the upper bits of addr (apsel +
        # apbank) are handled by SELECT writes — see
        # __select_for / flush_ops.
        lo = addr & 0xC
        if lo & 0x4:
            request |= protocol.XFER_A2
        if lo & 0x8:
            request |= protocol.XFER_A3
        if read:
            payload = b""
        else:
            payload = (data & 0xFFFFFFFF).to_bytes(4, "little")
        return request, payload

    async def __flush_xfer_group(self, encoded):
        # Pack into per-call batches that fit in one packet.
        max_req = self.__transport.packet_size - 3
        max_resp = self.__transport.packet_size - 3

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
                op, future, _, _ = encoded[i]
                if not future.done():
                    future.set_exception(protocol.CmsisDapError(
                        "Transfer too large to fit in one "
                        "CMSIS-DAP packet"))
                i += 1
                continue
            await self.__issue_transfer(encoded[i:j])
            i = j

    async def __issue_transfer(self, encoded_slice):
        count = len(encoded_slice)
        req = bytearray([protocol.CMD_TRANSFER, 0, count])
        for _op, _future, request_byte, payload in encoded_slice:
            req.append(request_byte)
            req.extend(payload)

        resp = await self.__transport.request(bytes(req))
        if not resp or resp[0] != protocol.CMD_TRANSFER:
            for _op, fut, _, _ in encoded_slice:
                if not fut.done():
                    fut.set_exception(protocol.CmsisDapError(
                        f"DAP_Transfer bad echo: "
                        f"{resp[:2].hex() if resp else '(empty)'}"))
            return
        actual_count = resp[1]
        last_response = resp[2]
        cursor = 3
        ack = last_response & 0b111
        flags = last_response & ~0b111

        for idx, (op, future, request_byte, _payload) in enumerate(
                encoded_slice):
            if future.done():
                continue
            if idx >= actual_count:
                future.set_exception(
                    _xfer_exception_dp(ack, flags))
                continue
            if request_byte & protocol.XFER_RnW:
                if cursor + 4 > len(resp):
                    future.set_exception(protocol.CmsisDapError(
                        "DAP_Transfer response truncated"))
                    continue
                value = int.from_bytes(
                    resp[cursor:cursor + 4], "little")
                cursor += 4
                future.set_result(value)
            else:
                future.set_result(None)

    # --- Low-level CMSIS-DAP command helpers ----------------------

    async def __dap_connect(self, port: int) -> int:
        resp = await self.__transport.request(
            bytes([protocol.CMD_CONNECT, port]))
        if not resp or resp[0] != protocol.CMD_CONNECT:
            raise protocol.CmsisDapError(
                f"DAP_Connect bad echo: "
                f"{resp[:2].hex() if resp else '(empty)'}")
        if resp[1] == 0:
            raise protocol.CmsisDapError(
                "DAP_Connect failed (firmware reports port=0 — "
                "unsupported)")
        return resp[1]

    async def __dap_swj_clock(self, hz: int) -> None:
        resp = await self.__transport.request(
            bytes([protocol.CMD_SWJ_CLOCK])
            + int(hz).to_bytes(4, "little"))
        self.__check_status(resp, protocol.CMD_SWJ_CLOCK, "DAP_SWJ_Clock")

    async def __dap_swd_configure(self, turnaround: int,
                                  data_phase: bool) -> None:
        cfg = (turnaround - 1) & 0x3
        if data_phase:
            cfg |= 0x4
        resp = await self.__transport.request(
            bytes([protocol.CMD_SWD_CONFIGURE, cfg]))
        self.__check_status(
            resp, protocol.CMD_SWD_CONFIGURE, "DAP_SWD_Configure")

    async def __dap_transfer_configure(self, idle_cycles: int,
                                       wait_retry: int,
                                       match_retry: int) -> None:
        resp = await self.__transport.request(
            bytes([protocol.CMD_TRANSFER_CONFIGURE, idle_cycles & 0xFF])
            + int(wait_retry).to_bytes(2, "little")
            + int(match_retry).to_bytes(2, "little"))
        self.__check_status(resp, protocol.CMD_TRANSFER_CONFIGURE,
                            "DAP_TransferConfigure")

    async def __dap_swj_sequence(self, count: int,
                                 payload: bytes) -> None:
        resp = await self.__transport.request(
            bytes([protocol.CMD_SWJ_SEQUENCE, count]) + payload)
        self.__check_status(resp, protocol.CMD_SWJ_SEQUENCE,
                            "DAP_SWJ_Sequence")

    async def __dap_write_abort(self, value: int) -> None:
        # DAP_WriteAbort: command + DAP index + ABORT register
        # value (32-bit little-endian).
        resp = await self.__transport.request(
            bytes([protocol.CMD_WRITE_ABORT, 0])
            + int(value & 0xFFFFFFFF).to_bytes(4, "little"))
        self.__check_status(resp, protocol.CMD_WRITE_ABORT,
                            "DAP_WriteAbort")

    @staticmethod
    def __check_status(resp: bytes, cmd: int, label: str) -> None:
        if not resp or resp[0] != cmd:
            raise protocol.CmsisDapError(
                f"{label} bad echo: "
                f"{resp[:2].hex() if resp else '(empty)'}")
        if resp[1] != protocol.DAP_OK:
            raise protocol.CmsisDapError(
                f"{label} status=0x{resp[1]:02x}")


def _xfer_exception_dp(ack: int, flags: int) -> Exception:
    """Map a CMSIS-DAP per-transfer ACK + flags to a DP-layer
    exception."""
    if flags & protocol.ERR_PROTOCOL:
        return dpmod.DpAccessFailure(
            f"protocol error (flags=0x{flags:02x})")
    if flags & protocol.ERR_VALUE_MISMATCH:
        return dpmod.DpAccessFailure("value-match mismatch")
    if ack == protocol.ACK_OK:
        return dpmod.DpAccessFailure(
            f"transfer aborted with ACK=OK and no flag "
            f"(flags=0x{flags:02x})")
    if ack == protocol.ACK_WAIT:
        return dpmod.DpAccessFailure(
            "WAIT (firmware retries exhausted)")
    if ack == protocol.ACK_FAULT:
        return dpmod.DpAccessFailure("FAULT")
    if ack == protocol.ACK_NO_ACK:
        return dpmod.DpAccessFailure("no ACK (target unresponsive)")
    return dpmod.DpAccessFailure(
        f"invalid ACK 0b{ack:03b} (flags=0x{flags:02x})")
