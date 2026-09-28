"""Chip flow against a remote JTAG interface.

Server hosts a fake JtagInterface that implements the minimum
needed for Chain.discover to find a single IDCODE, claimed by a
minimal test-local SRAM FPGA Tap.
Client walks `wire/srv/fakeadapter/jtag/chain/0` through the
cutoff machinery, then runs the same flow as
`acrobe chip` (start_tree + Field.discover + lookup Target).
"""

import textwrap
from dataclasses import dataclass

import pytest
from aiohttp.test_utils import TestServer

from acrobe.adapter.model import HwRoot
from acrobe.bitstring import BitString
from acrobe.component.fpga import JtagSramFpga, SramFpga
from acrobe.configuration import Configuration
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol.jtag import (
    CaptureDr,
    CaptureIr,
    JtagInterface,
    Reset,
    Run,
    Shift,
)
from acrobe.part_id import PartId
from acrobe.protocol.jtag import Tap
from acrobe.target import Target
from acrobe.wire import WireEnumerator
from acrobe.wire.server import make_app


FPGA_IDCODE = 0x1badf00f  # claimed by no real part


class _FakeFpga(Tap, JtagSramFpga):
    """Minimal JTAG SRAM FPGA. Reports itself unconfigured, so
    starting it does not need anything the simulator can't answer."""

    def __init__(self, idcode, **kw):
        super().__init__(idcode=idcode, name="fakefpga", **kw)

    async def is_configured(self):
        return False


@pytest.fixture(autouse=True)
def _ensure_fake_fpga_registration():
    """test_jtag.py clears Tap.db.registry in some teardowns. Register
    _FakeFpga here so Chain.tap_add finds it regardless of test order."""
    if not any(_FakeFpga in handlers
               for handlers in Tap.db.registry.values()):
        Tap.db.register(PartId.from_idcode(FPGA_IDCODE))(_FakeFpga)
    yield


class _SingleFpgaInterface(JtagInterface):
    """Mock JtagInterface backed by the same shift-register simulator
    as test_jtag.py's ChainSimulator. Pre-configured with one FPGA
    device so Chain.discover() succeeds against it."""

    def __init__(self, devices=((FPGA_IDCODE, 10),)):
        super().__init__(name="jtag")
        self.devices = devices
        self._reg_val = 0
        self._reg_len = 0
        self._bypass = False
        self._in_ir = False

    async def flush_ops(self, batch):
        for op, future in batch:
            tdo = None
            if isinstance(op, Reset):
                self._bypass = False
                self._in_ir = False
            elif isinstance(op, CaptureDr):
                self._in_ir = False
                if self._bypass:
                    self._reg_val = 0
                    self._reg_len = len(self.devices)
                else:
                    val, pos = 0, 0
                    for idcode, _ in self.devices:
                        val |= idcode << pos
                        pos += 32
                    self._reg_val = val
                    self._reg_len = pos
            elif isinstance(op, CaptureIr):
                self._in_ir = True
                val, pos = 0, 0
                for _, irlen in self.devices:
                    val |= 1 << pos
                    pos += irlen
                self._reg_val = val
                self._reg_len = pos
            elif isinstance(op, Shift):
                tdo = self._do_shift(op)
            future.set_result(tdo)

    def _do_shift(self, op):
        L = self._reg_len
        N = len(op.tdi)
        tdi_val = int(op.tdi)
        tdo = None
        if op.read_tdo:
            if N <= L:
                tdo = BitString(self._reg_val & ((1 << N) - 1), N)
            else:
                tdo_val = (self._reg_val | (tdi_val << L)) & ((1 << N) - 1)
                tdo = BitString(tdo_val, N)
        if L > 0 and N >= L:
            new_val = (tdi_val >> (N - L)) & ((1 << L) - 1)
            self._reg_val = new_val
            if self._in_ir and new_val == (1 << L) - 1:
                self._bypass = True
        return tdo


def _build_remote_tree():
    iface = _SingleFpgaInterface()
    adapter = Node("fakeadapter")
    adapter.child_add(iface)
    root = Node("HwRoot")
    root.child_add(adapter)
    return root


async def _make_local_root(server_url, tmp_path):
    cfg = tmp_path / "acrobe.conf"
    cfg.write_text(textwrap.dedent(f"""
        wire:
          servers:
            srv:
              base: {server_url}
    """).strip())
    root = HwRoot()
    root.add_enumerator(WireEnumerator(configuration=Configuration(path=cfg)))
    await root.ensure_started()
    return root


@pytest.mark.asyncio
async def test_remote_tap_class_identity(tmp_path):
    """Walking to chain/0 on the remote side lands on the Tap class
    registered for the IDCODE, same as the local case would."""
    app = make_app(_build_remote_tree())
    async with TestServer(app) as server:
        local = await _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            leaf = await local.child_summon(
                "wire", "srv", "fakeadapter", "jtag", "chain", "0")
            assert isinstance(leaf, _FakeFpga)
            # SramFpga is what the FPGA Target explorer is registered for.
            assert isinstance(leaf, SramFpga), (
                f"expected leaf to be SramFpga, got "
                f"{type(leaf).__name__} with MRO {[c.__name__ for c in type(leaf).__mro__]}")
        finally:
            await local.stop_tree()


@pytest.mark.asyncio
async def test_chip_flow_discovers_target(tmp_path):
    """The flow `acrobe chip` runs, against a remote FPGA."""
    app = make_app(_build_remote_tree())
    async with TestServer(app) as server:
        local = await _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            leaf = await local.child_summon(
                "wire", "srv", "fakeadapter", "jtag", "chain", "0")
            await leaf.start_tree()

            await local.discover_targets()

            targets = local.children_of_class(Target)

            assert targets, (
                f"No targets found. Leaf: {type(leaf).__name__}")
        finally:
            await local.stop_tree()
