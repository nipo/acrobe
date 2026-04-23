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
from acrobe.component.altera.sdm_jtag import SdmJtag
from acrobe.component.altera.sdm import SdmError
from acrobe import log


async def main():
    log.setup(level=logging.INFO)

    root_path = sys.argv[1] if len(sys.argv) > 1 else "tei-/jtag"

    hw_root = HwRoot()
    hw_root.add_enumerator(UsbEnumerator())

    parts = root_path.strip('/').split('/')
    leaf = await hw_root.child_summon(*parts)
    await leaf.start_tree()

    # Get the Agilex 5 TAP from the discovered chain
    from acrobe.component.altera.agilex5 import Agilex5
    tap = None
    for child in leaf._children:
        for sub in getattr(child, '_children', []):
            if isinstance(sub, Agilex5):
                tap = sub
                break

    if tap is None:
        print("No Agilex 5 TAP found in chain")
        return

    idcode = tap.idcode
    print(f"JTAG IDCODE: {idcode:#010x} ({tap.name})")

    interface = leaf._interface
    sdm = SdmJtag(interface)

    # Sync
    print("Syncing with SDM...")
    try:
        echoed = await sdm.sync()
        print(f"Sync OK, nonce echoed: {echoed:#010x}")
    except SdmError as e:
        print(f"Sync failed: {e}")
        return

    # GET_IDCODE (opcode 0x10)
    print("\nSending GET_IDCODE...")
    try:
        data = await sdm.command(0x10)
        sdm_idcode = int.from_bytes(data[:4], 'little')
        print(f"SDM IDCODE: {sdm_idcode:#010x}")
        if sdm_idcode == idcode:
            print("  Matches JTAG IDCODE!")
    except SdmError as e:
        print(f"GET_IDCODE failed: {e}")

    # GET_CHIPID (opcode 0x12)
    print("\nSending GET_CHIPID...")
    try:
        data = await sdm.command(0x12)
        chipid = int.from_bytes(data[:8], 'little')
        print(f"Chip ID: {chipid:#018x}")
    except SdmError as e:
        print(f"GET_CHIPID failed: {e}")

    # CONFIG_STATUS (opcode 0x04)
    print("\nSending CONFIG_STATUS...")
    try:
        data = await sdm.command(0x04)
        print(f"CONFIG_STATUS: {len(data)} bytes")
        for i in range(0, len(data), 4):
            w = int.from_bytes(data[i:i+4], 'little')
            print(f"  [{i//4}] {w:#010x}")
    except SdmError as e:
        print(f"CONFIG_STATUS failed: {e}")


asyncio.run(main())
