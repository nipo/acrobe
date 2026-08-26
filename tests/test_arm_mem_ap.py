"""Tests for the ARM MEM-AP."""

from dataclasses import FrozenInstanceError

import pytest

from acrobe.component.arm.ap import Ap
from acrobe.component.arm.dp import (
    ApRead, ApWrite, Dp, DpRead, DpWrite,
)
from acrobe.component.arm.mem_ap import MemAp
from acrobe.protocol.memory import (
    Read32, UnsupportedAccess, Write8, Write32,
)


# -- Frozen op invariants ------------------------------------------

class TestOpsAreFrozen:
    def test_read32_immutable(self):
        op = Read32(addr=0x1000)
        with pytest.raises(FrozenInstanceError):
            op.addr = 0
        assert op.addr == 0x1000

    def test_write8_immutable(self):
        op = Write8(addr=0x1000, data=0x55)
        with pytest.raises(FrozenInstanceError):
            op.data = 0

    def test_value_equality(self):
        assert Read32(addr=0x1000) == Read32(addr=0x1000)
        assert Write32(addr=0x1000, data=1) != Write32(addr=0x1000, data=2)

    def test_no_result_field(self):
        # All results travel via futures; ops carry inputs only.
        assert not hasattr(Read32(0), "data")


# -- Recording DP for MemAp lowering tests -------------------------

class RecordingDp(Dp):
    """Dp that records every AP-level (CSW/TAR/DRW) op posted to it
    via ApRead / ApWrite. Resolves ApReads from a programmable
    register file mirroring the AP's storage.

    Models a single AP at the given base; CSW writes update an
    internal CSW value, TAR_LO writes update tar, DRW reads/writes
    target the address held in TAR (with auto-increment if CSW says
    so). Idle/abort ops are no-ops."""

    def __init__(self, ap_base=0):
        super().__init__("recdp")
        self.ap_base = ap_base
        self.posted: list = []        # list of ApRead/ApWrite ops
        self.memory: dict[int, int] = {}  # full-address -> 32-bit word
        self._csw = 0
        self._tar = 0

    def install_word(self, addr: int, value: int):
        # Word-aligned address for memory.
        self.memory[addr & ~3] = value

    def get_word(self, addr: int) -> int:
        return self.memory.get(addr & ~3, 0)

    async def flush_ops(self, batch):
        from acrobe.component.arm.dp import Abort, Run
        for op, future in batch:
            self.posted.append(op)
            if isinstance(op, ApWrite):
                # Absolute address → AP register offset within ap_base.
                if (op.addr & ~0xFFF) != self.ap_base:
                    future.set_result(None)
                    continue
                reg = op.addr - self.ap_base
                if reg == MemAp.CSW:
                    self._csw = op.data
                elif reg == MemAp.TAR_LO:
                    self._tar = op.data
                elif reg == MemAp.DRW:
                    # Combine partial-byte writes with what's already
                    # in memory at TAR.
                    size_bits = self._size_in_bits()
                    shift = (self._tar & 3) * 8
                    mask = ((1 << size_bits) - 1) << shift
                    cur = self.get_word(self._tar)
                    new = (cur & ~mask) | (op.data & mask)
                    self.memory[self._tar & ~3] = new & 0xffffffff
                    self._auto_increment()
                future.set_result(None)
            elif isinstance(op, ApRead):
                if (op.addr & ~0xFFF) != self.ap_base:
                    future.set_result(0)
                    continue
                reg = op.addr - self.ap_base
                if reg == MemAp.CSW:
                    future.set_result(self._csw)
                elif reg == MemAp.TAR_LO:
                    future.set_result(self._tar)
                elif reg == MemAp.DRW:
                    val = self.get_word(self._tar)
                    future.set_result(val)
                    self._auto_increment()
                else:
                    future.set_result(0)
            elif isinstance(op, (DpRead,)):
                future.set_result(0)
            elif isinstance(op, (DpWrite, Abort, Run)):
                future.set_result(None)

    def _size_in_bits(self) -> int:
        size_field = self._csw & 0x7
        return {0: 8, 1: 16, 2: 32}.get(size_field, 32)

    def _auto_increment(self):
        addrinc = (self._csw >> 4) & 0x3
        if addrinc != 0b01:
            return
        size_bits = self._size_in_bits()
        bytes_inc = size_bits // 8
        next_tar = self._tar + bytes_inc
        # Wrap at 1KB (10-bit auto-inc guarantee).
        if (next_tar & 0xFFFFFC00) != (self._tar & 0xFFFFFC00):
            next_tar = (self._tar & 0xFFFFFC00) | (next_tar & 0x3FF)
        self._tar = next_tar


def _make_memap(dp: RecordingDp, base: int = 0,
                idr: int = 0x04770001) -> MemAp:
    """Construct a MemAp, manually attached without running start()."""
    ap = MemAp(dp=dp, base=base, idr=idr)
    return ap


# -- Single-word read/write through the lowering --------------------

class TestSingleWord:
    @pytest.mark.asyncio
    async def test_read32_emits_csw_tar_drw(self):
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0xdeadbeef)
        ap = _make_memap(dp)

        v = await ap.read32(0x1000)

        assert v == 0xdeadbeef
        # Expect: CSW write, TAR write, DRW read.
        kinds = [(type(op).__name__, op.addr) for op in dp.posted]
        assert kinds == [
            ("ApWrite", MemAp.CSW),
            ("ApWrite", MemAp.TAR_LO),
            ("ApRead", MemAp.DRW),
        ]

    @pytest.mark.asyncio
    async def test_write32_emits_csw_tar_drw(self):
        dp = RecordingDp(ap_base=0)
        ap = _make_memap(dp)

        await ap.write32(0x1000, 0xc0ffeeee)

        assert dp.get_word(0x1000) == 0xc0ffeeee
        kinds = [(type(op).__name__, op.addr) for op in dp.posted]
        assert kinds == [
            ("ApWrite", MemAp.CSW),
            ("ApWrite", MemAp.TAR_LO),
            ("ApWrite", MemAp.DRW),
        ]

    @pytest.mark.asyncio
    async def test_read8_extracts_correct_byte_lane(self):
        dp = RecordingDp(ap_base=0)
        # Word at 0x1000 = 0xddccbbaa (little-endian: a0=aa, a1=bb, a2=cc, a3=dd)
        dp.install_word(0x1000, 0xddccbbaa)
        ap = _make_memap(dp)

        # Each test resets the cache and reads at a different byte
        # offset to verify lane extraction.
        for offset, expected in [(0, 0xaa), (1, 0xbb), (2, 0xcc), (3, 0xdd)]:
            sub_dp = RecordingDp(ap_base=0)
            sub_dp.memory = dict(dp.memory)  # share installed word
            sub_ap = _make_memap(sub_dp)
            v = await sub_ap.read8(0x1000 + offset)
            assert v == expected, (
                f"offset {offset}: got 0x{v:02x}, want 0x{expected:02x}")

    @pytest.mark.asyncio
    async def test_write8_writes_to_correct_byte_lane(self):
        dp = RecordingDp(ap_base=0)
        # Pre-populate the word so we can see byte-level updates.
        dp.install_word(0x1000, 0x00000000)
        ap = _make_memap(dp)

        await ap.write8(0x1001, 0x42)

        # Byte 1 should be 0x42, others zero.
        assert dp.get_word(0x1000) == 0x00004200

    @pytest.mark.asyncio
    async def test_read16_extracts_correct_halfword(self):
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0xddccbbaa)
        ap = _make_memap(dp)
        v = await ap.read16(0x1000)
        assert v == 0xbbaa

        dp2 = RecordingDp(ap_base=0)
        dp2.install_word(0x1000, 0xddccbbaa)
        ap2 = _make_memap(dp2)
        v = await ap2.read16(0x1002)
        assert v == 0xddcc


# -- CSW / TAR caching ---------------------------------------------

class TestCsw_Tar_Caching:
    @pytest.mark.asyncio
    async def test_consecutive_same_size_reads_share_csw_and_tar(self):
        # Sequential 32-bit reads of nearby addresses: only one CSW
        # write, one TAR write, then DRW reads (TAR auto-increments
        # between them).
        dp = RecordingDp(ap_base=0)
        for i in range(4):
            dp.install_word(0x1000 + i * 4, 0x10000000 + i)
        ap = _make_memap(dp)

        words = []
        for i in range(4):
            words.append(ap.read32(0x1000 + i * 4))
        results = [await f for f in words]

        assert results == [0x10000000, 0x10000001, 0x10000002, 0x10000003]
        kinds = [(type(op).__name__, op.addr) for op in dp.posted]
        # 1× CSW write, 1× TAR write, 4× DRW reads.
        assert kinds.count(("ApWrite", MemAp.CSW)) == 1
        assert kinds.count(("ApWrite", MemAp.TAR_LO)) == 1
        assert kinds.count(("ApRead", MemAp.DRW)) == 4

    @pytest.mark.asyncio
    async def test_size_change_emits_new_csw(self):
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0xddccbbaa)
        ap = _make_memap(dp)

        await ap.read32(0x1000)
        n_csw_after_first = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.CSW)

        await ap.read8(0x1000)
        n_csw_total = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.CSW)
        assert n_csw_total == n_csw_after_first + 1

    @pytest.mark.asyncio
    async def test_address_jump_emits_new_tar(self):
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0xa)
        dp.install_word(0x2000, 0xb)
        ap = _make_memap(dp)

        await ap.read32(0x1000)
        n_tar_after_first = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.TAR_LO)

        await ap.read32(0x2000)
        n_tar_total = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.TAR_LO)
        assert n_tar_total == n_tar_after_first + 1

    @pytest.mark.asyncio
    async def test_1kb_wrap_invalidates_tar_cache(self):
        # Read32 at 0x13FC then 0x1400 — auto-inc would cross the 1KB
        # boundary [0x1000, 0x13FF] into [0x1400, 0x17FF], so the
        # cache must be invalidated and TAR rewritten.
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x13FC, 0xaaaaaaaa)
        dp.install_word(0x1400, 0xbbbbbbbb)
        ap = _make_memap(dp)

        v1 = await ap.read32(0x13FC)
        v2 = await ap.read32(0x1400)

        assert v1 == 0xaaaaaaaa
        assert v2 == 0xbbbbbbbb
        n_tar = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.TAR_LO)
        # First TAR write for 0x13FC, second for 0x1400 (wrap forced).
        assert n_tar == 2

    @pytest.mark.asyncio
    async def test_within_1kb_segment_no_extra_tar_writes(self):
        # Within a single batch, sequential reads inside a 1 KiB
        # segment ride hardware auto-inc — TAR is written only at
        # the start. (Cross-batch caching is intentionally disabled
        # in MemAp.flush_ops, so this only holds intra-batch.)
        import asyncio
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x10FC, 0xaaaaaaaa)
        dp.install_word(0x1100, 0xbbbbbbbb)
        ap = _make_memap(dp)

        f1 = ap.read32(0x10FC)
        f2 = ap.read32(0x1100)
        v1, v2 = await asyncio.gather(f1, f2)

        assert v1 == 0xaaaaaaaa
        assert v2 == 0xbbbbbbbb
        n_tar = sum(
            1 for op in dp.posted
            if isinstance(op, ApWrite) and op.addr == MemAp.TAR_LO)
        assert n_tar == 1


# -- mem_read / mem_write convenience ------------------------------

class TestMemReadWrite:
    @pytest.mark.asyncio
    async def test_word_aligned_read(self):
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0x44332211)
        dp.install_word(0x1004, 0x88776655)
        ap = _make_memap(dp)

        data = await ap.mem_read(0x1000, 8)
        assert data == bytes.fromhex("11223344 55667788".replace(" ", ""))

    @pytest.mark.asyncio
    async def test_unaligned_read(self):
        dp = RecordingDp(ap_base=0)
        # Word @ 0x1000 = 0xDDCCBBAA → bytes a0=aa b0=bb c0=cc d0=dd
        # Word @ 0x1004 = 0xDDCCBBAA again
        dp.install_word(0x1000, 0xddccbbaa)
        dp.install_word(0x1004, 0x44332211)
        ap = _make_memap(dp)

        # Read 5 bytes starting at 0x1001 (unaligned head, aligned middle)
        data = await ap.mem_read(0x1001, 5)
        assert data == bytes([0xbb, 0xcc, 0xdd, 0x11, 0x22])

    @pytest.mark.asyncio
    async def test_zero_length_read_returns_empty(self):
        dp = RecordingDp(ap_base=0)
        ap = _make_memap(dp)
        assert await ap.mem_read(0x1000, 0) == b""

    @pytest.mark.asyncio
    async def test_word_aligned_write(self):
        dp = RecordingDp(ap_base=0)
        ap = _make_memap(dp)

        await ap.mem_write(0x1000, b"\x11\x22\x33\x44\x55\x66\x77\x88")

        assert dp.get_word(0x1000) == 0x44332211
        assert dp.get_word(0x1004) == 0x88776655

    @pytest.mark.asyncio
    async def test_unaligned_read_uses_word_accesses_only(self):
        # The overwide strategy covers the range with aligned words,
        # so one CSW establishes word size for the whole blob and TAR
        # rides auto-increment from the first word onwards.
        dp = RecordingDp(ap_base=0)
        dp.install_word(0x1000, 0xddccbbaa)
        dp.install_word(0x1004, 0x44332211)
        ap = _make_memap(dp)

        await ap.mem_read(0x1001, 5)

        kinds = [(type(op).__name__, op.addr) for op in dp.posted]
        assert kinds.count(("ApWrite", MemAp.CSW)) == 1
        assert kinds.count(("ApWrite", MemAp.TAR_LO)) == 1
        assert kinds.count(("ApRead", MemAp.DRW)) == 2

    @pytest.mark.asyncio
    async def test_write_stays_byte_granular(self):
        dp = RecordingDp(ap_base=0)
        ap = _make_memap(dp)

        await ap.mem_write(0x1001, b"\xaa\xbb\xcc")

        sizes = [op.data & 0x7 for op in dp.posted
                 if isinstance(op, ApWrite) and op.addr == MemAp.CSW]
        assert MemAp.CSW_SIZE_BYTE in sizes
        assert MemAp.CSW_SIZE_WORD not in sizes

    @pytest.mark.asyncio
    async def test_unaligned_write_preserves_neighbors(self):
        dp = RecordingDp(ap_base=0)
        # Pre-populate so we can verify only the targeted bytes change.
        dp.install_word(0x1000, 0xffffffff)
        dp.install_word(0x1004, 0xffffffff)
        ap = _make_memap(dp)

        # Write 3 bytes at 0x1001 — affects byte lanes 1,2,3 of word@0x1000.
        await ap.mem_write(0x1001, b"\xaa\xbb\xcc")

        assert dp.get_word(0x1000) == 0xccbbaaff
        assert dp.get_word(0x1004) == 0xffffffff


# -- Op families offered -------------------------------------------

class TestOpFamilies:
    @pytest.mark.asyncio
    async def test_memap_offers_both_families(self):
        dp = RecordingDp(ap_base=0)
        ap = _make_memap(dp)
        assert await ap.mem_read(0x1000, 4) == b"\x00\x00\x00\x00"
        assert await ap.read8(0x1000) == 0

    def test_dp_system_bus_declines_bulk_family(self):
        from acrobe.component.arm.dp import DpSystemBus

        bus = DpSystemBus(RecordingDp(ap_base=0))
        with pytest.raises(UnsupportedAccess):
            bus.mem_read(0x1000, 4)
        with pytest.raises(UnsupportedAccess):
            bus.mem_write(0x1000, b"\x00")

    def test_dp_system_bus_declines_narrow_registers(self):
        from acrobe.component.arm.dp import DpSystemBus

        bus = DpSystemBus(RecordingDp(ap_base=0))
        with pytest.raises(UnsupportedAccess):
            bus.read8(0x1000)
        with pytest.raises(UnsupportedAccess):
            bus.write16(0x1000, 0)

    @pytest.mark.asyncio
    async def test_dp_system_bus_serves_word_registers(self):
        from acrobe.component.arm.dp import DpSystemBus

        dp = RecordingDp(ap_base=0)
        bus = DpSystemBus(dp)
        await bus.read32(0x1000)
        assert [type(op).__name__ for op in dp.posted] == ["ApRead"]

    def test_dp_system_bus_rejects_unaligned(self):
        from acrobe.component.arm.dp import DpSystemBus

        bus = DpSystemBus(RecordingDp(ap_base=0))
        with pytest.raises(ValueError):
            bus.read32(0x1001)
        with pytest.raises(ValueError):
            bus.write32(0x1002, 0)


# -- Registration via Ap.db / discovery -----------------------------

class TestRegistration:
    @pytest.mark.asyncio
    async def test_ahb_ap_idr_resolves_to_memap(self):
        from acrobe.component.arm.ap import Ap

        dp = RecordingDp(ap_base=0)
        dp._ap_regs_for_lookup = {}  # n/a (RecordingDp doesn't model IDR via memory)
        # Use FakeDp pattern via directly constructing — easier here.
        # Actually we want to exercise Ap.discover via the regular flow.
        # Reach into the lookup directly:
        idr = 0x04770001
        cls = Ap.db.call(idr, dp=dp, base=0, idr=idr)
        assert isinstance(cls, MemAp)

    def test_apb_ap_resolves(self):
        dp = RecordingDp(ap_base=0)
        ap = Ap.db.call(0x04770002, dp=dp, base=0, idr=0x04770002)
        assert isinstance(ap, MemAp)
        assert ap.type == 0x2

    def test_axi_ap_resolves(self):
        dp = RecordingDp(ap_base=0)
        ap = Ap.db.call(0x04770004, dp=dp, base=0, idr=0x04770004)
        assert isinstance(ap, MemAp)
        assert ap.type == 0x4

    def test_revision_and_variant_masked(self):
        # IDR 0x14770051: revision=1, variant=5, designer/class/type
        # match AHB-AP's 0x04770001 registration.
        dp = RecordingDp(ap_base=0)
        ap = Ap.db.call(0x14770051, dp=dp, base=0, idr=0x14770051)
        assert isinstance(ap, MemAp)
        assert ap.type == 0x1
        assert ap.revision == 0x1
        assert ap.variant == 0x5

    def test_friendly_name_uses_type(self):
        dp = RecordingDp(ap_base=0)
        ahb = MemAp(dp=dp, base=0, idr=0x04770001)
        assert ahb.name == "AHB-AP@0"
        apb = MemAp(dp=dp, base=1 << 24, idr=0x04770002)
        assert apb.name == "APB-AP@1"
        axi = MemAp(dp=dp, base=2 << 24, idr=0x04770004)
        assert axi.name == "AXI-AP@2"


# -- start() reads CFG and BASE -------------------------------------

class TestStart:
    @pytest.mark.asyncio
    async def test_start_reads_cfg_and_base(self):
        dp = RecordingDp(ap_base=0)
        # Install CFG and BASE_LO at the AP register addresses.
        # MemAp.start reads them via Ap.reg_read, which posts ApRead.
        # Our RecordingDp answers ApRead from per-register handling
        # — only CSW / TAR / DRW get special handling; others return 0.
        # So we need to extend or use a different stub. Inline a
        # minimal ApRegResponder.
        responses: dict[int, int] = {
            MemAp.CFG:    0x0,            # no LA, no LD
            MemAp.BASE_LO: 0xe000_0001,   # P=1, format=ADIv5 (bit1=0),
                                          # base address = 0xe0000000
        }
        # Patch the Dp's flush_ops to fall back to responses{} for
        # other AP-register reads.
        orig_flush = dp.flush_ops

        async def patched_flush(batch):
            from acrobe.component.arm.dp import (
                Abort, ApRead as ApR, Run,
            )
            for op, future in list(batch):
                if isinstance(op, ApR) and op.addr in responses:
                    future.set_result(responses[op.addr])
                else:
                    await orig_flush([(op, future)])
            return
        dp.flush_ops = patched_flush

        ap = _make_memap(dp, idr=0x04770001)
        await ap.start()

        assert ap.cfg == 0x0
        assert ap.base_addr == 0xe0000000

    @pytest.mark.asyncio
    async def test_start_with_valid_base_walks_rom_table(self):
        # MemAp.start at an AP whose BASE points to a valid ROM Table
        # should attach the ROM Table as a child after discovery.
        from acrobe.component.arm.coresight.model import (
            MemoryMappedComponent, PartId,
        )
        from acrobe.component.arm.coresight.rom_table import RomTable

        dp = RecordingDp(ap_base=0)
        # Place a Class 0x1 ROM Table at 0xE000_0000 in the AP's
        # memory space. The CIDR preamble + class encoding plus the
        # PIDR4 (size=0, jep106 cont=4) identify it as a ROM Table.
        rom_base = 0xE000_0000
        # PIDR0..3 — synthesize an ARM-designed component, part 0x100.
        dp.install_word(rom_base + 0xFE0, 0x00)        # PIDR0 (part lo)
        dp.install_word(rom_base + 0xFE4, 0xB1)        # PIDR1 (jep_id_lo<<4 | part_hi)
        dp.install_word(rom_base + 0xFE8, 0x03)        # PIDR2 (rev<<4 | jedec | jep_id_hi)
        dp.install_word(rom_base + 0xFEC, 0x00)        # PIDR3
        dp.install_word(rom_base + 0xFD0, 0x04)        # PIDR4 (size_log2<<4 | jep_cont=4)
        # CIDR preamble + class 0x1
        dp.install_word(rom_base + 0xFF0, 0x0D)
        dp.install_word(rom_base + 0xFF4, 0x10)        # class=1
        dp.install_word(rom_base + 0xFF8, 0x05)
        dp.install_word(rom_base + 0xFFC, 0xB1)
        # ROM entry 0 = 0 → terminator (empty ROM).

        # AP register file: CFG=0, BASE_LO=0xE000_0001 (P=1, addr=0xE000_0000).
        responses = {
            MemAp.CFG:    0x0,
            MemAp.BASE_LO: 0xE000_0001,
        }
        orig_flush = dp.flush_ops

        async def patched_flush(batch):
            from acrobe.component.arm.dp import (
                Abort, ApRead as ApR, Run,
            )
            for op, future in list(batch):
                if isinstance(op, ApR) and op.addr in responses:
                    future.set_result(responses[op.addr])
                else:
                    await orig_flush([(op, future)])
        dp.flush_ops = patched_flush

        ap = _make_memap(dp, idr=0x04770001)
        await ap.start()

        # MemAp should have a single child: the discovered RomTable.
        rom_children = [c for c in ap.children if isinstance(c, RomTable)]
        assert len(rom_children) == 1
        assert rom_children[0].base == rom_base

    @pytest.mark.asyncio
    async def test_start_no_debug_components(self):
        # New-format BASE with P=0: 0x2 (FORMAT=1, P=0).
        dp = RecordingDp(ap_base=0)
        responses = {MemAp.CFG: 0x0, MemAp.BASE_LO: 0x2}
        orig_flush = dp.flush_ops

        async def patched_flush(batch):
            from acrobe.component.arm.dp import (
                Abort, ApRead as ApR, Run,
            )
            for op, future in list(batch):
                if isinstance(op, ApR) and op.addr in responses:
                    future.set_result(responses[op.addr])
                else:
                    await orig_flush([(op, future)])
        dp.flush_ops = patched_flush

        ap = _make_memap(dp, idr=0x04770001)
        await ap.start()
        assert ap.base_addr is None

    @pytest.mark.asyncio
    async def test_start_legacy_format_base(self):
        # ADIv5.0 legacy BASE format: address in bits[31:12], bit 1=0,
        # bit 0 RAZ. BASE != 0xFFFFFFFF means "present". Zynq-7's
        # APB-AP returns 0x80000000 in this format.
        dp = RecordingDp(ap_base=0)
        responses = {MemAp.CFG: 0x0, MemAp.BASE_LO: 0x80000000}
        orig_flush = dp.flush_ops

        async def patched_flush(batch):
            from acrobe.component.arm.dp import ApRead as ApR
            for op, future in list(batch):
                if isinstance(op, ApR) and op.addr in responses:
                    future.set_result(responses[op.addr])
                else:
                    await orig_flush([(op, future)])
        dp.flush_ops = patched_flush

        ap = _make_memap(dp, idr=0x04770002)
        await ap.start()
        assert ap.base_addr == 0x80000000

    @pytest.mark.asyncio
    async def test_start_legacy_format_no_entry(self):
        # Legacy "no entry" sentinel: 0xFFFFFFFF.
        dp = RecordingDp(ap_base=0)
        responses = {MemAp.CFG: 0x0, MemAp.BASE_LO: 0xFFFFFFFF}
        orig_flush = dp.flush_ops

        async def patched_flush(batch):
            from acrobe.component.arm.dp import ApRead as ApR
            for op, future in list(batch):
                if isinstance(op, ApR) and op.addr in responses:
                    future.set_result(responses[op.addr])
                else:
                    await orig_flush([(op, future)])
        dp.flush_ops = patched_flush

        ap = _make_memap(dp, idr=0x04770002)
        await ap.start()
        assert ap.base_addr is None
