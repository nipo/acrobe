from ...protocol.jtag import Tap, Chain, Dr, Instruction
from ...part_id import PartId
from ..arm.jtag_dp import JtagDpTap
from .series7 import Series7

# IDCODE -> part name, from the device table of U-Boot's
# drivers/soc/soc_xilinx_zynqmp.c.
_PARTS = {
    0x04688093: "ZU1",
    0x04689093: "ZU1-LR",
    0x0468a093: "ZU1-LR",
    0x04711093: "ZU2",
    0x04710093: "ZU3",
    0x04718093: "ZU3-TEG",
    0x04721093: "ZU4",
    0x04720093: "ZU5",
    0x04739093: "ZU6",
    0x04730093: "ZU7",
    0x04738093: "ZU9",
    0x04740093: "ZU11",
    0x04741093: "ZU11-SE",
    0x04750093: "ZU15",
    0x04759093: "ZU17",
    0x04758093: "ZU19",
    0x0475c093: "ZU19-SE",
    0x047e1093: "ZU21DR",
    0x047e3093: "ZU23DR",
    0x047e5093: "ZU25DR",
    0x047e4093: "ZU27DR",
    0x047e0093: "ZU28DR",
    0x047e2093: "ZU29DR",
    0x047e6093: "ZU39DR",
    0x047fd093: "ZU43DR",
    0x047f8093: "ZU46DR",
    0x047ff093: "ZU47DR",
    0x047fa093: "ZU47DR-SE",
    0x047fb093: "ZU48DR",
    0x047fe093: "ZU49DR",
    0x047f9093: "ZU58DR",
    0x047fc093: "ZU59DR",
    0x046d0093: "ZU67DR",
    0x046d7093: "ZU67DR-SE",
    0x046d1093: "ZU65DR",
    0x046d2093: "ZU55DR",
    0x046d3093: "ZU57DR",
    0x046d4093: "ZU42DR",
    0x046d5093: "ZU63DR",
    0x046d6093: "ZU64DR",
    0x04712093: "ZU24",
    0x04724093: "ZU26",
}


# The PS TAP occupies the 6 upper IR bits, the PL TAP the 6 lower
# ones. PL instructions are the PL opcode with the PS half set to
# 0x24.
def _pl(opcode):
    return 0x900 | opcode


@Tap.db.register(*map(PartId.from_idcode, _PARTS.keys()))
class ZynqMp(Series7):
    """Zynq UltraScale+ PS and PL TAPs, concatenated into a single
    12-bit IR device.

    The PL TAP behaves as a Series-7 one: same configuration packets,
    same IR capture status bits, and the same 6-bit opcodes once
    routed through the PS half of the IR.

    The ARM DAP slot sits immediately TDO-side of this TAP, with a
    4-bit IR. Depending on JTAG_CTRL, it is either the actual ARM
    JTAG-DP or a placeholder TAP that never answers with an IDCODE.
    Either way, it is claimed as gated by this TAP.

    On start, JTAG_CTRL is set to connect both the PL TAP and the DAP
    unless the `attach_dap=false` option is given, in which case it
    is left untouched.
    """

    irlen = 12
    # DS925
    max_freq = 25e6

    USER_IR = [_pl(op) for op in Series7.USER_IR]

    JTAG_CTRL_DR = Dr(32)

    IDCODE = Instruction(0x249, "DEVICE_ID")
    IR_JTAG_CTRL = Instruction(0x83f, "JTAG_CTRL_DR")
    IR_USERCODE = Instruction(_pl(0x08), "USER_CODE")
    IR_ISC_NOP = Instruction(_pl(0x14), None)
    IR_JPROGRAM = Instruction(_pl(0x0b), "ISC_DEFAULT")
    IR_JSTART = Instruction(_pl(0x0c), None)
    IR_CFG_IN = Instruction(_pl(0x05), "CONFIG")
    IR_CFG_OUT = Instruction(_pl(0x04), "CONFIG")

    DAP_IRLEN = 4
    # Without the PL bit, the PL half of the IR captures neither INIT
    # nor DONE.
    JTAG_CTRL_PL = 0x1
    JTAG_CTRL_DAP = 0x2

    def __init__(self, idcode, **kw):
        name = "ZynqMP-" + _PARTS.get(
            int(PartId.from_idcode(idcode).drop_revision()),
            f"0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
        self.attach_dap = True

    def option_set(self, key, value):
        if key == "attach_dap":
            if value not in ("true", "false"):
                raise ValueError(
                    f"{self.name}: attach_dap expects true or false,"
                    f" got {value!r}")
            self.attach_dap = value == "true"

    async def start(self):
        if self.attach_dap:
            await self.dap_attach()
        await super().start()

    def dap_slot(self):
        """TAP currently in the DAP slot, or None."""
        chain = self.parent_of_class(Chain)
        expected_ir_pre = chain.context(self).ir_pre - self.DAP_IRLEN
        for tap in chain.children:
            if tap is self:
                continue
            ctx = chain.context(tap)
            if not ctx.enabled or ctx.ir_pre != expected_ir_pre:
                continue
            if tap.irlen != self.DAP_IRLEN:
                continue
            if tap.idcode is not None and not isinstance(tap, JtagDpTap):
                continue
            return tap
        return None

    async def dap_attach(self):
        """Connect the PL TAP and the ARM DAP through JTAG_CTRL.
        JTAG_CTRL survives TAP resets, but changes only take effect
        on the next one, so the chain is refreshed if it changed."""
        value = self.JTAG_CTRL_PL | self.JTAG_CTRL_DAP
        previous = await self.IR_JTAG_CTRL(value, read_tdo=True)
        await self.run(100)
        if int(previous) & value == value:
            return
        placeholder = self.dap_slot()
        if isinstance(placeholder, JtagDpTap):
            placeholder = None
        chain = self.parent_of_class(Chain)
        await chain.tlr_and_refresh()
        if not isinstance(self.dap_slot(), JtagDpTap):
            self.logger.warning(
                "ARM DAP did not join the chain after JTAG_CTRL write")
            return
        if placeholder is not None:
            await chain.tap_remove(placeholder)

    async def post_tlr(self):
        tap = self.dap_slot()
        if tap is None:
            return
        chain = self.parent_of_class(Chain)
        if chain.context(tap).controller is not None:
            return
        self.logger.note("Claiming %s as DAP slot", tap.name)
        chain.tap_set_controller(tap, self)
