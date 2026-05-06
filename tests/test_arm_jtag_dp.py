"""Tests for ARM JTAG-DP: JtagDp lowering, pending-read pipelining,
end-of-batch RDBUFF flush, SELECT caching."""

import asyncio

import pytest

from acrobe.bitstring import BitString
from acrobe.component.arm.dp import (
    Abort, ApRead, ApWrite, Dp, DpAccessFailure, DpRead, DpWrite, Run,
)
from acrobe.component.arm.jtag_dp import (
    JtagDp, JtagDpTap, JtagDpV3Tap, _Wire,
)
from acrobe.protocol.jtag import (
    Chain, JtagInterface, Reset, Run as JtagRun, Shift, CaptureDr,
    CaptureIr, Tap, _TapRun, _TapShift,
)


# -- Recording fake tap: short-circuits flush_ops to capture what
#    JtagDp posts at the Tap level, with a queue of canned TDO values.

class RecordingDpTap(JtagDpTap):
    """JtagDpTap subclass whose flush_ops records ``_TapShift`` /
    ``_TapRun`` envelopes from a child JtagDp and resolves their
    futures with canned TDO BitStrings — instead of forwarding to a
    Chain.

    Use ``queue_tdo(value)`` to schedule the next read_tdo shift's
    35-bit response. Reads with no queued value return zero."""

    def __init__(self):
        super().__init__(idcode=0x0BA00477, irlen=4, name="rec")
        self.shifts: list = []
        self.runs: list[int] = []
        self.tdo_queue: list[int] = []

    def queue_tdo(self, value: int):
        self.tdo_queue.append(value)

    def queue_response(self, ack: int, data: int):
        self.tdo_queue.append((data & 0xffffffff) << 3 | (ack & 0x7))

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, _TapShift):
                self.shifts.append({
                    "ir": op.ir_value,
                    "tdi": int(op.tdi) if op.tdi is not None else None,
                    "len": len(op.tdi) if op.tdi is not None else 0,
                    "read_tdo": op.read_tdo,
                })
                if op.read_tdo:
                    bits = len(op.tdi) if op.tdi is not None else 35
                    val = self.tdo_queue.pop(0) if self.tdo_queue else 0
                    future.set_result(BitString(val, bits))
                else:
                    future.set_result(None)
            elif isinstance(op, _TapRun):
                self.runs.append(op.cycles)
                future.set_result(None)
            else:
                future.set_result(None)


def _make_dp(jtag_protocol_version: int = 0) -> tuple[RecordingDpTap, JtagDp]:
    """Build a recording tap with an attached JtagDp child, both
    detached from any chain. The DP is not started — tests post ops
    directly to exercise flush_ops in isolation."""
    tap = RecordingDpTap()
    dp = JtagDp(jtag_protocol_version=jtag_protocol_version)
    tap._child_attach(dp)
    return tap, dp


# -- Wire packing ---------------------------------------------------

class TestWirePacking:
    def test_pack_read(self):
        # RnW=1, addr=0x04 (CTRL_STAT, wire bits=0b01), data ignored.
        # Expected low 3 bits: addr_bits<<1 | rnw = 0b01<<1 | 1 = 0b011.
        v = _Wire.pack(rnw=True, addr=0x04, data=0)
        assert v & 0x7 == 0b011

    def test_pack_write(self):
        # RnW=0, addr=0x08 (SELECT, wire bits=0b10), data=0x12345678.
        v = _Wire.pack(rnw=False, addr=0x08, data=0x12345678)
        assert v & 0x7 == 0b100
        assert (v >> 3) & 0xffffffff == 0x12345678

    def test_unpack_ok(self):
        ack, data = _Wire.unpack(BitString((0xdeadbeef << 3) | 0b010, 35))
        assert ack == _Wire.ACK_OK_FAULT
        assert data == 0xdeadbeef

    def test_unpack_wait(self):
        ack, _ = _Wire.unpack(BitString(0b001, 35))
        assert ack == _Wire.ACK_WAIT


# -- Single DP read end-to-end --------------------------------------

class TestDpRead:
    @pytest.mark.asyncio
    async def test_single_read_resolves_via_rdbuff_flush(self):
        # On a fresh DP, reading CTRL_STAT produces:
        #   1. SELECT write (DPBANKSEL=0, non-capturing)
        #   2. DPACC read of CTRL_STAT (non-capturing — no pending yet)
        #   3. DPACC read of RDBUFF (capturing — TDO carries CTRL_STAT
        #      response, resolves the user future)
        tap, dp = _make_dp()
        tap.queue_response(ack=_Wire.ACK_OK_FAULT, data=0xc0ffee01)

        result = await dp.post(DpRead(Dp.CTRL_STAT))

        assert result == 0xc0ffee01
        assert len(tap.shifts) == 3
        assert tap.shifts[0]["ir"] == JtagDpTap.DPACC.ir
        assert tap.shifts[0]["read_tdo"] is False
        assert tap.shifts[1]["ir"] == JtagDpTap.DPACC.ir
        assert tap.shifts[1]["read_tdo"] is False
        assert tap.shifts[2]["ir"] == JtagDpTap.DPACC.ir
        assert tap.shifts[2]["read_tdo"] is True


# -- AP read pipelining --------------------------------------------

class TestApReadPipelining:
    @pytest.mark.asyncio
    async def test_consecutive_ap_reads_pipeline(self):
        # Two ApReads at the same AP/bank: SELECT write, two APACC
        # reads, single RDBUFF flush. The second APACC's TDO carries
        # the first read's response; RDBUFF carries the second.
        tap, dp = _make_dp()

        tap.queue_response(_Wire.ACK_OK_FAULT, 0xaaaa1111)   # APACC#2 → read#1
        tap.queue_response(_Wire.ACK_OK_FAULT, 0xbbbb2222)   # RDBUFF → read#2

        f1 = dp.post(ApRead(addr=0x00))
        f2 = dp.post(ApRead(addr=0x00))

        v1 = await f1
        v2 = await f2

        assert v1 == 0xaaaa1111
        assert v2 == 0xbbbb2222
        assert len(tap.shifts) == 4
        assert tap.shifts[0]["ir"] == JtagDpTap.DPACC.ir   # SELECT
        assert tap.shifts[0]["read_tdo"] is False
        assert tap.shifts[1]["ir"] == JtagDpTap.APACC.ir   # ApRead#1
        assert tap.shifts[1]["read_tdo"] is False
        assert tap.shifts[2]["ir"] == JtagDpTap.APACC.ir   # ApRead#2
        assert tap.shifts[2]["read_tdo"] is True
        assert tap.shifts[3]["ir"] == JtagDpTap.DPACC.ir   # RDBUFF
        assert tap.shifts[3]["read_tdo"] is True

    @pytest.mark.asyncio
    async def test_write_after_read_flushes_pending(self):
        # ApRead followed by ApWrite: the write captures the read's
        # response on its own TDO. The write itself is then pending —
        # an end-of-batch RDBUFF drains it. The write's user-future
        # value is meaningless (writes have no payload), only that the
        # future is resolved (any value, no exception).
        tap, dp = _make_dp()

        tap.queue_response(_Wire.ACK_OK_FAULT, 0x12345678)
        tap.queue_response(_Wire.ACK_OK_FAULT, 0)

        f1 = dp.post(ApRead(addr=0x00))
        f2 = dp.post(ApWrite(addr=0x04, data=0xcafebabe))

        assert await f1 == 0x12345678
        # Write resolves; value is irrelevant, just must not raise.
        await f2
        assert len(tap.shifts) == 4


# -- SELECT caching -------------------------------------------------

# A DPACC SELECT write has TDI low 3 bits = 0b100 (RnW=0, addr-bits=0b10
# from SELECT@0x08). Identifying via TDI is tighter than counting
# non-capturing DPACC shifts — the new lowering keeps reads/writes that
# are part of the pending pipeline non-capturing as well.
def _is_select_write(shift) -> bool:
    return (shift["ir"] == JtagDpTap.DPACC.ir
            and (shift["tdi"] & 0x7) == 0b100)


class TestSelectCaching:
    @pytest.mark.asyncio
    async def test_each_batch_rewrites_select_at_start(self):
        # Per-batch SELECT refresh: the JtagDpLowerer is instantiated
        # per batch (last_select=None), so the first op of every batch
        # always emits a SELECT write — we don't trust other DP/AP
        # users not to have moved SELECT between batches.
        tap, dp = _make_dp()

        for _ in range(2):
            tap.queue_response(_Wire.ACK_OK_FAULT, 0)
            await dp.post(DpRead(Dp.CTRL_STAT))

        # 3 shifts per batch (SELECT + CTRL_STAT-read + RDBUFF). Both
        # batches kick off with a non-capturing SELECT write.
        assert len(tap.shifts) == 6
        assert _is_select_write(tap.shifts[0])
        assert _is_select_write(tap.shifts[3])

    @pytest.mark.asyncio
    async def test_bank_change_within_batch_emits_select_write(self):
        # Within one batch, CTRL_STAT (bank 0) then DPIDR1 (bank 1)
        # forces a mid-batch SELECT rewrite for the new DPBANKSEL.
        tap, dp = _make_dp()

        for _ in range(2):
            tap.queue_response(_Wire.ACK_OK_FAULT, 0)

        f1 = dp.post(DpRead(Dp.CTRL_STAT))
        f2 = dp.post(DpRead(Dp.DPIDR1))
        await asyncio.gather(f1, f2)

        # Two SELECT writes — one for bank 0, one for bank 1.
        n_select_writes = sum(1 for s in tap.shifts if _is_select_write(s))
        assert n_select_writes == 2

    @pytest.mark.asyncio
    async def test_ap_change_within_batch_emits_select_write(self):
        # Two APACC reads at different AP bases in one batch. SELECT
        # changes between them.
        tap, dp = _make_dp()

        for _ in range(2):
            tap.queue_response(_Wire.ACK_OK_FAULT, 0)

        f1 = dp.post(ApRead(addr=0))
        f2 = dp.post(ApRead(addr=1 << 24))
        await asyncio.gather(f1, f2)

        apacc_shifts = sum(1 for s in tap.shifts
                           if s["ir"] == JtagDpTap.APACC.ir)
        assert apacc_shifts == 2
        n_select_writes = sum(1 for s in tap.shifts if _is_select_write(s))
        assert n_select_writes == 2


# -- Idle TCK insertion --------------------------------------------

class TestIdleTcks:
    @pytest.mark.asyncio
    async def test_idle_run_after_each_shift(self):
        # AP transactions are behind a resynchronizing gateway and need
        # idle TCKs between Update-DR and the next Capture-DR. The
        # lowerer emits a Run after every shift; the count must match
        # the number of shifts, no matter what type.
        tap, dp = _make_dp()

        tap.queue_response(_Wire.ACK_OK_FAULT, 0)

        await dp.post(DpRead(Dp.CTRL_STAT))

        # 3 shifts (SELECT, CTRL_STAT-read, RDBUFF) → 3 idle runs.
        assert len(tap.runs) == len(tap.shifts) == 3
        assert all(c > 0 for c in tap.runs)


# -- Abort -----------------------------------------------------------

class TestAbort:
    @pytest.mark.asyncio
    async def test_abort_uses_dedicated_ir(self):
        tap, dp = _make_dp()

        await dp.post(Abort(0x1f))

        # Single shift via the ABORT instruction.
        assert len(tap.shifts) == 1
        assert tap.shifts[0]["ir"] == JtagDpTap.ABORT_IR.ir
        # data 0x1f shifted left 3 into the 35-bit DR.
        assert tap.shifts[0]["tdi"] == (0x1f << 3)


# -- Errors ---------------------------------------------------------

class TestAckErrors:
    @pytest.mark.asyncio
    async def test_wait_ack_raises(self):
        # WAIT surfaces as a user-visible DpAccessFailure. (Wire-level
        # retry was removed — re-issuing a posted read may return
        # different data than the first attempt; retry has to live at
        # a higher layer with op-aware semantics.)
        tap, dp = _make_dp()
        tap.queue_response(_Wire.ACK_WAIT, 0)

        with pytest.raises(DpAccessFailure, match="wait"):
            await dp.post(DpRead(Dp.CTRL_STAT))

    @pytest.mark.asyncio
    async def test_invalid_ack_raises(self):
        # Any ACK that isn't WAIT or the version's OK encoding is
        # treated as FAULT.
        tap, dp = _make_dp()
        tap.queue_response(0b111, 0)

        with pytest.raises(DpAccessFailure, match="fault"):
            await dp.post(DpRead(Dp.CTRL_STAT))


# -- Protocol-v1 (ADIv6 / DPv3) ACK decoding -----------------------

class TestProtocolV1Ack:
    """Wire-level layout (request/response) is identical between v0
    and v1; only ACK semantics change. These tests exercise the v1
    decoding path on an isolated JtagDp constructed with
    ``jtag_protocol_version=1``."""

    @pytest.mark.asyncio
    async def test_ok_returns_data(self):
        tap, dp = _make_dp(jtag_protocol_version=1)

        # In v1, OK = 0b100. The RDBUFF flush carries the real
        # CTRL_STAT response.
        tap.queue_response(ack=_Wire.ACK_V1_OK, data=0xc0ffee01)

        result = await dp.post(DpRead(Dp.CTRL_STAT))
        assert result == 0xc0ffee01

    @pytest.mark.asyncio
    async def test_fault_raises(self):
        tap, dp = _make_dp(jtag_protocol_version=1)

        # In v1, 0b010 = FAULT (not OK). Returned data is UNKNOWN
        # and must surface as DpAccessFailure.
        tap.queue_response(ack=_Wire.ACK_V1_FAULT, data=0)

        with pytest.raises(DpAccessFailure, match="fault"):
            await dp.post(DpRead(Dp.CTRL_STAT))

    @pytest.mark.asyncio
    async def test_v0_ok_encoding_is_invalid_under_v1(self):
        # 0b010 means "OK" under v0 but "FAULT" under v1, so seeing
        # it on a v1 DP is a fault — never silently treated as OK.
        tap, dp = _make_dp(jtag_protocol_version=1)
        tap.queue_response(ack=_Wire.ACK_OK_FAULT, data=0xdeadbeef)

        with pytest.raises(DpAccessFailure, match="fault"):
            await dp.post(DpRead(Dp.CTRL_STAT))

    def test_invalid_protocol_version_rejected(self):
        with pytest.raises(ValueError):
            JtagDp(jtag_protocol_version=2)


# -- Integration via Chain + a full JTAG-DP simulator --------------

class _DpSim(JtagInterface):
    """JtagInterface that simulates a single-TAP JTAG chain whose
    only device is a JTAG-DP.

    Tracks IR, the current 35-bit DR, the most-recent transaction
    response, and the SELECT register. Models DPIDR, CTRL/STAT,
    SELECT, RDBUFF responses sufficient for ``Dp.start()``."""

    IR_BYPASS = 0xf
    IR_IDCODE = 0xe
    IR_DPACC  = 0xa
    IR_APACC  = 0xb
    IR_ABORT  = 0x8

    def __init__(self, idcode=0x0BA00477, dpidr=0x4BA02477):
        super().__init__(name="dpsim")
        self._idcode = idcode
        self._dpidr = dpidr
        self._ir = self.IR_IDCODE
        self._dr_val = idcode
        self._dr_len = 32
        self._in_ir = False
        self._select = 0
        self._last_response = (_Wire.ACK_OK_FAULT, 0)
        self._ctrl_stat = 0  # power-up bits set on CDBGPWRUPREQ/CSYSPWRUPREQ
        # AP register file: ap_regs[(base, register_offset)] -> value.
        self._ap_regs: dict[tuple[int, int], int] = {}

    def install_ap(self, base: int, registers: dict[int, int]):
        """Place an AP at ``base`` with ``registers`` (offset -> value)."""
        for off, val in registers.items():
            self._ap_regs[(base, off)] = val

    async def flush_ops(self, batch):
        for op, future in batch:
            tdo = None
            if isinstance(op, Reset):
                self._ir = self.IR_IDCODE
                self._dr_val = self._idcode
                self._dr_len = 32
                self._in_ir = False
            elif isinstance(op, CaptureIr):
                self._in_ir = True
                # JTAG: low two bits of captured IR are 0b01 by spec.
                self._dr_val = 0b01
                self._dr_len = 4
            elif isinstance(op, CaptureDr):
                self._in_ir = False
                self._dr_val = self._dr_value_for_ir()
                self._dr_len = self._dr_len_for_ir()
            elif isinstance(op, Shift):
                tdo = self._do_shift(op)
            elif isinstance(op, JtagRun):
                pass
            future.set_result(tdo)

    def _dr_len_for_ir(self):
        if self._ir == self.IR_IDCODE:
            return 32
        if self._ir == self.IR_BYPASS:
            return 1
        # DPACC / APACC / ABORT are 35-bit.
        return 35

    def _dr_value_for_ir(self):
        if self._ir == self.IR_IDCODE:
            return self._idcode
        if self._ir in (self.IR_DPACC, self.IR_APACC):
            ack, data = self._last_response
            return ((data & 0xffffffff) << 3) | (ack & 0x7)
        return 0

    def _do_shift(self, op):
        L = self._dr_len
        N = len(op.tdi)
        tdi_val = int(op.tdi)
        tdo = None
        if op.read_tdo:
            if N <= L:
                tdo = BitString(self._dr_val & ((1 << N) - 1), N)
            else:
                tdo_val = (self._dr_val | (tdi_val << L)) & ((1 << N) - 1)
                tdo = BitString(tdo_val, N)
        if L > 0 and N >= L:
            new_val = (tdi_val >> (N - L)) & ((1 << L) - 1)
            self._dr_val = new_val
            if self._in_ir:
                self._ir = new_val
            else:
                self._on_dr_update(new_val)
        return tdo

    def _on_dr_update(self, dr):
        if self._ir == self.IR_DPACC:
            rnw = dr & 1
            addr = (dr >> 1) & 0x3
            data = (dr >> 3) & 0xffffffff
            wire_offset = addr << 2
            if rnw:
                self._last_response = (_Wire.ACK_OK_FAULT,
                                       self._dp_read(wire_offset))
            else:
                self._dp_write(wire_offset, data)
                self._last_response = (_Wire.ACK_OK_FAULT, 0)
        elif self._ir == self.IR_APACC:
            rnw = dr & 1
            wire_addr = (dr >> 1) & 0x3
            data = (dr >> 3) & 0xffffffff
            # ADIv6 view: full system address = SELECT[31:4]<<0 | wire<<2.
            full_addr = (self._select & 0xFFFFFFF0) | (wire_addr << 2)
            # Locate the owning AP by base (lowest base <= full_addr that
            # has any installed register; APs occupy a 4 KB region).
            ap_base = self._ap_base_for(full_addr)
            reg_off = full_addr - ap_base if ap_base is not None else None
            if rnw:
                if ap_base is None:
                    self._last_response = (_Wire.ACK_OK_FAULT, 0)
                else:
                    val = self._ap_regs.get((ap_base, reg_off), 0)
                    self._last_response = (_Wire.ACK_OK_FAULT, val)
            else:
                if ap_base is not None:
                    self._ap_regs[(ap_base, reg_off)] = data
                self._last_response = (_Wire.ACK_OK_FAULT, 0)
        elif self._ir == self.IR_ABORT:
            pass

    def _dp_read(self, wire_offset: int) -> int:
        bank = self._select & 0xf
        if wire_offset == 0x00:
            return self._dpidr
        if wire_offset == 0x04 and bank == 0:
            return self._ctrl_stat
        if wire_offset == 0x0c:
            # RDBUFF returns the most-recent AP read result; for DP
            # accesses in slice 1 we just return the cached
            # last-response data.
            return self._last_response[1]
        return 0

    def _ap_base_for(self, full_addr: int) -> int | None:
        """Return the base of the AP that owns ``full_addr`` (any AP
        with at least one installed register within [base, base+0xFFC]).
        Returns None if no AP matches."""
        candidates = {base for (base, _) in self._ap_regs}
        for base in candidates:
            if base <= full_addr < base + 0x1000:
                return base
        return None

    def _dp_write(self, wire_offset: int, data: int):
        bank = self._select & 0xf
        if wire_offset == 0x08:
            self._select = data
        elif wire_offset == 0x04 and bank == 0:
            # Write to CTRL/STAT — set ACKs for any REQs immediately.
            self._ctrl_stat = data
            if data & Dp.CDBGPWRUPREQ:
                self._ctrl_stat |= Dp.CDBGPWRUPACK
            if data & Dp.CSYSPWRUPREQ:
                self._ctrl_stat |= Dp.CSYSPWRUPACK


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_dp_start_through_chain(self):
        sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
        chain = Chain()
        sim.child_add(chain)

        await chain.discover()

        assert len(chain.children) == 1
        tap = chain.children[0]
        assert isinstance(tap, JtagDpTap)
        assert tap.idcode == 0x4BA00477

        await tap.start_tree()

        # JtagDpTap.start adds a JtagDp child; that child's start
        # reads DPIDR and powers up the DP.
        dps = [c for c in tap.children if isinstance(c, JtagDp)]
        assert len(dps) == 1
        dp = dps[0]
        assert dp.dpidr == 0x4BA02477
        assert dp.dp_version == 2
        assert dp.adi_version == 5

    @pytest.mark.asyncio
    async def test_ap_enumeration_picks_up_installed_aps(self):
        # Mirror the Zynq-7's first two APs: an AHB-AP at apsel 0 and
        # an APB-AP at apsel 1. Other APSELs read IDR=0 and are
        # skipped. The DP enumeration walks 0..15 + 240..255 and adds
        # the two real APs as children.
        from acrobe.component.arm.ap import Ap
        from acrobe.component.arm.mem_ap import MemAp

        sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
        # AHB-AP also needs CFG and BASE reads to succeed during
        # MemAp.start; populate them with sensible defaults.
        sim.install_ap(base=0x00000000, registers={
            Ap.IDR: 0x04770001,
            MemAp.CFG: 0x0,         # no LA/LD extensions
            MemAp.BASE_LO: 0x0,     # P=0: no debug components
        })
        sim.install_ap(base=0x01000000, registers={
            Ap.IDR: 0x04770002,
            MemAp.CFG: 0x0,
            MemAp.BASE_LO: 0x0,
        })

        chain = Chain()
        sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        await tap.start_tree()

        dp = [c for c in tap.children if isinstance(c, JtagDp)][0]

        ap_children = [c for c in dp.children if isinstance(c, Ap)]
        assert len(ap_children) == 2
        # Both APs are MemAps (registered against AHB-AP / APB-AP IDRs).
        assert all(isinstance(ap, MemAp) for ap in ap_children)
        bases = sorted(ap.base for ap in ap_children)
        assert bases == [0x00000000, 0x01000000]
        idrs = sorted(ap.idr for ap in ap_children)
        assert idrs == [0x04770001, 0x04770002]

        # IDR.TYPE-derived friendly names.
        names = sorted(ap.name for ap in ap_children)
        assert names == ["AHB-AP@0", "APB-AP@1"]

    @pytest.mark.asyncio
    async def test_ap_enumeration_with_no_aps(self):
        sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
        chain = Chain()
        sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        await tap.start_tree()

        dp = [c for c in tap.children if isinstance(c, JtagDp)][0]
        from acrobe.component.arm.ap import Ap
        assert [c for c in dp.children if isinstance(c, Ap)] == []

    @pytest.mark.asyncio
    async def test_high_apsel_is_probed(self):
        # AP at APSEL 255 (high range probed by enumeration).
        from acrobe.component.arm.ap import Ap

        sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
        sim.install_ap(base=0xff << 24, registers={Ap.IDR: 0x47700004})
        chain = Chain()
        sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        await tap.start_tree()

        dp = [c for c in tap.children if isinstance(c, JtagDp)][0]
        ap_children = [c for c in dp.children if isinstance(c, Ap)]
        assert len(ap_children) == 1
        assert ap_children[0].base == 0xff000000
        assert ap_children[0].name == "ap255"

    @pytest.mark.asyncio
    async def test_failing_ap_start_does_not_drop_siblings(self):
        # One AP's start blows up; the sibling AP must still end up
        # as a child of the DP. The failing AP stays attached too,
        # just with an incomplete subtree.
        from acrobe.component.arm.ap import Ap

        # Custom AP subclass whose start raises. Pick an IDR.TYPE
        # that's not in MemAp's registrations (TYPE=0x9 is unused).
        broken_idr = 0x04770009

        class BrokenStartAp(Ap):
            def __init__(self, dp, base, idr, name=None):
                if name is None:
                    name = f"broken@{base >> 24}"
                super().__init__(dp, base, idr, name)

            async def start(self):
                raise RuntimeError("simulated AP start failure")

        Ap.db.register(broken_idr)(BrokenStartAp)
        try:
            sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
            sim.install_ap(base=0x00000000,
                           registers={Ap.IDR: broken_idr})
            sim.install_ap(base=0x01000000, registers={
                Ap.IDR: 0x04770002,
                # MemAp.start reads CFG and BASE_LO — supply zeros.
                0xF4: 0x0,
                0xF8: 0x0,
            })
            chain = Chain()
            sim.child_add(chain)
            await chain.discover()
            tap = chain.children[0]
            await tap.start_tree()

            dp = [c for c in tap.children if isinstance(c, JtagDp)][0]
            ap_children = [c for c in dp.children if isinstance(c, Ap)]
            # Both APs are present despite the broken one's failed start.
            assert len(ap_children) == 2
            bases = sorted(ap.base for ap in ap_children)
            assert bases == [0x00000000, 0x01000000]
        finally:
            Ap.db._registry.pop(broken_idr, None)

    @pytest.mark.asyncio
    async def test_ap_reg_read_addresses_correctly(self):
        # After enumeration, an AP can read its own registers.
        # AP at apsel 1 with IDR=0x04770002 and a fake register at
        # offset 0x10 = 0xcafebabe.
        from acrobe.component.arm.ap import Ap

        sim = _DpSim(idcode=0x4BA00477, dpidr=0x4BA02477)
        sim.install_ap(base=0x01000000, registers={
            Ap.IDR: 0x04770002,
            0x10: 0xcafebabe,
        })

        chain = Chain()
        sim.child_add(chain)
        await chain.discover()
        tap = chain.children[0]
        await tap.start_tree()

        dp = [c for c in tap.children if isinstance(c, JtagDp)][0]
        ap = [c for c in dp.children if isinstance(c, Ap)][0]

        v = await ap.reg_read(0x10)
        assert v == 0xcafebabe
