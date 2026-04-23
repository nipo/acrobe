"""MITM JTAG interface for SDM protocol analysis.

Wraps a real JTAG interface, forwarding all operations while
intercepting and decoding SDM command/response FIFO traffic.

Registered as a spawnable child of JtagInterface:
    acrobe stapl run file.jam -r adapter/jtag/sdm-mitm -a CONFIGURE

Or programmatically:
    leaf = await hw_root.child_summon("adapter", "jtag", "sdm-mitm")
    interface = leaf._interface  # SdmMitm batcher
"""

import sys

from ...component import Component
from ...db import NoMatch
from ...engine import Batcher
from ...protocol.jtag import CaptureIr, CaptureDr, Shift, Run, Reset, JtagInterface


def _log(msg):
    print(msg, file=sys.stderr)

# IR codes for SDM JTAG FIFOs
SDM_CMD_IR = 0x201   # Host → SDM command FIFO
SDM_RSP_IR = 0x202   # SDM → Host response FIFO

# Known SDM opcodes
SDM_OPCODES = {
    0x000: "NOOP",
    0x001: "SYNC",
    0x006: "CONFIG_STATUS",
    0x010: "GET_IDCODE",
    0x012: "GET_CHIPID",
    0x013: "GET_USERCODE",
    0x018: "GET_VOLTAGE",
    0x019: "GET_TEMPERATURE",
    0x032: "QSPI_OPEN",
    0x033: "QSPI_CLOSE",
    0x034: "QSPI_SET_CS",
    0x035: "QSPI_READ_DEVICE_REG",
    0x038: "QSPI_ERASE",
    0x039: "QSPI_WRITE",
    0x03A: "QSPI_READ",
    0x05B: "RSU_STATUS",
}


def _decode_sdm_header(word):
    """Decode a 32-bit SDM command/response header word.

    Returns (opcode, length, id_tag) or None if not a valid header.
    """
    opcode = word & 0x7FF
    if word & 0x800:  # bit 11 must be 0
        return None
    length = (word >> 12) & 0x7FF
    if word & (1 << 23):  # bit 23 must be 0
        return None
    id_tag = (word >> 24) & 0xF
    reserved = (word >> 28) & 0xF
    return opcode, length, id_tag, reserved


def _format_sdm_word(word, framing):
    """Format a 34-bit SDM word for logging."""
    header = _decode_sdm_header(word)
    if header:
        opcode, length, id_tag, reserved = header
        name = SDM_OPCODES.get(opcode, f"UNK_{opcode:#05x}")
        return (f"HDR id={id_tag} op={name}({opcode:#05x}) "
                f"len={length} rsv={reserved} frame={framing:#04b}")
    return f"DATA {word:#010x} frame={framing:#04b}"


class SdmMitm(Batcher):
    """MITM wrapper around a JTAG interface for SDM protocol analysis.

    Forwards all JTAG operations to the underlying interface while
    capturing and decoding SDM FIFO traffic.
    """

    def __init__(self, interface, ir_width=10):
        super().__init__()
        self._iface = interface
        self._ir_width = ir_width
        self._current_ir = None  # last IR value loaded
        self._in_ir = False      # True after CaptureIr
        self._in_dr = False      # True after CaptureDr
        self._shift_count = 0    # DR shifts since capture

    async def flush_ops(self, batch):
        """Forward all operations, intercepting SDM traffic."""
        for op, future in batch:
            if isinstance(op, CaptureIr):
                self._in_ir = True
                self._in_dr = False
                result = await self._iface.post(op)
                future.set_result(result)

            elif isinstance(op, CaptureDr):
                self._in_ir = False
                self._in_dr = True
                self._shift_count = 0
                result = await self._iface.post(op)
                future.set_result(result)

            elif isinstance(op, Shift):
                result = await self._iface.post(op)
                if self._in_ir:
                    # IR shift — extract the IR value to track state
                    self._decode_ir_shift(op)
                elif self._in_dr and self._current_ir in (SDM_CMD_IR, SDM_RSP_IR):
                    self._decode_sdm_shift(op)
                # After shift, we're in Exit1-IR/DR (then Update on Run)
                future.set_result(result)

            elif isinstance(op, Run):
                if self._in_ir or self._in_dr:
                    # Run after shift = Update-IR/DR → RTI
                    self._in_ir = False
                    self._in_dr = False
                result = await self._iface.post(op)
                future.set_result(result)

            else:
                result = await self._iface.post(op)
                future.set_result(result)

    def _decode_ir_shift(self, shift_op):
        """Extract IR value from a shift operation."""
        tdi_bytes = bytes(shift_op.tdi.data)
        ir_val = int.from_bytes(tdi_bytes, 'little') & ((1 << self._ir_width) - 1)
        self._current_ir = ir_val
        if ir_val in (SDM_CMD_IR, SDM_RSP_IR):
            label = "SDM_CMD" if ir_val == SDM_CMD_IR else "SDM_RSP"
            _log(f"IR → {label} ({ir_val:#05x})")
            # Also log TDO if captured
            if shift_op.read_tdo and shift_op.tdo is not None:
                tdo_bytes = bytes(shift_op.tdo.data)
                tdo_val = int.from_bytes(tdo_bytes, 'little') & ((1 << self._ir_width) - 1)
                _log(f"  IR TDO = {tdo_val:#05x}")

    def _decode_sdm_shift(self, shift_op):
        """Decode a DR shift on SDM_CMD or SDM_RSP instruction."""
        tdi_data = bytes(shift_op.tdi.data)
        tdo_data = bytes(shift_op.tdo.data) if shift_op.tdo is not None else None
        bit_count = len(shift_op.tdi)

        is_cmd = (self._current_ir == SDM_CMD_IR)
        direction = "CMD" if is_cmd else "RSP"

        if bit_count % 34 != 0:
            _log(f"  {direction} DR shift {bit_count} bits "
                        f"(not multiple of 34)")

        word_count = bit_count // 34

        for wi in range(word_count):
            bit_offset = wi * 34

            tdi_raw = self._extract_raw(tdi_data, bit_offset)
            if tdo_data is not None:
                tdo_raw = self._extract_raw(tdo_data, bit_offset)
            else:
                tdo_raw = None

            # CMD: TDI is host→SDM (framing at MSB), TDO is SDM→host (framing at LSB)
            # RSP: TDI is host→SDM (framing at MSB), TDO is SDM→host (framing at LSB)
            # TDI always uses CMD format, TDO always uses RSP format
            tdi_word, tdi_frame = self._split_cmd(tdi_raw)
            if tdo_raw is not None:
                tdo_word, tdo_frame = self._split_rsp(tdo_raw)
            else:
                tdo_word, tdo_frame = None, None

            self._log_sdm_word(direction, wi, word_count,
                               tdi_raw, tdi_word, tdi_frame,
                               tdo_raw, tdo_word, tdo_frame)

        self._shift_count += 1

    @staticmethod
    def _extract_raw(data, bit_offset, bit_count=34):
        """Extract raw bits from byte data at the given bit offset."""
        val = 0
        for i in range(bit_count):
            byte_idx = (bit_offset + i) >> 3
            bit_idx = (bit_offset + i) & 7
            if byte_idx < len(data) and data[byte_idx] & (1 << bit_idx):
                val |= 1 << i
        return val

    @staticmethod
    def _split_cmd(raw_34):
        """Split a CMD (host→SDM) 34-bit value.

        Framing at MSB [33:32] (last bits shifted in via TDI).
        Word at [31:0].
        """
        return raw_34 & 0xFFFFFFFF, (raw_34 >> 32) & 0x3

    @staticmethod
    def _split_rsp(raw_34):
        """Split a RSP (SDM→host) 34-bit value.

        [0]    = VALID (response word is meaningful)
        [1]    = ERROR (error condition)
        [33:2] = 32-bit SDM response word
        """
        return (raw_34 >> 2) & 0xFFFFFFFF, raw_34 & 0x3

    @staticmethod
    def _fmt_cmd_word(word, frame):
        """Format a CMD-side 32-bit word with framing."""
        flags = []
        if frame & 1: flags.append('F')  # FIRST
        if frame & 2: flags.append('L')  # LAST
        flag_s = ''.join(flags) if flags else '-'
        s = f'{word:#010x} {flag_s:>2}'
        hdr = _decode_sdm_header(word)
        if hdr:
            opcode, length, id_tag, reserved = hdr
            name = SDM_OPCODES.get(opcode, f"UNK_{opcode:#05x}")
            s += f' ({name} id={id_tag} len={length})'
        return s

    @staticmethod
    def _fmt_rsp_word(word, frame):
        """Format a RSP-side 32-bit word with VALID/ERROR flags."""
        valid = frame & 1
        error = (frame >> 1) & 1
        flags = []
        if valid: flags.append('V')
        if error: flags.append('E')
        flag_s = ''.join(flags) if flags else '-'
        s = f'{word:#010x} {flag_s:>2}'
        if valid:
            hdr = _decode_sdm_header(word)
            if hdr:
                opcode, length, id_tag, reserved = hdr
                name = SDM_OPCODES.get(opcode, f"UNK_{opcode:#05x}")
                s += f' ({name} id={id_tag} len={length})'
        return s

    @staticmethod
    def _log_sdm_word(direction, index, total,
                      tdi_raw, tdi_word, tdi_frame,
                      tdo_raw, tdo_word, tdo_frame):
        """Log one 34-bit SDM word."""
        parts = [f"  {direction}[{index}/{total}]"]
        # TDI is always CMD format (host→SDM), TDO is always RSP format (SDM→host)
        parts.append(f" TDI {SdmMitm._fmt_cmd_word(tdi_word, tdi_frame)}")
        if tdo_word is not None:
            parts.append(f" | TDO {SdmMitm._fmt_rsp_word(tdo_word, tdo_frame)}")
        _log(''.join(parts))


class SdmMitmComponent(Component):
    """Component wrapper for SdmMitm.

    Spawned as a child of JtagInterface, wraps the parent's raw
    JTAG batcher with SDM protocol logging. Downstream code
    accesses self._interface to get the MITM batcher.
    """

    def __init__(self, interface, name="sdm-mitm"):
        super().__init__(name)
        self._interface = SdmMitm(interface)

    async def start(self):
        pass


# Register as a spawnable child of JtagInterface
_orig_child_spawn = JtagInterface.__dict__.get("child_spawn")

async def _jtag_child_spawn(self, name):
    if name == "sdm-mitm":
        return SdmMitmComponent(self._interface)
    if _orig_child_spawn is not None:
        return await _orig_child_spawn(self, name)
    raise NoMatch("child", name)

JtagInterface.child_spawn = _jtag_child_spawn
