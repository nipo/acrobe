"""Bit-level JTAG interface backed by the XDS110 firmware.

The XDS110 firmware is state-aware rather than bit-bang: it exposes
``XDS_GOTO_STATE(target, transit)``, ``XDS_JTAG_SCAN(shift_state,
bits, end_state)``, ``XDS_CYCLE_TCK(count)``, and ``OCD_PATHMOVE(path)``
as its JTAG vocabulary. This interface translates acrobe's
:class:`~acrobe.protocol.jtag.JtagInterface` ops into those primitives.

Translation rules
-----------------

* ``Reset(count)`` → ``GOTO_STATE(RESET)`` + ``CYCLE_TCK(count-5)``.
  Test-Logic-Reset is stable under TMS=1 so the firmware holds TMS high
  through the trailing cycles.

* ``Run(cycles)`` → ``GOTO_STATE(IDLE)`` (only if parked at
  ``PAUSE_DR|IR``) + ``CYCLE_TCK(cycles)``.

* ``CaptureDr`` / ``CaptureIr`` → ``GOTO_STATE(PAUSE_DR|IR,
  transit=VIA_CAPTURE)``. The ``VIA_CAPTURE`` transit makes the firmware
  navigate through the Capture state regardless of current position,
  covering both the normal pre-shift Capture and the IEEE-1149.7
  Zero-Bit Scan (standalone Capture) uniformly. Sets ``_shift_state``
  to ``SHIFT_DR|IR`` for the following ``Shift``.

* ``Shift(tdi, …, post_run=N)`` → ``XDS_JTAG_SCAN(shift_state, tdi,
  PAUSE_DR|IR)`` always ending at PAUSE (``PAUSE == SHIFT + 2`` in the
  ``JtagState`` enum). With ``post_run``: end at ``IDLE`` +
  ``CYCLE_TCK(N)`` instead.

* ``SwdToJtag`` → two ``OCD_PATHMOVE`` calls expressing the ARM SWJ-DP
  line-reset + JTAG-select TMS pattern as state-machine transitions,
  bracketed by ``GOTO_STATE(RESET)`` + ``CYCLE_TCK(50)`` on each side.

* JTAG engine arming: ``CJTAG_CONNECT(MODE_JTAG)`` is issued once, on
  the first ``flush_ops`` call, to arm the firmware's JTAG dispatch
  path.

Limitation: scans are issued one ``XDS_JTAG_SCAN`` per ``Shift`` (no
batching via ``OCD_SCAN_REQUEST`` yet)."""

from __future__ import annotations

from ...bitstring import BitString
from ...protocol import jtag
from . import protocol


class XDS110JtagInterface(jtag.JtagInterface):
    """JTAG interface implemented on top of XDS110 firmware commands."""

    # Cap a single XDS_JTAG_SCAN at the firmware data buffer; longer
    # shifts would need to be split with PAUSE_DR/IR park states. Hasn't
    # been needed yet (typical IR/DR widths sit well below 32 kbit).
    _MAX_SCAN_BITS = protocol.MAX_DATA_BLOCK * 8

    def __init__(self, transport, version: protocol.Version,
                 *, initial_delay_count: int, name: str = "jtag"):
        super().__init__(name=name)
        self._transport = transport
        self._firmware = version.firmware
        self._delay_count = initial_delay_count
        self._tck_dirty = False
        # Deferred arm: CJTAG_CONNECT(MODE_JTAG) is issued once on the
        # first flush rather than at construction so the adapter open
        # sequence and the jtag interface remain independent.
        self._inited = False
        # SHIFT_DR / SHIFT_IR while parked at PAUSE_DR/IR after a
        # Capture; False/None when at IDLE or RESET.
        self._shift_state = False
        # XDS110 fast-mode peak ceiling. The bit-level layer publishes
        # this so FreqCapper clamps user requests rather than handing
        # us a value the firmware can't honour.
        if version.firmware >= protocol.FAST_TCK_FIRMWARE_VERSION:
            self.max_freq = protocol.MAX_FAST_TCK_KHZ * 1000
        else:
            self.max_freq = protocol.MAX_SLOW_TCK_KHZ * 1000

    # ------------------------------------------------------------------
    # FreqCapper hook
    # ------------------------------------------------------------------

    def freq_update(self, freq):
        """Translate a desired TCK rate into a delay-count and queue
        an ``XDS_SET_TCK`` for the next flush. ``None`` keeps the
        currently-loaded rate (set by :class:`XDS110Adapter` on open)
        and lets FreqCapper report the channel as unconstrained."""
        if freq is None:
            return None
        delay_count, achieved_khz = protocol.TckDelay.for_freq(
            int(freq), self._firmware)
        if delay_count != self._delay_count:
            self._delay_count = delay_count
            self._tck_dirty = True
        return achieved_khz * 1000

    # ------------------------------------------------------------------
    # Op translation
    # ------------------------------------------------------------------

    async def flush_ops(self, batch):
        if self._tck_dirty:
            self._tck_dirty = False
            await self._set_tck(self._delay_count)

        if not self._inited:
            self._inited = True
            await self._cjtag_connect_jtag()

        captured: dict[int, BitString] = {}

        n = len(batch)
        for i, (op, _future) in enumerate(batch):
            if isinstance(op, jtag.Reset):
                await self._goto_state(protocol.JtagState.RESET)
                # TLR is stable under TMS=1; the long-form count keeps
                # TMS high for the required number of cycles.
                extra = max(0, op.count - 5)
                if extra:
                    await self._cycle_tck(extra)
                self._shift_state = None

            elif isinstance(op, jtag.SwdToJtag):
                # ARM SWJ-DP line-reset + JTAG-select TMS pattern
                # expressed as state-machine transitions.
                await self._goto_state(protocol.JtagState.RESET)
                await self._cycle_tck(50)
                await self._pathmove([
                    protocol.JtagState.IDLE,
                    protocol.JtagState.IDLE,
                    protocol.JtagState.SELECT_DR,
                    protocol.JtagState.SELECT_IR,
                    protocol.JtagState.RESET,
                    protocol.JtagState.RESET,
                    protocol.JtagState.IDLE,
                    protocol.JtagState.IDLE,
                ])
                await self._pathmove([
                    protocol.JtagState.SELECT_DR,
                    protocol.JtagState.SELECT_IR,
                    protocol.JtagState.RESET,
                    protocol.JtagState.IDLE,
                    protocol.JtagState.IDLE,
                    protocol.JtagState.SELECT_DR,
                    protocol.JtagState.SELECT_IR,
                    protocol.JtagState.RESET,
                ])
                await self._goto_state(protocol.JtagState.RESET)
                await self._cycle_tck(50)
                self._shift_state = None

            elif isinstance(op, jtag.Run):
                if self._shift_state:
                    await self._goto_state(protocol.JtagState.IDLE)
                    self._shift_state = None
                if op.cycles > 0:
                    await self._cycle_tck(op.cycles)

            elif isinstance(op, jtag.CaptureDr):
                # VIA_CAPTURE navigates through Capture-DR from any
                # current state, covering both normal pre-shift Capture
                # and Zero-Bit Scan (standalone Capture).
                self._shift_state = protocol.JtagState.SHIFT_DR
                await self._goto_state(protocol.JtagState.PAUSE_DR,
                                       transit=protocol.JtagTransit.VIA_CAPTURE)

            elif isinstance(op, jtag.CaptureIr):
                self._shift_state = protocol.JtagState.SHIFT_IR
                await self._goto_state(protocol.JtagState.PAUSE_IR,
                                       transit=protocol.JtagTransit.VIA_CAPTURE)

            elif isinstance(op, jtag.Shift):
                if len(op.tdi):
                    # PAUSE_DR = SHIFT_DR + 2, PAUSE_IR = SHIFT_IR + 2.
                    tdo = await self._jtag_scan(self._shift_state, op.tdi,
                                                self._shift_state + 2)
                    if op.read_tdo:
                        captured[i] = tdo
                if op.post_run:
                    await self._goto_state(protocol.JtagState.IDLE)
                    await self._cycle_tck(op.post_run)
                    self._shift_state = None

            else:
                raise ValueError(
                    f"XDS110: unsupported JTAG op {type(op).__name__}")

        for idx, (_op, fut) in enumerate(batch):
            if fut is None or fut.done():
                continue
            fut.set_result(captured.get(idx))

    # ------------------------------------------------------------------
    # Wire-level command helpers
    # ------------------------------------------------------------------

    async def _set_tck(self, delay_count: int) -> None:
        payload = (bytes([protocol.Opcode.XDS_SET_TCK])
                   + protocol.Bytes.pack_u32(delay_count))
        await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    async def _cjtag_connect_jtag(self) -> None:
        payload = (bytes([protocol.Opcode.CJTAG_CONNECT])
                   + protocol.Bytes.pack_u32(protocol.MODE_JTAG))
        await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    async def _pathmove(self, states: list) -> None:
        """Emit an exact TMS sequence via OCD_PATHMOVE. Each consecutive
        pair of states must be reachable by a single TMS bit; the firmware
        visits them in order, including transient states."""
        payload = (bytes([protocol.Opcode.OCD_PATHMOVE])
                   + protocol.Bytes.pack_u32(len(states))
                   + bytes(int(s) for s in states))
        await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    async def _goto_state(self, state: int,
                          transit: int = protocol.JtagTransit.QUICKEST) -> None:
        payload = (bytes([protocol.Opcode.XDS_GOTO_STATE])
                   + protocol.Bytes.pack_u32(state)
                   + protocol.Bytes.pack_u32(transit))
        await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    async def _cycle_tck(self, count: int) -> None:
        payload = (bytes([protocol.Opcode.XDS_CYCLE_TCK])
                   + protocol.Bytes.pack_u32(count))
        await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    async def _jtag_scan(self, shift_state: int, tdi: BitString,
                         end_state: int) -> BitString:
        nbits = len(tdi)
        if nbits > self._MAX_SCAN_BITS:
            raise ValueError(
                f"XDS110: shift {nbits} bits exceeds firmware "
                f"buffer ({self._MAX_SCAN_BITS} bits) — split via "
                f"PAUSE-DR/IR park states is not implemented yet")
        nbytes = (nbits + 7) // 8

        # XDS_JTAG_SCAN parameter block (18 bytes after the opcode):
        #   bits(u16) | path(u8) | trans1(u8) | end(u8) | trans2(u8)
        #   | pre(u16) | pos(u16) | delay(u16) | reps(u16)
        #   | out_offset(u16) | in_offset(u16) | data_out[total_bytes]
        # ``reps`` is fixed at 1; ``out_offset``/``in_offset`` are
        # only meaningful when ``reps>1`` but the firmware expects
        # them to equal total_bytes regardless.
        payload = (bytes([protocol.Opcode.XDS_JTAG_SCAN])
                   + protocol.Bytes.pack_u16(nbits)
                   + bytes([shift_state, protocol.JtagTransit.QUICKEST,
                            end_state, protocol.JtagTransit.QUICKEST])
                   + protocol.Bytes.pack_u16(0)        # pre
                   + protocol.Bytes.pack_u16(0)        # pos
                   + protocol.Bytes.pack_u16(0)        # delay
                   + protocol.Bytes.pack_u16(1)        # reps
                   + protocol.Bytes.pack_u16(nbytes)   # out_offset
                   + protocol.Bytes.pack_u16(nbytes)   # in_offset
                   + bytes(tdi).ljust(nbytes, b'\x00'))

        result = await self._transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN + nbytes)
        return BitString(result, nbits)
