import asyncio
import inspect
import math

from ..engine import Batcher
from ..node import Node
from ..freq_capper import FreqCapper
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
    def __init__(self, ir_value, tdi, read_tdo):
        self.ir_value = ir_value
        self.tdi = tdi
        self.read_tdo = read_tdo

    def __repr__(self):
        if ir is not None:
            ir = f"{self.ir_value:#x}"
        return f"_TapShift(ir={ir}, tdi={self.tdi!r}, read_tdo={self.read_tdo})"

class _TapRun:
    def __init__(self, cycles):
        self.cycles = cycles

    def __repr__(self):
        return f"_TapRun(cycles={self.cycles})"

class _TapIrStatus:
    def __repr__(self):
        return "_TapIrStatus()"


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

def _idcode_eq(key, lookup):
    """Compare IDCODEs ignoring the 4-bit revision field (bits 31:28)."""
    return (key & 0x0FFFFFFF) == (lookup & 0x0FFFFFFF)


class Tap(Batcher, Node, InstructionRegistry):
    irlen = None
    max_freq = None
    db = Db("TAP idcode", eq_func=_idcode_eq)

    def __init__(self, interface, idcode=None, irlen=None, name=None):
        if irlen is not None:
            self.irlen = irlen
        self.idcode = idcode
        self.ir_pre = 0
        self.ir_post = 0
        self.dr_pre = 0
        self.dr_post = 0
        self._interface = interface

        if name is None:
            name = f"TAP[0x{int(idcode):08x}]" if isinstance(idcode, int) else "TAP"

        Batcher.__init__(self)
        Node.__init__(self, name)
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

    def ir_status(self):
        """Post an IR status capture. Shifts BYPASS into IR, returns Future -> BitString.

        The captured IR value contains status bits (device-specific).
        This always forces an IR shift regardless of _current_ir tracking.
        """
        return self.post(_TapIrStatus())

    def _post_instruction(self, instr, tdi, read_tdo):
        """Post a DR shift for a given instruction. Called by TapInstruction.__call__."""
        ir_value = int(instr.ir) & ((1 << self.irlen) - 1)

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

        op = _TapShift(ir_value, tdi, read_tdo)
        return self.post(op)

    async def flush_ops(self, batch):
        """Translate tap-level ops into JTAG interface ops."""
        self.logger.protocol("Tap batch: %s", [op for op, _ in batch])
        jtag_futures = []
        # Track which batch entries need TDO extraction
        tdo_info = []  # list of (batch_index, jtag_shift_future)
        current_ir = None
        bypass_val = (1 << self.irlen) - 1

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, _TapIrStatus):
                # Capture IR status: split IR shift into pre/data/post
                # so we can read just our device's captured IR value.
                jtag_futures.append(self._interface.post(CaptureIr()))

                if self.ir_pre:
                    jtag_futures.append(self._interface.post(
                        Shift(BitString(-1, self.ir_pre), read_tdo=False)))

                data_shift = Shift(BitString(bypass_val, self.irlen), read_tdo=True)
                data_future = self._interface.post(data_shift)
                jtag_futures.append(data_future)

                if self.ir_post:
                    jtag_futures.append(self._interface.post(
                        Shift(BitString(-1, self.ir_post), read_tdo=False)))

                current_ir = bypass_val
                tdo_info.append((idx, data_future))

            elif isinstance(op, _TapShift):
                # IR shift if IR changed
                if op.ir_value is not None and op.ir_value != current_ir:
                    ir_data = (BitString(-1, self.ir_pre) +
                               BitString(op.ir_value, self.irlen) +
                               BitString(-1, self.ir_post))
                    jtag_futures.append(self._interface.post(CaptureIr()))
                    jtag_futures.append(self._interface.post(
                        Shift(ir_data, read_tdo=False)))
                    current_ir = op.ir_value

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
                        tdo_info.append((idx, data_future))

            elif isinstance(op, _TapRun):
                jtag_futures.append(self._interface.post(Run(op.cycles)))

        # Await all JTAG interface futures
        if jtag_futures:
            await asyncio.gather(*jtag_futures)

        # Resolve futures with TDO values
        for idx, data_future in tdo_info:
            shift_op = data_future.result()  # The Shift op, with .tdo populated
            batch[idx][1].set_result(shift_op.tdo)

        # Resolve remaining futures with None
        for op, future in batch:
            if not future.done():
                future.set_result(None)

    def __repr__(self):
        return f"<Tap {self._name} irlen={self.irlen}>"


# JTAG Interface

class JtagInterface(Node, FreqCapper):
    """JTAG master interface. Holds a single Chain as child.

    Supports multiple chains (e.g. behind a mux) in the future.
    """
    db = Db("Interface handler")

    def __init__(self, name="jtag"):
        Node.__init__(self, name)
        FreqCapper.__init__(self)

    async def child_spawn(self, name):
        return await self.db.acall(name, self)

    def option_set(self, opt):
        if opt.startswith("fmax="):
            from ..util.pretty import sci_parse
            self.freq_cap("user", sci_parse(opt[5:]))
            return
        super().option_set(opt)


# Chain

class OpenChain(Exception):
    """TDO line is stuck or disconnected."""
    pass

@JtagInterface.db.register("chain")
class Chain(Node):
    """JTAG Chain. Holds TAPs and manages chain geometry."""

    def __init__(self, interface, name="chain"):
        super().__init__(name)
        self._interface = interface
        self.total_irlen = 0
        self.total_drlen = 0

    async def start(self):
        jtag_iface = self.parent_of_class(JtagInterface)
        with jtag_iface.freq_capped("enumeration", 1e6):
            await self.discover()

    def children_changed(self):
        try:
            jtag_iface = self.parent_of_class(JtagInterface)
        except LookupError:
            return
        jtag_iface.freq_cap_min(self.children)

    async def _shift_discover(self, max_length=512, shift_in=None):
        """Probe a register's length by shifting a marker through.

        Shifts a 32-bit marker followed by zeros. The marker's
        position in TDO reveals the register length.

        Args:
            max_length: maximum register length to probe.
            shift_in: value to shift back into the register after
                measuring (e.g. -1 for all-ones). None to leave as-is.

        Returns the captured register contents (BitString).
        """
        marker = 0xc05a5a03
        tdi = BitString(marker, 32) + BitString(0, max_length + 4)
        shift = Shift(tdi, read_tdo=True)
        result = await self._interface.post(shift)
        tdo = result.tdo[:max_length + 32]

        if not int(tdo):
            raise OpenChain("TDO stuck low")

        if tdo == BitString(-1, len(tdo)):
            raise OpenChain("TDO stuck high")

        length = int(math.log2(int(tdo))) + 1
        register = tdo[:length - 32]
        rx_marker = int(tdo[length - 32:length])

        if rx_marker != marker:
            raise OpenChain("TDO changed but marker not received back")

        if shift_in is not None:
            back = BitString(shift_in, len(register))
            shift_back = Shift(back, read_tdo=False)
            await self._interface.post(shift_back)
            await self._interface.post(Run(1))

        return register

    async def discover(self):
        """Blind discovery of the JTAG chain.

        Reliably identifies IDCODEs and TAP count. Determines IR
        lengths from the captured IR pattern (JTAG spec: after
        Capture-IR, each TAP's IR starts with 01 in bits [1:0]).
        Cross-references with Tap.db for known IR lengths to
        disambiguate when needed.

        Unknown devices get a generic Tap with the discovered IR
        length.
        """
        # Reset and read DR — contains IDCODEs
        self.logger.trace("Discovering chain...")
        self._interface.post(Reset())
        self._interface.post(Run(1))
        self._interface.post(CaptureDr())
        reset_dr = await self._shift_discover()
        self.logger.trace("DR after reset: %d bits", len(reset_dr))

        # Capture IR (loads default IR), shift all-ones to load BYPASS
        self._interface.post(CaptureIr())
        captured_ir = await self._shift_discover(shift_in=-1)
        captured_ir_length = len(captured_ir)
        self.logger.trace("IR captured: %d bits", captured_ir_length)

        # Now all TAPs are in BYPASS (1-bit DR each).
        # Probe DR to count devices.
        self._interface.post(CaptureDr())
        bypass_dr = await self._shift_discover(max_length=captured_ir_length // 2)
        device_count = len(bypass_dr)
        self.logger.trace("BYPASS DR: %d devices", device_count)

        # Extract IDCODEs from reset DR
        idcodes = []
        pos = 0
        for _ in range(device_count):
            if pos >= len(reset_dr):
                idcodes.append(None)
            elif reset_dr[pos]:
                idcodes.append(int(reset_dr[pos:pos + 32]))
                pos += 32
            else:
                idcodes.append(None)
                pos += 1

        self.logger.note("IDCODEs: %s", ", ".join(
            f"0x{idc:08x}" if idc else "none" for idc in idcodes))

        # Determine IR lengths from captured IR pattern.
        # JTAG spec: after Capture-IR, each TAP loads a value with
        # bits [1:0] = 01. So we look for positions where bit pair
        # is 01 — these are potential IR boundaries.
        cutoffs = [i for i in range(captured_ir_length)
                   if int(captured_ir[i:i + 2]) == 1]
        cutoffs.append(captured_ir_length)

        # Build all possible IR length assignments
        segments = [b - a for a, b in zip(cutoffs, cutoffs[1:])]

        def ir_merge(prefix, parts, count_left):
            if len(parts) < count_left or count_left < 0:
                return []
            if len(parts) == count_left:
                return [prefix + parts]
            result = []
            for i in range(1, len(parts) + 1):
                result += ir_merge(
                    prefix + [sum(parts[:i])], parts[i:], count_left - 1)
            return result

        possibilities = ir_merge([], segments, device_count)

        # Filter by known IR lengths from Tap.db
        known_irlens = [self._irlen_for(idc) for idc in idcodes]
        possibilities = [
            p for p in possibilities
            if all(k is None or k == l for k, l in zip(known_irlens, p))]

        if len(possibilities) != 1:
            raise ValueError(
                f"Ambiguous IR lengths: {len(possibilities)} possibilities "
                f"for {device_count} devices (idcodes={idcodes!r})")

        ir_lengths = possibilities[0]
        self.logger.trace("IR lengths: %s", ir_lengths)

        # Create TAPs
        for idcode, irlen in zip(idcodes, ir_lengths):
            tap = self.tap_add(idcode, irlen)
            self.logger.note("TAP: %s (irlen=%d)", tap._name, irlen)

        # Return to Run-Test/Idle
        await self._interface.post(Run(1))

    @staticmethod
    def _irlen_for(idcode):
        """Look up known IR length for an IDCODE via Tap.db. Returns None if unknown."""
        if idcode is None:
            return None
        try:
            taps = Tap.db.get(idcode, allow_default=False)
        except NoMatch:
            return None
        irlens = {t.irlen for t in taps if t.irlen is not None}
        if len(irlens) == 1:
            return irlens.pop()
        return None

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

    async def child_spawn(self, name):
        """Delegate to single TAP when chain has exactly one device."""
        if len(self._children) == 1:
            return await self._children[0].child_summon(name)
        raise NoMatch("child", name)

    def __repr__(self):
        return f"<Chain {self._name} taps={len(self._children)}>"
