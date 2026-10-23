"""Cypress EZ-USB FX3 TAP registration."""

import acrobe.component  # noqa: F401
from acrobe.component.cypress.fx3 import Fx3Tap
from acrobe.part_id import PartId
from acrobe.protocol.jtag import Tap


def test_fx3_registered():
    assert Tap.db.get(int(PartId(0, 0x34, 0x7926, 1))) == [Fx3Tap]
