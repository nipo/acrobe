"""STAPL player for acrobe JTAG hardware.

Implements StaplPlayer using a raw JTAG interface (not a Tap).
"""

import asyncio

from ..bitstring import BitString
from ..protocol.jtag import CaptureIr, CaptureDr, Shift, Run
from .interpreter import StaplPlayer


class JtagPlayer(StaplPlayer):
    """StaplPlayer that drives hardware via a raw JTAG interface.

    Takes a JTAG interface (JtagMpsse or similar Batcher) directly,
    NOT a Tap. The STAPL program handles its own IR/DR assembly
    with pre/post chain bypass padding.

    TAP state transitions are approximated via run-test/idle cycles
    (IRPAUSE, DRPAUSE mapped to IDLE).
    """

    def __init__(self, interface):
        self._iface = interface
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

        if not self._started:
            await self._iface.post(Run(1))
            self._started = True
        await self._iface.post(CaptureIr())
        shift_op = Shift(combined, read_tdo=capture)
        result = await self._iface.post(shift_op)
        await self._iface.post(Run(1))

        if capture:
            tdo_all = bytes(result.data[:((total + 7) // 8)])
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

        await self._iface.post(CaptureDr())
        shift_op = Shift(combined, read_tdo=capture)
        result = await self._iface.post(shift_op)
        await self._iface.post(Run(1))

        if capture:
            tdo_all = bytes(result.data[:((total + 7) // 8)])
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
