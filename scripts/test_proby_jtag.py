#!/usr/bin/env python3
"""Integration test: JTAG chain discovery on Proby adapter.

Opens the Proby adapter (VID:PID 10eb:0026) on its internal JTAG chain
(Channel B), discovers the chain, and verifies the Spartan-6 FPGA
(IDCODE 0x24001093).

Usage:
    python -m scripts.test_proby_jtag
"""

import asyncio

from crobe_async.adapter.ftdi.transport import FtdiTransport
from crobe_async.adapter.ftdi.mpsse import MpsseEngine
from crobe_async.adapter.ftdi.jtag import JtagMpsse
from crobe_async.protocol.jtag import Chain

# Proby USB identifiers
PROBY_VID = 0x10eb
PROBY_PID = 0x0026

# Proby internal JTAG chain is on Channel B (interface index 1)
PROBY_JTAG_CHANNEL = 1

# Proby has resetn on pin 9 (GPIO H1)
PROBY_RESETN_PIN = 9

# Spartan-6 IDCODE
SPARTAN6_IDCODE = 0x24001093
SPARTAN6_IRLEN = 6


async def main():
    print(f"Opening Proby ({PROBY_VID:#06x}:{PROBY_PID:#06x}) channel B...")
    transport = await FtdiTransport.open(
        vid=PROBY_VID, pid=PROBY_PID,
        interface_index=PROBY_JTAG_CHANNEL)
    print(f"  Transport ready (MPS={transport._max_packet_size})")

    engine = MpsseEngine(transport)
    jtag = JtagMpsse(engine)

    # Configure GPIO:
    #   - JTAG pins (TCK, TDI, TMS) handled by JtagMpsse.setup()
    #   - resetn on pin 9 (GPIO H1): output, deasserted (high)
    resetn_bit = 1 << PROBY_RESETN_PIN
    await jtag.setup(gpio_oe=resetn_bit, gpio_val=resetn_bit)
    print("  JTAG interface initialized")

    # Discover chain
    chain = Chain(jtag)
    print("Discovering JTAG chain...")
    await chain.discover()

    print(f"Found {len(chain.children)} device(s):")
    for tap in chain.children:
        print(f"  IDCODE=0x{tap.idcode:08x} irlen={tap.irlen}")

    # Verify
    assert len(chain.children) == 1, f"Expected 1 device, found {len(chain.children)}"
    tap = chain.children[0]
    assert tap.idcode == SPARTAN6_IDCODE, \
        f"Expected IDCODE 0x{SPARTAN6_IDCODE:08x}, got 0x{tap.idcode:08x}"
    assert tap.irlen == SPARTAN6_IRLEN

    # Read IDCODE register via dynamic instruction
    idcode_val = await tap.ir(0x09, dr_length=32)()
    print(f"  IDCODE register read: 0x{int(idcode_val):08x}")
    assert int(idcode_val) == SPARTAN6_IDCODE

    print("\nAll checks passed!")

    await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
