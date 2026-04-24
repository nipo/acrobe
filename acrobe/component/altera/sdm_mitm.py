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
from .agilex5 import Agilex5SdmCommand, Agilex5
from .sdm_jtag import SdmJtag
from .sdm import SdmErrorCode
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
            name = Agilex5SdmCommand(opcode).name
        except:
            name = f"UNK_{opcode:#05x}"
        return (f"HDR id={id_tag} op={name}({opcode:#05x}) "
                f"len={length} rsv={reserved} frame={framing:#04b}")
    return f"DATA {word:#010x} frame={framing:#04b}"

@JtagInterface.db.register("sdm-mitm")
class SdmMitm(JtagInterface, Batcher):
    """MITM wrapper around a JTAG interface for SDM protocol analysis.

    Forwards all JTAG operations to the underlying interface while
    capturing and decoding SDM FIFO traffic.
    """

    def __init__(self, interface, ir_width=10):
        JtagInterface.__init__(self, "sdm-mitm")
        Batcher.__init__(self)
        self._interface = interface
        self._ir_width = ir_width
        self._current_ir = None  # last IR value loaded
        self._shifting = None
        self._tdi = BitString()
        self._tdo = BitString()
        
    async def flush_ops(self, batch):
        """Forward all operations, intercepting SDM traffic."""

        ret = await self._interface.flush_ops(batch)

        for op, future in batch:
            if isinstance(op, CaptureIr):
                self._shifting = "ir"
                self._tdi = BitString()
                self._tdo = BitString()

            elif isinstance(op, CaptureDr):
                self._shifting = "dr"
                self._tdi = BitString()
                self._tdo = BitString()

            elif isinstance(op, Shift):
                self._tdi.append(op.tdi.data)

                if op.read_tdo and op.tdo is not None:
                    self._tdo += op.tdo.data
                else:
                    self._tdo += BitString(0, len(op.tdi))

            elif isinstance(op, Run):
                if self._shifting:
                    self._do_update()

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

                _log(f" SDM_CMD {self._fmt_cmd_word(tdi_word, tdi_frame)}")
            elif self._current_ir == SDM_RSP_IR:
                tdi = int(self._tdi)
                tdo = int(self._tdo)

                tdi_word, tdi_frame = self._split_cmd(tdi)
                tdo_word, tdo_frame = self._split_rsp(tdo)

                _log(f" SDM_RSP {self._fmt_rsp_word(tdo_word, tdo_frame)}")
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
    _CMD_FRAME = {0: ' ', 1: '>', 2: '.', 3: '!'}
    # RSP framing [1:0]:  00=idle, 01=data(more), 10=???,         11=data(last)
    _RSP_FRAME = {0: ' ', 1: '>', 2: '?', 3: '.'}

    @staticmethod
    def _fmt_cmd_word(word, frame):
        """Format a CMD-side 32-bit word with framing."""
        flag_s = SdmMitm._CMD_FRAME.get(frame, '?')
        s = f'{word:#010x} {flag_s}'
        hdr = _decode_sdm_header(word)
        if hdr:
            code, length, id_tag, upper = hdr
            try:
                name = Agilex5SdmCommand(code).name
            except:
                name = f"op={code:#05x}"
            s += f' ({name} id={id_tag} len={length})'
            if upper:
                s += f' [{upper:#x}]'
        return s

    @staticmethod
    def _fmt_rsp_word(word, frame):
        """Format a RSP-side 32-bit word with framing.

        Framing interpretation TBD — may differ from CMD side.
        Show raw bits for now.
        """
        flag_s = SdmMitm._RSP_FRAME.get(frame, '?')
        s = f'{word:#010x} {flag_s}'
        if frame & 1:  # 01 or 11 = valid data
            hdr = _decode_sdm_header(word)
            if hdr:
                code, length, id_tag, upper = hdr
                try:
                    err_name = SdmErrorCode(code).name
                except:
                    err_name = f"err={code:#05x}"
                s += f' ({err_name} id={id_tag} len={length})'
                if upper:
                    s += f' [{upper:#x}]'
        return s
