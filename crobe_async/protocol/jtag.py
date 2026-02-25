import asyncio
import inspect

from ..engine import Batcher
from ..component import Component
from ..bitstring import BitString, BitStringBase
from ..db import Db, NoMatch


# JTAG Interface Operations

class Shift:
    """Shift data through TDI/TDO."""

    def __init__(self, tdi, read_tdo=True):
        self.tdi = tdi
        self.read_tdo = read_tdo
        self.tdo = None

    def __repr__(self):
        return f"Shift(tdi={self.tdi!r}, read_tdo={self.read_tdo})"


class CaptureDr:
    """Transition FSM to Capture-DR."""

    def __repr__(self):
        return "CaptureDr()"


class CaptureIr:
    """Transition FSM to Capture-IR."""

    def __repr__(self):
        return "CaptureIr()"


class Reset:
    """TAP reset via TMS."""

    def __init__(self, count=5):
        self.tms = BitString(-1, max(count, 5))

    def __repr__(self):
        return f"Reset(count={len(self.tms)})"


class Run:
    """Run TCK cycles in Run-Test/Idle."""

    def __init__(self, cycles):
        self.cycles = cycles

    def __repr__(self):
        return f"Run(cycles={self.cycles})"


class SwdToJtag:
    """SWD-to-JTAG switch sequence."""

    tms = BitString(-1, 50) + BitString(0xe73c, 16) + BitString(-1, 5)

    def __repr__(self):
        return "SwdToJtag()"


# Internal Tap Operations

class _TapShift:
    def __init__(self, ir_value, tdi, read_tdo, postprocess=None):
        self.ir_value = ir_value
        self.tdi = tdi
        self.read_tdo = read_tdo
        self.postprocess = postprocess

    def __repr__(self):
        return f"_TapShift(ir={self.ir_value:#x}, tdi={self.tdi!r}, read_tdo={self.read_tdo})"


class _TapRun:
    def __init__(self, cycles):
        self.cycles = cycles

    def __repr__(self):
        return f"_TapRun(cycles={self.cycles})"


# Instruction Registry

class Dr:
    """Class-level descriptor for a data register."""

    def __init__(self, length=None, type=None):
        self.length = length
        self.type = type

    def _spawn(self, name, tap):
        return TapDr(tap, name, length=self.length, type=self.type)


class Instruction:
    """Class-level descriptor for a JTAG instruction. References a Dr by name."""

    def __init__(self, ir, dr=None):
        self.ir = ir
        self.dr = dr

    def _spawn(self, name, tap):
        dr = None
        if self.dr is not None:
            dr = getattr(tap, self.dr)
        return TapInstruction(tap, name, self.ir, dr)


class TapDr:
    """A data register bound to a specific Tap instance."""

    def __init__(self, tap, name, length=None, type=None):
        self.tap = tap
        self.name = name
        self.length = length
        self.type = type

    def __repr__(self):
        return f"<Dr {self.name} length={self.length}>"


class TapInstruction:
    """A bound instruction. Callable: returns Future resolving to TDO value."""

    def __init__(self, tap, name, ir, dr):
        self.tap = tap
        self.name = name
        self.ir = ir
        self.dr = dr

    def __call__(self, tdi=None, read_tdo=None):
        """Post a DR shift with this instruction. Returns Future -> TDO value."""
        return self.tap._post_instruction(self, tdi, read_tdo)

    def __int__(self):
        return int(self.ir) & ((1 << self.tap.irlen) - 1)

    def __repr__(self):
        return f"<Instruction {self.name} ir={int(self):#x}>"


class InstructionRegistry:
    """Mixin: spawns Dr and Instruction class attributes into bound instances."""

    BYPASS_REG = Dr(1)
    BYPASS = Instruction(-1, "BYPASS_REG")

    def _init_instructions(self):
        # Pass 1: spawn Drs
        for name in dir(type(self)):
            obj = inspect.getattr_static(type(self), name)
            if isinstance(obj, Dr):
                setattr(self, name, obj._spawn(name, self))
        # Pass 2: spawn Instructions (they reference spawned Drs)
        for name in dir(type(self)):
            obj = inspect.getattr_static(type(self), name)
            if isinstance(obj, Instruction):
                setattr(self, name, obj._spawn(name, self))

    def instructions(self):
        for v in self.__dict__.values():
            if isinstance(v, TapInstruction):
                yield v


# Dynamic Instruction

class _DynamicInstruction:
    """Callable for ad-hoc IR values not in the InstructionRegistry."""

    def __init__(self, tap, ir_value, dr_length=None):
        self._tap = tap
        self._ir_value = int(ir_value) & ((1 << tap.irlen) - 1)
        self._dr_length = dr_length

    def __call__(self, tdi=None, read_tdo=None):
        if tdi is None:
            if read_tdo is None:
                read_tdo = self._dr_length is not None
            if read_tdo and self._dr_length is not None:
                tdi = BitString(0, self._dr_length)
            elif read_tdo:
                raise ValueError("Cannot determine shift length")
        else:
            if read_tdo is None:
                read_tdo = True
            if isinstance(tdi, int):
                if self._dr_length is not None:
                    tdi = BitString(tdi, self._dr_length)
                else:
                    raise ValueError("Cannot determine shift length from int")
            elif not isinstance(tdi, BitStringBase):
                raise TypeError("tdi must be int, BitString, or None")

        op = _TapShift(self._ir_value, tdi, read_tdo)
        return self._tap.post(op)

    def __repr__(self):
        return f"<DynamicInstruction ir={self._ir_value:#x}>"


# Tap

class Tap(Batcher, Component, InstructionRegistry):
    irlen = None
    db = Db("TAP idcode")

    def __init__(self, interface, idcode=None, irlen=None, name=None):
        if irlen is not None:
            self.irlen = irlen
        self.idcode = idcode
        self.ir_pre = 0
        self.ir_post = 0
        self.dr_pre = 0
        self.dr_post = 0
        self._current_ir = None
        self._interface = interface

        if name is None:
            name = f"TAP[0x{int(idcode):08x}]" if isinstance(idcode, int) else "TAP"

        Batcher.__init__(self)
        Component.__init__(self, name)
        self._init_instructions()

    def position_set(self, ir_pre, dr_pre, ir_post=None, dr_post=None):
        self.ir_pre = ir_pre
        self.dr_pre = dr_pre
        if ir_post is not None:
            self.ir_post = ir_post
        if dr_post is not None:
            self.dr_post = dr_post

    def ir(self, value, dr_length=None):
        """Create a dynamic instruction for an ad-hoc IR value."""
        return _DynamicInstruction(self, value, dr_length)

    def run(self, cycles=1):
        """Post a run operation. Returns Future."""
        return self.post(_TapRun(cycles))

    def _post_instruction(self, instr, tdi, read_tdo):
        """Post a DR shift for a given instruction. Called by TapInstruction.__call__."""
        ir_value = int(instr.ir) & ((1 << self.irlen) - 1)
        postprocess = None

        if tdi is None:
            if read_tdo is None or read_tdo:
                # Read-only: need length from DR
                if instr.dr and instr.dr.length is not None:
                    tdi = BitString(0, instr.dr.length)
                    read_tdo = True
                elif read_tdo:
                    raise ValueError("Cannot determine shift length for read-only shift")
                else:
                    read_tdo = False
            # tdi stays None -> IR-only shift (only shift IR, no DR)
        else:
            if read_tdo is None:
                read_tdo = True
            if isinstance(tdi, int):
                if instr.dr and instr.dr.length is not None:
                    tdi = BitString(tdi, instr.dr.length)
                else:
                    raise ValueError("Cannot determine shift length from int without DR length")
            elif not isinstance(tdi, BitStringBase):
                raise TypeError(f"tdi must be int, BitString, or None")

        if instr.dr and instr.dr.type:
            postprocess = instr.dr.type

        op = _TapShift(ir_value, tdi, read_tdo, postprocess)
        return self.post(op)

    async def flush_ops(self, batch):
        """Translate tap-level ops into JTAG interface ops."""
        jtag_futures = []
        # Track which batch entries need TDO extraction
        tdo_info = []  # list of (batch_index, jtag_shift_future, postprocess_fn)

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, _TapShift):
                # IR shift if IR changed
                if op.ir_value != self._current_ir:
                    ir_data = (BitString(-1, self.ir_pre) +
                               BitString(op.ir_value, self.irlen) +
                               BitString(-1, self.ir_post))
                    jtag_futures.append(self._interface.post(CaptureIr()))
                    jtag_futures.append(self._interface.post(
                        Shift(ir_data, read_tdo=False)))
                    self._current_ir = op.ir_value

                # DR shift
                if op.tdi is not None:
                    jtag_futures.append(self._interface.post(CaptureDr()))

                    if self.dr_pre:
                        jtag_futures.append(self._interface.post(
                            Shift(BitString(0, self.dr_pre), read_tdo=False)))

                    data_shift = Shift(op.tdi, read_tdo=op.read_tdo)
                    data_future = self._interface.post(data_shift)
                    jtag_futures.append(data_future)

                    if self.dr_post:
                        jtag_futures.append(self._interface.post(
                            Shift(BitString(0, self.dr_post), read_tdo=False)))

                    if op.read_tdo:
                        tdo_info.append((idx, data_future, op.postprocess))

            elif isinstance(op, _TapRun):
                jtag_futures.append(self._interface.post(Run(op.cycles)))

        # Await all JTAG interface futures
        if jtag_futures:
            await asyncio.gather(*jtag_futures)

        # Resolve futures with TDO values
        for idx, data_future, postprocess in tdo_info:
            shift_op = data_future.result()  # The Shift op, with .tdo populated
            tdo = shift_op.tdo
            if postprocess is not None and tdo is not None:
                tdo = postprocess(tdo)
            batch[idx][1].set_result(tdo)

        # Resolve remaining futures with None
        for op, future in batch:
            if not future.done():
                future.set_result(None)

    def __repr__(self):
        return f"<Tap {self._name} irlen={self.irlen}>"


# Chain

class Chain(Component):
    """JTAG Chain. Holds TAPs and manages chain geometry."""

    irlen_db = Db("IDCODE irlen")

    def __init__(self, interface, name="chain"):
        super().__init__(name)
        self._interface = interface
        self.total_irlen = 0
        self.total_drlen = 0

    async def discover(self, max_devices=8):
        """Discover JTAG chain by reading IDCODEs after TAP reset.

        Reads IDCODEs from the DR chain and looks up IR lengths
        via Chain.irlen_db. Creates and adds TAP objects.

        Raises NoMatch if an IDCODE has no registered IR length.
        """
        # Reset all TAPs
        self._interface.post(Reset())
        # Enter Run-Test/Idle
        self._interface.post(Run(1))
        # Capture DR (all devices have IDCODE loaded after reset)
        self._interface.post(CaptureDr())
        # Shift zeros through, reading IDCODEs
        shift = Shift(BitString(0, 32 * max_devices), read_tdo=True)
        result = await self._interface.post(shift)

        tdo = result.tdo
        pos = 0
        idcodes = []
        while pos + 32 <= len(tdo):
            if not tdo[pos]:
                break  # No more IDCODEs (bit 0 = 0 means no IDCODE)
            idcode = int(tdo[pos:pos + 32])
            if idcode == 0xFFFFFFFF:
                break
            idcodes.append(idcode)
            pos += 32

        # Create TAPs with looked-up IR lengths
        for idcode in idcodes:
            irlen = self.irlen_db.call(idcode, idcode)
            self.tap_add(idcode, irlen)

        # Return to Run-Test/Idle
        await self._interface.post(Run(1))

    def tap_add(self, idcode, irlen, ir_pre=None, dr_pre=None):
        """Add a TAP to the chain at the current end position."""
        if ir_pre is None:
            ir_pre = self.total_irlen
        if dr_pre is None:
            dr_pre = self.total_drlen

        try:
            tap = Tap.db.call(idcode, self._interface, idcode, irlen=irlen)
        except NoMatch:
            tap = Tap(self._interface, idcode=idcode, irlen=irlen)

        self.total_irlen += irlen
        self.total_drlen += 1

        ir_post = self.total_irlen - ir_pre - irlen
        dr_post = self.total_drlen - dr_pre - 1
        tap.position_set(ir_pre, dr_pre, ir_post, dr_post)

        # Update existing TAPs' post values
        for child in self.children:
            if child is not tap:
                if child.ir_pre < ir_pre:
                    child.ir_post += irlen
                    child.dr_post += 1
                else:
                    child.ir_pre += irlen
                    child.dr_pre += 1

        self.child_add(tap)
        return tap

    def __repr__(self):
        return f"<Chain {self._name} taps={len(self._children)}>"
