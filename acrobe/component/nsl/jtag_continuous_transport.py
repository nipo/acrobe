"""Host (ATE) driver for ``nsl_jtag.continuous_transport``.

The FPGA-side block keeps a JTAG TAP in a single continuous Shift-DR run
and streams bytes both ways over it: TDI carries ATE→TAP, TDO carries
TAP→ATE, sharing TCK. One uninterrupted Shift-DR run is a **batch**. The
TAP state machine no longer frames anything, so byte framing, flow
control and truncation-safety are rebuilt in-band. The full protocol is
specified in ``nsl/lib/nsl_jtag/continuous_transport/continuous_transport.md``;
this module is the matching host end.

`ContinuousTransport` is a :class:`~acrobe.protocol.datagram.Datagram`
and a *client of the JTAG TAP API*: it owns no JTAG geometry and drives
the link purely through ``tap.ir(user_ir)`` DR shifts. Each batch is one
such shift — Capture-DR, one long Shift-DR carrying the wire stream,
Update-DR — issued by the parent :class:`~acrobe.protocol.jtag.Tap`.

The link only moves data while it is being clocked, and the peer may
have unsolicited frames to deliver at any time. So the bridge is driven
by a **background worker** that runs batches continuously once the node
starts — fast, back-to-back, while there is TX queued, a receiver
waiting, or a frame mid-reassembly; and at a slow idle poll otherwise,
so unsolicited RX still flows when nobody is reading. `flush_ops` never
shifts: it only queues sends / receivers and wakes the worker, which
resolves their futures as the bytes actually move.

Per batch the worker:

* emits, on TDI: preamble (``0x55``×P), SOF (``0xd5``), a TX-budget grant
  so the TAP may send this batch, then app data frames bounded by the RX
  credit the TAP last advertised, then idle filler that honours the
  budget promise (``budget·8 + margin`` further TCK cycles);
* parses, from TDO: bit-searches the preamble→SOF lock, then counts
  bytes and decodes data / credit / tx-level / idle frames.

Flow control (spec §6):

* **ATE→TAP** is gated by *RX credit* — free space in the TAP's RX FIFO,
  advertised absolutely on TDO. In-flight bytes from the current batch
  are not yet reflected, so the host debits them locally.
* **TAP→ATE** is gated by the *TX budget* the host grants on TDI; the
  budget resets to zero at every Capture-DR, so the host re-grants each
  batch and clocks enough trailing cycles to honour it.
"""

from __future__ import annotations

import asyncio
from collections import deque

from ...bitstring import BitString
from ...protocol.datagram import Datagram, Recv, Send


class _TxItem:
    """One queued outgoing datagram. ``sent`` tracks how many of its
    bytes have already been framed onto the wire; ``future`` resolves
    once the final byte (carrying end-of-packet) has been shifted."""

    __slots__ = ("data", "sent", "future")

    def __init__(self, data: bytes, future):
        self.data = data
        self.sent = 0
        self.future = future


class ContinuousTransport(Datagram):
    """Datagram channel over an ``nsl_jtag.continuous_transport`` slave.

    Parent is the :class:`~acrobe.protocol.jtag.Tap` whose ``user_ir``
    selects the continuous-transport data register.
    """

    # Wire constants (spec §4 / continuous_transport.pkg.vhd).
    PREAMBLE = 0x55
    SOF = 0xd5
    IDLE = 0xf0
    CREDIT = 0xf1            # +2 bytes LE
    TX_LEVEL = 0xf2          # +2 bytes LE (TDO only)
    SET_PAD_BASE = 0xf8      # | pad(0..7); TDI only

    DATA_CTRL_BIT = 0x80     # 1 => control frame
    DATA_LAST_BIT = 0x40     # within a data header: end-of-packet
    DATA_LEN_MASK = 0x3f     # length-1
    DATA_BYTES_MAX = 64

    # Pessimistic TAP-internal pipeline latency, in TCK cycles, matching
    # tap_tx_latency_c / tap_rx_latency_c in the RTL (spec §6.2).
    TAP_TX_LATENCY = 32
    TAP_RX_LATENCY = 32

    # The preamble→SOF lock sits at the very start of a batch's TDO
    # (only the pad, preamble and the Capture-DR entry precede it), so
    # the search is bounded to the first few bytes.
    SYNC_SEARCH_BITS = 64

    # Inherent TDO bit offset the serializer adds with pad value 0: its
    # ST_PAD stage always emits one cycle. The default pad cancels it.
    SERIALIZER_PAD_BITS = 1

    def __init__(self, tap, user_ir, *, name: str = "continuous_transport",
                 preamble_count: int = 2, tx_budget: int = 64,
                 idle_poll_interval: float = 0.05):
        super().__init__(name)
        self._tap = tap
        self._user = tap.ir(user_ir)
        self._preamble_count = max(2, int(preamble_count))
        self._tx_budget = int(tx_budget)
        self._idle_poll_interval = float(idle_poll_interval)

        # 16-bit preamble→SOF lock pattern. JTAG is LSB-first, so the
        # preamble is the low byte and the SOF the high byte; this is the
        # window the deserializer matches against.
        self._sync = BitString(self.PREAMBLE | (self.SOF << 8), 16)

        # Trailing slack (bytes) appended after the granted payload so
        # the budget·8 + margin promise holds and the TAP's own
        # preamble/SOF/pipeline fit before its payload is clocked out.
        # BYPASS latency (U/D) is absorbed transparently by the Tap/Chain
        # layer, so it plays no part here.
        self._margin_bytes = (self.TAP_TX_LATENCY + 7) // 8 \
            + self._preamble_count + 3

        # Trailing idle (bytes) the host must clock *after its own last
        # data byte* so the slave's RX deserializer→deframer pipeline
        # flushes that byte before Update-DR. Spec §2: payload must be
        # kept out of the untransmitted batch tail; with U=0 that tail is
        # exactly this pipeline depth. Covers tap_rx_latency_c plus the
        # byte-framing/CDC stages, with slack (generosity is ~free).
        self._rx_flush_bytes = (self.TAP_RX_LATENCY + 7) // 8 + 3

        # TDO alignment pad (spec §7). The serializer's ST_PAD stage runs
        # one cycle even for pad value 0, so the TDO SOF lands one bit late
        # by default; preselect the pad that cancels that off-by-one so the
        # payload is byte-aligned from the first padded batch. BYPASS
        # latency (dr_pre) is already removed by the Chain's TDO slice, so
        # it does not enter here — only the serializer's own +1 does.
        self._tdo_pad = (-self.SERIALIZER_PAD_BITS) & 0x7
        # A set-pad frame is queued (``_pad_dirty``) and, once shifted,
        # applies only to the *next* batch's TDO; ``_pad_settle`` counts
        # the batches to wait for that before measuring again, so we never
        # stack two corrections (which made the pad march endlessly).
        self._pad_dirty = True
        self._pad_settle = 0

        # RX credit (host→TAP allowance) is *running* across batches.
        self._rx_credit = 0
        # Outgoing datagrams awaiting transmission (FIFO).
        self._tx_queue: deque[_TxItem] = deque()
        # Inbound reassembly: bytes of the in-progress datagram, the queue
        # of completed-but-undelivered ones, and receivers blocked on a
        # frame.
        self._rx_partial = bytearray()
        self._rx_frames: deque[bytes] = deque()
        self._recv_waiters: deque = deque()
        # Last TAP backlog advertised on TDO (diagnostic / pacing hint).
        self._tx_level: int | None = None

        # Worker plumbing.
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._closed = False
        self._error: Exception | None = None

    # --- Lifecycle ---------------------------------------------------

    async def start(self):
        from ...lifecycle import on_shutdown
        on_shutdown(self.close)
        self._ensure_worker()

    async def stop(self):
        await self.close()

    async def close(self):
        from ...lifecycle import cancel_shutdown
        cancel_shutdown(self.close)
        self._closed = True
        self._wake.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _ensure_worker(self):
        if self._error is not None or self._closed:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    def _wake_worker(self):
        self._wake.set()

    # --- Datagram flush (no IO — just enqueue and wake) --------------

    async def flush_ops(self, batch):
        if self._error is not None:
            for _op, future in batch:
                if not future.done():
                    future.set_exception(self._error)
            return

        self._ensure_worker()
        woke = False
        for op, future in batch:
            if isinstance(op, Send):
                if not op.data:
                    if not future.done():
                        future.set_exception(ValueError(
                            "ContinuousTransport cannot send a zero-length "
                            "datagram (no zero-byte frame on the wire)"))
                    continue
                self._tx_queue.append(_TxItem(bytes(op.data), future))
                woke = True
            elif isinstance(op, Recv):
                if self._rx_frames:
                    if not future.done():
                        future.set_result((self._rx_frames.popleft(), None))
                else:
                    self._recv_waiters.append(future)
                    woke = True
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"ContinuousTransport: unsupported op "
                        f"{type(op).__name__}"))
        if woke:
            self._wake_worker()

    # --- Background worker -------------------------------------------

    def _active(self) -> bool:
        """Whether there is in-flight work that wants back-to-back
        batches: queued TX, a blocked receiver, or a datagram still being
        reassembled across batches."""
        return bool(self._tx_queue) or bool(self._recv_waiters) \
            or bool(self._rx_partial)

    async def _worker(self):
        try:
            while not self._closed:
                await self._run_batch()
                self._dispatch_rx()
                if not self._active():
                    await self._idle_wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    async def _idle_wait(self):
        """Sleep up to one idle poll interval, but wake immediately when
        a send / receiver is posted. Even when idle the worker keeps
        polling so the peer's unsolicited frames are picked up."""
        self._wake.clear()
        if self._active():
            return
        try:
            await asyncio.wait_for(self._wake.wait(),
                                   self._idle_poll_interval)
        except asyncio.TimeoutError:
            pass

    async def _run_batch(self):
        tdi, sent, done = self._build_tdi()
        self.logger.protocol("< %s", tdi.hex())
        tdo = await self._user(
            BitString(bytes(tdi), len(tdi) * 8), read_tdo=True)
        # Bytes are on the wire now: the completed datagrams' futures can
        # resolve, and TDO can be decoded.
        for future in done:
            if not future.done():
                future.set_result(None)
        self._parse_tdo(tdo, sent)

    def _dispatch_rx(self):
        """Hand completed inbound frames to blocked receivers, in order.
        Frames with no waiter stay buffered for a future recv."""
        while self._recv_waiters and self._rx_frames:
            future = self._recv_waiters[0]
            if future.done():           # cancelled receiver: drop it
                self._recv_waiters.popleft()
                continue
            self._recv_waiters.popleft()
            future.set_result((self._rx_frames.popleft(), None))

    def _fail(self, exc: Exception):
        self._error = exc
        for item in self._tx_queue:
            if not item.future.done():
                item.future.set_exception(exc)
        self._tx_queue.clear()
        for future in self._recv_waiters:
            if not future.done():
                future.set_exception(exc)
        self._recv_waiters.clear()
        self.logger.error("continuous_transport worker stopped: %r", exc)

    # --- TDI build / TDO parse ---------------------------------------

    def _build_tdi(self) -> tuple[bytearray, int, list]:
        buf = bytearray()
        buf += bytes([self.PREAMBLE]) * self._preamble_count
        buf.append(self.SOF)

        if self._pad_dirty:
            buf.append(self.SET_PAD_BASE | (self._tdo_pad & 0x7))
            self._pad_dirty = False
            # Commits on this batch's Update-DR and applies from the next
            # batch's preamble; wait one batch before measuring again.
            self._pad_settle = 1

        budget = self._tx_budget
        buf += bytes([self.CREDIT, budget & 0xff, (budget >> 8) & 0xff])

        sent = 0
        done: list = []
        # Drain queued datagrams into data frames (<=64 bytes each),
        # bounded by the running RX credit. A frame carries end-of-packet
        # only on the datagram's final byte.
        while self._tx_queue and (self._rx_credit - sent) > 0:
            item = self._tx_queue[0]
            remaining = len(item.data) - item.sent
            n = min(remaining, self.DATA_BYTES_MAX, self._rx_credit - sent)
            is_last = (item.sent + n == len(item.data))
            header = (self.DATA_LAST_BIT if is_last else 0) | (n - 1)
            buf.append(header)
            buf += item.data[item.sent:item.sent + n]
            item.sent += n
            sent += n
            if item.sent == len(item.data):
                self._tx_queue.popleft()
                done.append(item.future)

        # Fill with idle to satisfy both, whichever is larger:
        #  * RX flush — at least `_rx_flush_bytes` idle *after the host's
        #    own last data byte*, so the slave clocks it through before the
        #    batch closes (otherwise the final byte strands in the tail and
        #    the next batch's first byte is mis-framed in its place);
        #  * TX budget promise — budget·8 + margin TCK cycles after the
        #    grant, so the TAP can return up to `budget` bytes this batch.
        target = max(len(buf) + self._rx_flush_bytes,
                     self._preamble_count + 1 + budget + self._margin_bytes)
        if len(buf) < target:
            buf += bytes([self.IDLE]) * (target - len(buf))

        return buf, sent, done

    def _parse_tdo(self, tdo: BitString, sent: int):
        off = self._find_sof(tdo)
        if off is None:
            self.logger.protocol("no SOF lock in %d-bit TDO batch", len(tdo))
            self._update_credit(None, sent)
            return
        # Pad convergence. The SOF's sub-byte offset is how far the payload
        # sits past a byte boundary; more pad bits push it *further*, so we
        # pull it back by *subtracting* that offset from the running pad
        # (the offset already folds in the pad in effect). Only measure
        # once a previously-sent set-pad has taken effect — `_pad_settle`
        # holds us off for the one batch its commit needs — so corrections
        # never stack and the pad can't march.
        sub = off & 0x7
        if self._pad_settle > 0:
            self._pad_settle -= 1
        elif sub:
            self._tdo_pad = (self._tdo_pad - sub) & 0x7
            self._pad_dirty = True
        payload = self._extract_bytes(tdo, off)
        self.logger.protocol("> %s", payload.hex())
        last_credit = self._parse_frames(payload)
        self._update_credit(last_credit, sent)

    def _find_sof(self, tdo: BitString) -> int | None:
        """Bit-search for the preamble→SOF lock, as the deserializer
        does: the first offset whose 16-bit window equals
        ``[preamble][SOF]``. Returns the bit offset of the first payload
        bit, or None. The lock sits at the head of the stream, so only
        the first :attr:`SYNC_SEARCH_BITS` offsets are scanned."""
        limit = min(self.SYNC_SEARCH_BITS, len(tdo) - 16)
        for k in range(0, limit + 1):
            if tdo[k:k + 16] == self._sync:
                return k + 16
        return None

    @staticmethod
    def _extract_bytes(tdo: BitString, off: int) -> bytes:
        """Group the post-SOF bits into whole bytes (LSB-first). The
        common case is byte-aligned (the pad converges there after the
        first batch) and slices straight out; the sub-byte case pulls
        each byte with an 8-bit slice. No whole-stream int round-trip."""
        count = (len(tdo) - off) // 8
        if (off & 0x7) == 0:
            return bytes(tdo[off:off + count * 8])
        out = bytearray(count)
        pos = off
        for i in range(count):
            out[i] = int(tdo[pos:pos + 8])
            pos += 8
        return bytes(out)

    def _parse_frames(self, payload: bytes) -> int | None:
        """Decode the post-SOF byte stream. Returns the value of the
        last RX-credit frame seen (free space in the TAP RX FIFO), or
        None. A truncated trailing frame (batch tail) is dropped."""
        i = 0
        last_credit = None
        n = len(payload)
        while i < n:
            header = payload[i]
            i += 1
            if (header & self.DATA_CTRL_BIT) == 0:
                count = (header & self.DATA_LEN_MASK) + 1
                last = bool(header & self.DATA_LAST_BIT)
                if i + count > n:
                    break
                #self.logger.protocol(f"RX data {last=}, {payload[i:i + count].hex()}")
                self._rx_partial += payload[i:i + count]
                i += count
                if last:
                    self._rx_frames.append(bytes(self._rx_partial))
                    self._rx_partial = bytearray()
            elif header == self.CREDIT:
                if i + 2 > n:
                    break
                last_credit = payload[i] | (payload[i + 1] << 8)
                #self.logger.protocol(f"RX credit {last_credit}")
                i += 2
            elif header == self.TX_LEVEL:
                if i + 2 > n:
                    break
                self._tx_level = payload[i] | (payload[i + 1] << 8)
                #self.logger.protocol(f"RX credit {self._tx_level}")
                i += 2
            elif header == self.IDLE:
                # idle, set-pad echoed on TDO, or reserved: ignore.
                #self.logger.protocol(f"RX Idle")
                pass
            else:
                # idle, set-pad echoed on TDO, or reserved: ignore.
                #self.logger.protocol(f"RX Other {header:#04x}")
                pass
        return last_credit

    def _update_credit(self, last_credit: int | None, sent: int):
        # Credit is absolute as of the position the TAP emitted it;
        # this batch's `sent` bytes are still in flight and unreflected,
        # so debit them. Earlier batches are already accounted (a full
        # batch is a round trip). Conservative: never over-grants.
        base = last_credit if last_credit is not None else self._rx_credit
        self._rx_credit = max(0, base - sent)

    def __repr__(self):
        return (f"<ContinuousTransport {self._name} "
                f"rx_credit={self._rx_credit} "
                f"tx_queued={len(self._tx_queue)} "
                f"rx_buffered={len(self._rx_frames)}>")
