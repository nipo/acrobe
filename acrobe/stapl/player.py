"""STAPL player for acrobe JTAG hardware.

Implements StaplPlayer using a Tap instance for JTAG communication.
"""

import asyncio

from ..bitstring import BitString
from ..protocol.jtag import CaptureIr, CaptureDr, Shift, Run
from .interpreter import StaplPlayer


class AcrobePlayer(StaplPlayer):
    """StaplPlayer that drives hardware via an acrobe Tap.

    Handles IR/DR scans with chain pre/post bypass padding.
    Approximates TAP state transitions via run-test/idle cycles
    (IRPAUSE, DRPAUSE mapped to IDLE — exact state support
    requires Interface-level access).
    """

    def __init__(self, tap):
        self._tap = tap
        self._iface = tap._interface
        self._current_ir = None
        self._started = False

    async def ir_scan(self, length, tdi, pre, post, capture):
        pre_bits, pre_data = pre
        post_bits, post_data = post

        combined = BitString()
        if pre_bits:
            combined.append(pre_data, pre_bits)
        combined.append(tdi, length)
        if post_bits:
            combined.append(post_data, post_bits)

        total = len(combined)
        ir_val = int.from_bytes(tdi, 'little') & ((1 << length) - 1)
        self._current_ir = ir_val

        # IR scan: shift data into the IR register.
        # Use the JTAG interface directly (CaptureIr + Shift).
        # Ensure we're in RTI before the first scan (chain discovery
        # may leave the TAP in RESET state).
        if not self._started:
            await self._iface.post(Run(1))
            self._started = True
        await self._iface.post(CaptureIr())
        shift_op = Shift(combined, read_tdo=capture)
        result = await self._iface.post(shift_op)
        await self._iface.post(Run(1))

        # Invalidate Tap's IR cache (we shifted IR directly)
        self._tap._current_ir = None

        if capture:
            tdo_all = bytes(result.tdo.data[:((total + 7) // 8)])
            tdo_bits = BitString(tdo_all, total)
            return bytes(tdo_bits[pre_bits:pre_bits + length])
        return None

    async def dr_scan(self, length, tdi, pre, post, capture):
        pre_bits, pre_data = pre
        post_bits, post_data = post

        combined = BitString()
        if pre_bits:
            combined.append(pre_data, pre_bits)
        combined.append(tdi, length)
        if post_bits:
            combined.append(post_data, post_bits)

        total = len(combined)

        # DR scan: shift data into the DR register.
        await self._iface.post(CaptureDr())
        shift_op = Shift(combined, read_tdo=capture)
        result = await self._iface.post(shift_op)
        await self._iface.post(Run(1))

        if capture:
            tdo_all = bytes(result.tdo.data[:((total + 7) // 8)])
            tdo_bits = BitString(tdo_all, total)
            return bytes(tdo_bits[pre_bits:pre_bits + length])
        return None

    async def state(self, target, path=None):
        t = target.upper()
        if t == 'RESET':
            await self._iface.post(Run(5))
        else:
            await self._iface.post(Run(1))

    async def wait(self, wait_state, cycles, usecs, end_state):
        if wait_state:
            await self.state(wait_state)
        if cycles:
            await self._iface.post(Run(cycles))
        if usecs:
            await asyncio.sleep(usecs / 1_000_000)
        if end_state and end_state != wait_state:
            await self.state(end_state)

    async def trst(self, cycles, usecs):
        await self._iface.post(Run(5))

    async def note(self, text):
        print(f"NOTE: {text}")

    async def export(self, key, value):
        print(f"EXPORT {key} = {value}")
