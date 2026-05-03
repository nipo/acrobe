"""Tests for the SWD protocol layer (frozen-dataclass ops + the
abstract :class:`swd.Interface`). Backend-specific concerns (wire
encoding, bit-bang, ACK extraction) live in their own tests."""

import dataclasses

import pytest

from acrobe.protocol import swd


class TestAck:
    def test_ok(self):
        assert int(swd.Ack.OK) == 0b001

    def test_wait(self):
        assert int(swd.Ack.WAIT) == 0b010

    def test_fault(self):
        assert int(swd.Ack.FAULT) == 0b100


class TestOpsAreFrozen:
    """Ops are inputs only — futures carry results. Mutation is a bug."""

    def test_read_frozen(self):
        op = swd.Read(ap=False, addr=0x00)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.addr = 0x04

    def test_write_frozen(self):
        op = swd.Write(ap=True, addr=0x04, data=0x12345678)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.data = 0xdeadbeef

    def test_run_frozen(self):
        op = swd.Run(cycles=10)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.cycles = 20

    def test_wakeup_frozen(self):
        op = swd.Wakeup(cycles=100)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.cycles = 50

    def test_no_legacy_result_fields(self):
        """The legacy `op.data` / `op.ack` slots from the
        synchronous design were removed when these became frozen
        inputs. Make sure they don't drift back."""
        r = swd.Read(ap=False, addr=0)
        w = swd.Write(ap=False, addr=0, data=0)
        for legacy in ("data", "ack"):
            assert not hasattr(r, legacy), f"Read.{legacy} reappeared"
        for legacy in ("ack",):
            assert not hasattr(w, legacy), f"Write.{legacy} reappeared"


class TestSequenceMarkers:
    """JtagToSwd / LineReset are markers only — the actual bit
    pattern is the backend's responsibility."""

    def test_jtag_to_swd(self):
        # Just ensure the marker exists and is hashable (frozen).
        a = swd.JtagToSwd()
        b = swd.JtagToSwd()
        assert a == b
        assert hash(a) == hash(b)

    def test_line_reset(self):
        a = swd.LineReset()
        b = swd.LineReset()
        assert a == b


class TestInterfaceAbstract:
    def test_flush_not_implemented(self):
        iface = swd.Interface(name="t")
        import asyncio
        with pytest.raises(NotImplementedError):
            asyncio.run(iface.flush_ops([]))

    def test_db_present(self):
        # `swd.Interface.db` is the registry adapter-specific
        # interfaces don't use directly, but the standard SwDp
        # factory registers under "dap".
        assert isinstance(swd.Interface.db, type(swd.Interface.db))

    def test_dap_factory_registered(self):
        # Importing the arm package wires SwDp into the registry.
        import acrobe.component.arm  # noqa: F401
        # Spawning "dap" should yield a SwDp.
        from acrobe.component.arm.sw_dp import SwDp
        iface = swd.Interface(name="t")
        # db.acall("dap", iface) returns the SwDp from the factory.
        import asyncio
        result = asyncio.run(swd.Interface.db.acall("dap", iface))
        assert isinstance(result, SwDp)


class TestErrors:
    def test_swd_wait_is_access_failure(self):
        assert issubclass(swd.SwdWait, swd.SwdAccessFailure)
