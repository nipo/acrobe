#!/usr/bin/env python3.13
"""Standalone Agilex 5 configuration test.

Usage:
    python3.13 config_test.py <adapter-path> <rbf-path>
    e.g.: python3.13 config_test.py ub3-/jtag /tmp/de25-nano-spi/data/J2.bin
"""

import asyncio
import logging
import sys

from acrobe.adapter.model import HwRoot, UsbEnumerator
from acrobe.component.altera.agilex5 import Agilex5
from acrobe import log


async def main():
    log.setup(level=logging.INFO)

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <adapter-path> <rbf-path>")
        sys.exit(1)

    root_path = sys.argv[1]
    rbf_path = sys.argv[2]

    with open(rbf_path, 'rb') as f:
        bitstream = f.read()
    print(f"Bitstream: {len(bitstream)} bytes")

    hw_root = HwRoot()
    hw_root.add_enumerator(UsbEnumerator())

    parts = root_path.strip('/').split('/')
    tap = await hw_root.child_summon(*parts, 'chain', '0')

    assert isinstance(tap, Agilex5), f"Expected Agilex5, got {type(tap).__name__}"

    # Create a fake program segment for load()
    class Segment:
        def __init__(self, data):
            self.data = data

    await tap.load([Segment(bitstream)])
    print("Configuration successful!")

if __name__ == "__main__":
    asyncio.run(main())
