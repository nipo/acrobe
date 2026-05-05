"""Tests for the ARM AP base class: IDR decoding, registry,
discovery."""

import pytest

from acrobe.component.arm.ap import Ap, ApIdr
from acrobe.component.arm.dp import (
    ApRead, ApWrite, Dp, DpRead, DpWrite,
)
from acrobe.db import NoMatch


# -- ApIdr decoding --------------------------------------------------

class TestApIdrDecoding:
    def test_ahb_ap_decode(self):
        idr = ApIdr.from_idr(0x04770001)
        # DESIGNER 0x23B = ARM (jep4/0x3B), CLASS = 0b1000 (MEM-AP),
        # TYPE = 1 (AHB), VARIANT = 0, REVISION = 0.
        assert idr.jep106_bank == 4
        assert idr.jep106_id == 0x3B
        assert idr.klass == 0b1000
        assert idr.type == 0x1
        assert idr.variant == 0
        assert idr.revision == 0

    def test_round_trip(self):
        for raw in (0x04770001, 0x14770052, 0x84770005, 0x44760010):
            assert int(ApIdr.from_idr(raw)) == raw

    def test_is_same_ap_type_ignores_revision_and_variant(self):
        a = ApIdr.from_idr(0x04770001)  # rev=0, variant=0
        b = ApIdr.from_idr(0x14770051)  # rev=1, variant=5 — same type
        assert a.is_same_ap_type(b)
        # Different DESIGNER → not the same.
        c = ApIdr.from_idr(0x14760001)  # designer differs
        assert not a.is_same_ap_type(c)
        # Different TYPE → not the same.
        d = ApIdr.from_idr(0x04770002)
        assert not a.is_same_ap_type(d)

    def test_pretty_includes_manufacturer(self):
        idr = ApIdr.from_idr(0x04770001)
        s = idr.pretty()
        assert "0x04770001" in s
        assert "ARM" in s
        assert "class=0x8" in s
        assert "type=0x1" in s


# -- Fake DP for unit-testing Ap in isolation -----------------------

class FakeDp(Dp):
    """A Dp whose ``flush_ops`` resolves ApRead via a programmable
    register file keyed on absolute system address. ApWrite is
    recorded; DP/Run/Abort ops are no-ops. Lets Ap-level tests
    exercise reg_read/reg_write/discover without a JTAG-DP / Chain
    stack."""

    def __init__(self):
        super().__init__("fake")
        self.ap_regs: dict[int, int] = {}        # addr → value
        self.writes: list[tuple[int, int]] = []  # (addr, data)
        self.read_failure_at: int | None = None  # absolute addr

    def install_ap_reg(self, base: int, addr: int, value: int):
        self.ap_regs[base + addr] = value

    async def flush_ops(self, batch):
        from acrobe.component.arm.dp import (
            Abort, DpAccessFailure, Run,
        )
        for op, future in batch:
            if isinstance(op, ApRead):
                if self.read_failure_at == op.addr:
                    future.set_exception(DpAccessFailure("simulated"))
                    continue
                future.set_result(self.ap_regs.get(op.addr, 0))
            elif isinstance(op, ApWrite):
                self.writes.append((op.addr, op.data))
                future.set_result(None)
            elif isinstance(op, (DpRead,)):
                future.set_result(0)
            elif isinstance(op, (DpWrite, Abort, Run)):
                future.set_result(None)
            else:
                future.set_exception(
                    TypeError(f"FakeDp got {type(op).__name__}"))


# -- Naming heuristic ----------------------------------------------

class TestApNaming:
    def test_adiv5_style_apsel(self):
        # base = apsel << 24, low 24 bits zero → name "ap{apsel}".
        assert Ap(dp=FakeDp(), base=0).name == "ap0"
        assert Ap(dp=FakeDp(), base=1 << 24).name == "ap1"
        assert Ap(dp=FakeDp(), base=15 << 24).name == "ap15"
        assert Ap(dp=FakeDp(), base=0xff << 24).name == "ap255"

    def test_adiv6_arbitrary_base(self):
        # ADIv6 AP at non-(n<<24) address → "ap@{base:08x}".
        # 0x80001000 has low 24 bits non-zero, falls into the @addr path.
        ap = Ap(dp=FakeDp(), base=0x80001000)
        assert ap.name == "ap@80001000"
        ap = Ap(dp=FakeDp(), base=0x12345678)
        assert ap.name == "ap@12345678"

    def test_explicit_name_wins(self):
        ap = Ap(dp=FakeDp(), base=0, name="custom")
        assert ap.name == "custom"


# -- IDR field accessors -------------------------------------------

class TestIdrFields:
    def test_revision(self):
        # IDR[31:28] = 0xa.
        ap = Ap(dp=FakeDp(), base=0, idr=0xA0000000)
        assert ap.revision == 0xa

    def test_designer_arm(self):
        # IDR[27:17] = 0x23B (ARM JEP106 in 11-bit compressed form:
        # bank 4, code 0x3B → 4*128 + 0x3B = 0x23B).
        ap = Ap(dp=FakeDp(), base=0, idr=0x23B << 17)
        assert ap.designer == 0x23B

    def test_class_mem_ap(self):
        # IDR[16:13] = 0b1000.
        ap = Ap(dp=FakeDp(), base=0, idr=(0b1000 << 13))
        assert ap.klass == Ap.CLASS_MEM_AP

    def test_class_jtag_ap(self):
        # IDR[16:13] = 0b0000.
        ap = Ap(dp=FakeDp(), base=0, idr=0)
        assert ap.klass == Ap.CLASS_NONE

    def test_variant(self):
        # IDR[7:4] = 0xc.
        ap = Ap(dp=FakeDp(), base=0, idr=0xc0)
        assert ap.variant == 0xc

    def test_type(self):
        # IDR[3:0] = 0x1 (AHB).
        ap = Ap(dp=FakeDp(), base=0, idr=0x1)
        assert ap.type == 0x1


# -- Register access via DP ----------------------------------------

class TestRegisterAccess:
    @pytest.mark.asyncio
    async def test_reg_read_posts_apread(self):
        dp = FakeDp()
        dp.install_ap_reg(base=0, addr=0x10, value=0xdeadbeef)
        ap = Ap(dp=dp, base=0)

        v = await ap.reg_read(0x10)
        assert v == 0xdeadbeef

    @pytest.mark.asyncio
    async def test_reg_write_posts_apwrite(self):
        dp = FakeDp()
        ap = Ap(dp=dp, base=0x01000000)

        await ap.reg_write(0x04, 0xc0ffee01)
        assert (0x01000004, 0xc0ffee01) in dp.writes


# -- Discovery via Ap.db -------------------------------------------

class TestApDiscover:
    @pytest.mark.asyncio
    async def test_no_ap_idr_zero(self):
        dp = FakeDp()
        # No installed register: IDR reads as 0.
        ap = await Ap.discover(dp, base=0)
        assert ap is None

    @pytest.mark.asyncio
    async def test_unregistered_idr_returns_base_ap(self):
        dp = FakeDp()
        dp.install_ap_reg(base=0, addr=Ap.IDR, value=0xdeadbeef)
        ap = await Ap.discover(dp, base=0)
        assert ap is not None
        assert type(ap) is Ap
        assert ap.idr == 0xdeadbeef
        assert ap.base == 0

    @pytest.mark.asyncio
    async def test_registered_subclass_used(self):
        idr_match = 0x12345678

        class MyAp(Ap):
            def __init__(self, dp, base, idr, name=None):
                super().__init__(dp, base, idr, name)

        Ap.db.register(idr_match)(MyAp)
        try:
            dp = FakeDp()
            dp.install_ap_reg(base=0x02000000, addr=Ap.IDR, value=idr_match)
            ap = await Ap.discover(dp, base=0x02000000)
            assert isinstance(ap, MyAp)
            assert ap.idr == idr_match
        finally:
            Ap.db._registry.pop(idr_match, None)

    @pytest.mark.asyncio
    async def test_revision_and_variant_masked_in_lookup(self):
        # IDR equality masks REVISION (31:28) and VARIANT (7:4): one
        # registration covers all silicon revs and minor variants of
        # a given (DESIGNER, CLASS, TYPE). Use a vendor-neutral IDR
        # whose TYPE (0x3) is NOT covered by MemAp's registrations,
        # so this test stays isolated.
        idr_registered = 0x04770003

        class FakeAp(Ap):
            def __init__(self, dp, base, idr, name=None):
                super().__init__(dp, base, idr, name)

        Ap.db.register(idr_registered)(FakeAp)
        try:
            dp = FakeDp()
            # Chip reports the same TYPE/CLASS/DESIGNER but with a
            # bumped revision (0x1) and bumped variant (0x5).
            chip_idr = 0x14770053
            dp.install_ap_reg(base=0, addr=Ap.IDR, value=chip_idr)
            ap = await Ap.discover(dp, base=0)
            assert isinstance(ap, FakeAp)
            assert ap.revision == 0x1
            assert ap.variant == 0x5
        finally:
            Ap.db._registry.pop(idr_registered, None)

    @pytest.mark.asyncio
    async def test_dp_access_failure_returns_none(self):
        dp = FakeDp()
        dp.read_failure_at = 0x05000000 + Ap.IDR
        ap = await Ap.discover(dp, base=0x05000000)
        assert ap is None

    @pytest.mark.asyncio
    async def test_handler_construction_failure_falls_back_to_base(self):
        # A registered handler whose __init__ blows up shouldn't lose
        # the AP — discover logs and returns the generic base Ap.
        idr_registered = 0x04770009  # TYPE not used by MemAp (avoid collision)

        class BrokenAp(Ap):
            def __init__(self, dp, base, idr, name=None):
                raise RuntimeError("broken handler")

        Ap.db.register(idr_registered)(BrokenAp)
        try:
            dp = FakeDp()
            dp.install_ap_reg(base=0, addr=Ap.IDR, value=idr_registered)
            ap = await Ap.discover(dp, base=0)
            # Falls back to the base Ap class — the AP is still
            # surfaced in enumeration with its IDR.
            assert type(ap) is Ap
            assert ap.idr == idr_registered
        finally:
            Ap.db._registry.pop(idr_registered, None)
