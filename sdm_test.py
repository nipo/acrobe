#!/usr/bin/env python3.13
"""Quick SDM command test.

Usage:
    python3.13 sdm_test.py <adapter-path>
    e.g.: python3.13 sdm_test.py tei-/jtag

Syncs with SDM, sends GET_IDCODE, prints result.
"""

import asyncio
import logging
import sys

from acrobe.adapter.model import HwRoot, UsbEnumerator
from acrobe.component.altera.sdm_jtag import SdmJtagTransport, SdmError
from acrobe import log


async def main():
    log.setup(level=logging.INFO)

    root_path = sys.argv[1] if len(sys.argv) > 1 else "tei-/jtag"

    hw_root = HwRoot()
    hw_root.add_enumerator(UsbEnumerator())

    parts = root_path.strip('/').split('/')
    leaf = await hw_root.child_summon(*parts)
    await leaf.start_tree()

    # Get the raw JTAG interface
    interface = leaf._interface

    # Detect device via JTAG IDCODE (reset TAP, read DR)
    from acrobe.bitstring import BitString
    from acrobe.protocol.jtag import CaptureIr, CaptureDr, Shift, Run

    await interface.post(Run(5))  # Reset TAP
    await interface.post(Run(1))  # Enter RTI
    await interface.post(CaptureDr())
    shift = Shift(BitString(0, 32), read_tdo=True)
    result = await interface.post(shift)
    await interface.post(Run(1))

    idcode = int.from_bytes(bytes(result.tdo.data[:4]), 'little')
    print(f"JTAG IDCODE: {idcode:#010x}")

    # Pick sync nonce based on IDCODE
    part = idcode & 0x0FFFFFFF
    nonces = {
        0x0362c0dd: 0xAB92C300,  # DE25
        0x0364f0dd: 0x7F38963E,  # AXE5
    }
    nonce = nonces.get(part)
    if nonce is None:
        print(f"Unknown part {part:#010x}, cannot sync")
        return

    sdm = SdmJtagTransport(interface)

    # Sync
    print("Syncing with SDM...")
    try:
        echoed = await sdm.sync(nonce)
        print(f"Sync OK, nonce echoed: {echoed:#010x}")
    except SdmError as e:
        print(f"Sync failed: {e}")
        return

    # GET_IDCODE (opcode 0x10)
    print("\nSending GET_IDCODE (opcode 0x10)...")
    error, data = await sdm.command(0x10)
    if error is None:
        print("GET_IDCODE: no response")
    elif error:
        print(f"GET_IDCODE error: {error}")
    elif data:
        print(f"SDM IDCODE: {data[0]:#010x}")
        if data[0] == idcode:
            print("  Matches JTAG IDCODE!")
    else:
        print("GET_IDCODE: empty response")

    # CONFIG_STATUS (opcode 0x04)
    print("\nSending CONFIG_STATUS (opcode 0x04)...")
    error, data = await sdm.command(0x04, max_response=8)
    if error is None:
        print("CONFIG_STATUS: no response")
    elif error:
        print(f"CONFIG_STATUS error: {error}")
    else:
        print(f"CONFIG_STATUS: {len(data)} words")
        for i, w in enumerate(data):
            print(f"  [{i}] {w:#010x}")


asyncio.run(main())
