"""Tests for the abstract DP op classes."""

from dataclasses import FrozenInstanceError

import pytest

from acrobe.component.arm.dp import (
    Abort, ApRead, ApWrite, Dp, DpAccessFailure,
    DpRead, DpWrite, Run,
)


class TestOpsAreFrozen:
    """Frozen-dataclass invariants — no in-place mutation, hashable,
    re-postable (i.e. usable across batches)."""

    def test_apread_immutable(self):
        op = ApRead(ap=0, addr=0x0c)
        with pytest.raises(FrozenInstanceError):
            op.addr = 0x10

    def test_apwrite_immutable(self):
        op = ApWrite(ap=0, addr=0, data=0)
        with pytest.raises(FrozenInstanceError):
            op.data = 1

    def test_dpread_immutable(self):
        op = DpRead(addr=0x04)
        with pytest.raises(FrozenInstanceError):
            op.addr = 0x08

    def test_dpwrite_immutable(self):
        op = DpWrite(addr=0x04, data=0xdeadbeef)
        with pytest.raises(FrozenInstanceError):
            op.data = 0

    def test_abort_immutable(self):
        op = Abort()
        with pytest.raises(FrozenInstanceError):
            op.what = 0

    def test_run_immutable(self):
        op = Run(cycles=8)
        with pytest.raises(FrozenInstanceError):
            op.cycles = 0

    def test_no_result_field(self):
        # Frozen ops carry only inputs; results travel via futures.
        assert not hasattr(ApRead(ap=0, addr=0), "data")
        assert not hasattr(DpRead(addr=0), "data")

    def test_value_equality(self):
        assert ApRead(ap=0, addr=0x0c) == ApRead(ap=0, addr=0x0c)
        assert DpWrite(addr=0x04, data=1) == DpWrite(addr=0x04, data=1)
        assert ApRead(ap=0, addr=0) != ApRead(ap=1, addr=0)

    def test_hashable(self):
        # Hashability matters for using ops as dict keys in a lowering.
        {ApRead(ap=0, addr=0): 1, DpRead(addr=0): 2}


class TestAbortDefault:
    def test_default_clears_all_sticky(self):
        # 0x1f covers STICKYORUN, STICKYCMP, STICKYERR, WDATAERR plus
        # the spec-defined low bit; the lowering writes this to the
        # ABORT register.
        assert Abort().what == 0x1f


class TestDpRegisters:
    """Spot-check the DP register address constants used by the
    lowering layers."""

    def test_dpidr_offset(self):
        assert Dp.DPIDR == 0x00

    def test_ctrl_stat_offset(self):
        assert Dp.CTRL_STAT == 0x04

    def test_select_offset(self):
        assert Dp.SELECT == 0x08

    def test_rdbuff_offset(self):
        assert Dp.RDBUFF == 0x0c

    def test_dpidr1_in_bank1(self):
        # ADIv6: DPIDR1 is at DPBANKSEL=1, low addr 0x0.
        assert (Dp.DPIDR1 >> 4) == 1
        assert (Dp.DPIDR1 & 0xc) == 0x0

    def test_pwrup_masks(self):
        assert Dp.PWRUP_REQ_MASK == Dp.CDBGPWRUPREQ | Dp.CSYSPWRUPREQ
        assert Dp.PWRUP_ACK_MASK == Dp.CDBGPWRUPACK | Dp.CSYSPWRUPACK


class TestDpAbstract:
    def test_flush_ops_is_abstract(self):
        # Constructing Dp directly is allowed (Node + Batcher are
        # concrete) but flush_ops must be overridden by subclasses.
        # We use no event loop here — just confirm the contract.
        d = Dp("test")

        async def go():
            await d.flush_ops([])

        import asyncio
        with pytest.raises(NotImplementedError):
            asyncio.run(go())


class TestDpAccessFailureType:
    def test_is_exception(self):
        assert issubclass(DpAccessFailure, Exception)
