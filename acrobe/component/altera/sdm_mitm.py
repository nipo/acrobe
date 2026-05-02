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

from ...node import Node
from ...db import NoMatch
from ...protocol.jtag import CaptureIr, CaptureDr, Shift, Run, Reset, JtagInterface
from .agilex5 import Agilex5
from .sdm_jtag import SdmJtag
from .sdm import Command, ErrorCode
from ...bitstring import *

SDM_CMD_IR = Agilex5.SDM_CMD.ir
SDM_RSP_IR = Agilex5.SDM_RSP.ir

def _log(msg):
    print(msg)

def _decode_sdm_header(word):
    """Decode a 32-bit SDM command/response header word.

    Returns (opcode_or_error, length, id_tag, upper) or None.
    Field [10:0] is opcode in commands, error code in responses.
    """
    code = word & 0x7FF
    if word & 0x800:  # bit 11 must be 0
        return None
    length = (word >> 12) & 0x7FF
    if word & (1 << 23):  # bit 23 must be 0
        return None
    id_tag = (word >> 24) & 0xF
    upper = (word >> 28) & 0xF
    return code, length, id_tag, upper

def _format_sdm_word(word, framing):
    """Format a 34-bit SDM word for logging."""
    header = _decode_sdm_header(word)
    if header:
        opcode, length, id_tag, reserved = header
        try:
            name = Command(opcode).name
        except:
            name = f"UNK_{opcode:#05x}"
        return (f"HDR id={id_tag} op={name}({opcode:#05x}) "
                f"len={length} rsv={reserved} frame={framing:#04b}")
    return f"DATA {word:#010x} frame={framing:#04b}"

@JtagInterface.db.register("sdm-mitm")
class SdmMitm(JtagInterface):
    """MITM wrapper around a JTAG interface for SDM protocol analysis.

    Forwards all JTAG operations to the underlying interface while
    capturing and decoding SDM FIFO traffic.
    """

    def __init__(self, interface, ir_width=10):
        super().__init__(name="sdm-mitm")
        self._interface = interface
        self._ir_width = ir_width
        self._current_ir = (1 << ir_width) - 1
        self._shifting = None
        self._tdi = BitString()
        self._tdo = BitString()
        self._is_first_cmd = True
        self._is_first_rsp = True
        
    async def flush_ops(self, batch):
        """Forward all operations, intercepting SDM traffic.

        Reads the captured TDO from the per-op future once the inner
        interface has resolved it (ops are immutable; results live on
        futures, not on the op objects).
        """

        ret = await self._interface.flush_ops(batch)

        for op, future in batch:
            if isinstance(op, CaptureIr):
                if self._shifting:
                    self._do_update()
                self._shifting = "ir"
                self._tdi = BitString()
                self._tdo = BitString()

            elif isinstance(op, CaptureDr):
                if self._shifting:
                    self._do_update()
                self._shifting = "dr"
                self._tdi = BitString()
                self._tdo = BitString()

            elif isinstance(op, Shift):
                self._tdi += op.tdi

                tdo_value = (future.result()
                             if future.done() and not future.exception()
                             else None)
                if op.read_tdo and tdo_value is not None:
                    self._tdo += tdo_value
                else:
                    self._tdo += BitString(0, len(op.tdi))

            elif isinstance(op, Run):
                if self._shifting:
                    self._do_update()
                if op.cycles > 1:
                    if self._current_ir == SDM_CMD_IR:
                        ir_name = "SDM <"
                    elif self._current_ir == SDM_RSP_IR:
                        ir_name = "SDM >"
                    elif self._current_ir == 0x281:
                        ir_name = "SDM *"
                    else:
                        ir_name = f"IR {self._current_ir:#05x}"
                    _log(f" {ir_name} running {op.cycles} cycles")

        return ret

    def _do_update(self):
        if self._shifting == "ir":
            self._current_ir = int(self._tdi) & 0x3ff

        elif self._shifting == "dr":
            if self._current_ir == SDM_CMD_IR:
                tdi = int(self._tdi)
                tdo = int(self._tdo)

                tdi_word, tdi_frame = self._split_cmd(tdi)
                tdo_word, tdo_frame = self._split_rsp(tdo)

                _log(f" SDM < {self._fmt_cmd_word(tdi_word, tdi_frame)}")
            elif self._current_ir == SDM_RSP_IR:
                tdi = int(self._tdi)
                tdo = int(self._tdo)

                tdi_word, tdi_frame = self._split_cmd(tdi)
                tdo_word, tdo_frame = self._split_rsp(tdo)

                _log(f" SDM > {self._fmt_rsp_word(tdo_word, tdo_frame)}")
            elif self._current_ir == 0x208:
                tdi = int(self._tdi)
                tdo = int(self._tdo)

                _log(f" CONFIG_STATUS {tdi:#12x} {tdo:#12x}")
            elif self._current_ir == 0x2:
                _log(f" CONFIG_DATA {len(self._tdi)} bits")
            else:
                _log(f"Ins {self._current_ir:#5x}: shift {len(self._tdi)} bits")

        self._shifting = None

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

    # CMD framing [33:32]: 00=idle, 01=data(more), 10=data(last), 11=flush
    _CMD_FRAME = {0: '-', 1: 'M', 2: 'L', 3: 'S'}
    # RSP framing [1:0]:  00=idle, 01=data(more), 10=???,         11=data(last)
    _RSP_FRAME = {0: '-', 1: 'M', 2: '?', 3: 'L'}

    def _fmt_cmd_word(self, word, frame):
        """Format a CMD-side 32-bit word with framing."""
        flag_s = SdmMitm._CMD_FRAME.get(frame, '?')
        s = f'{word:#010x} {flag_s}'

        if self._is_first_cmd:
            if frame:
                self._is_first_cmd = False
            hdr = _decode_sdm_header(word)
            if hdr:
                code, length, id_tag, upper = hdr
                try:
                    name = Command(code).name
                except:
                    name = f"op={code:#05x}"
                s += f' ({name} id={id_tag} len={length})'
                if upper:
                    s += f' [{upper:#x}]'

        if frame == 2 or frame == 3:
            self._is_first_cmd = True
        return s

    def _fmt_rsp_word(self, word, frame):
        """Format a RSP-side 32-bit word with framing.

        Framing interpretation TBD — may differ from CMD side.
        Show raw bits for now.
        """
        flag_s = SdmMitm._RSP_FRAME.get(frame, '?')
        s = f'{word:#010x} {flag_s}'
        if frame & 1:  # 01 or 11 = valid data
            if self._is_first_rsp:
                self._is_first_rsp = False

                hdr = _decode_sdm_header(word)
                if hdr:
                    code, length, id_tag, upper = hdr
                    try:
                        err_name = ErrorCode(code).name
                    except:
                        err_name = f"err={code:#05x}"
                    s += f' ({err_name} id={id_tag} len={length})'
                    if upper:
                        s += f' [{upper:#x}]'
        if frame == 3:
            self._is_first_rsp = True
        return s
