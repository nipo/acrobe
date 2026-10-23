"""Xilinx Zynq-7000 target.

The PS debug logic sits behind an ARM DAP TAP chained next to the
PL TAP. The target is anchored on the PL TAP and claims the
neighbouring DAP; when only the DAP is visible, it is found by the
root ROM table PartId instead and the PL is left out.

Memory regions follow the reset configuration of the address map:
192 KiB of OCM low, 64 KiB high, DDR above 1 MiB. DDR is only
usable once its controller is initialized, by an FSBL or an init
script.

Loading is destructive: CPU0 runs the image, CPU1 is sent back to
the BootROM wait loop, the state of both cores at attach time is
not preserved.
"""

from __future__ import annotations

from ...component.arm.coresight.v7_debug import V7Debug
from ...component.arm.dp import Dp
from ...component.arm.jtag_dp import JtagDpTap
from ...component.arm.mem_ap import MemAp
from ...component.arm.memory import BusRam
from ...component.xilinx.zynq import Zynq
from ...db import NoMatch
from ...part_id import PartId
from ..debuggable import CoreState, Debuggable
from ..fpga import FpgaLoadable
from ..loadable import RamLoadable
from ..memory import Memory
from ..region import Ram
from ..target import Target
from .cortex_a import CortexACore
from .soc import ArmSocTarget


class ZynqDebuggable(Debuggable):
    """Run-control over the PS cores. Memory goes through a halted
    core when there is one, so it sees caches and translation as
    the software does, else through the AHB-AP."""

    def __init__(self, cores, ahb: MemAp, name: str = "debug"):
        super().__init__(name)
        self.ahb = ahb
        for core in cores:
            self.child_add(core)

    async def attach(self):
        await super().attach()
        for core in self.cores:
            await core.halt()

    async def detach(self):
        for core in self.cores:
            if await core.state() == CoreState.HALT:
                await core.resume()

    async def __bus(self):
        for core in self.cores:
            if await core.state() == CoreState.HALT:
                return core
        return self.ahb

    async def mem_read(self, addr: int, size: int) -> bytes:
        return await (await self.__bus()).mem_read(addr, size)

    async def mem_write(self, addr: int, data):
        await (await self.__bus()).mem_write(addr, data)


class ZynqPsLoadable(RamLoadable):
    """Loads to OCM / DDR through CPU0 and starts CPU0 at the image
    entry point."""

    L2_CONTROL = 0xf8f02100

    # BootROM wait loop CPU1 sits in out of reset: wait for an event,
    # jump to the address in CPU1_START once it no longer points
    # back to the loop, with MMU and caches off.
    CPU1_LOOP = 0xffffff2c
    CPU1_START = 0xfffffff0
    CPU1_LOOP_CODE = (
        0xf57ff04f,  # dsb
        0xe320f002,  # wfe
        0xe3e0000f,  # mvn r0, #0xf
        0xe590e000,  # ldr lr, [r0]
        0xe37e00d4,  # cmn lr, #0xd4
        0x0afffff9,  # beq CPU1_LOOP
        0xee070f15,  # mcr p15, 0, r0, c7, c5, 0  @ ICIALLU
        0xee070fd5,  # mcr p15, 0, r0, c7, c5, 6  @ BPIALL
        0xee080f17,  # mcr p15, 0, r0, c8, c7, 0  @ TLBIALL
        0xe3a04000,  # mov r4, #0
        0xee014f10,  # mcr p15, 0, r4, c1, c0, 0  @ SCTLR
        0xe12fff1e,  # bx lr
    )

    def __init__(self, cpu0: CortexACore, cpu1: CortexACore | None,
                 name="ps"):
        super().__init__(cpu0, name)
        self.cpu1 = cpu1

    async def pre_program(self, *, do_erase, assume_clean):
        await super().pre_program(do_erase=do_erase, assume_clean=assume_clean)
        await self.core.caches_disable()
        # Accesses with the MMU off bypass L2; disable it so it can't
        # write stale lines back over the image later.
        await self.core.mem_write(self.L2_CONTROL, (0).to_bytes(4, "little"))
        if self.cpu1 is not None:
            await self.cpu1.caches_disable()

    async def post_program(self, *, success, do_start, memory_map):
        if do_start and success and self.cpu1 is not None:
            await self.__cpu1_park(memory_map)
        await super().post_program(success=success, do_start=do_start,
                                   memory_map=memory_map)

    async def __cpu1_park(self, memory_map):
        code_end = self.CPU1_LOOP + 4 * len(self.CPU1_LOOP_CODE)
        if memory_map.within(self.CPU1_LOOP, code_end).size \
                or memory_map.within(self.CPU1_START, self.CPU1_START + 4).size:
            self.logger.warning(
                "Image overlaps the CPU1 wait loop, CPU1 is left halted")
            return
        code = b"".join(w.to_bytes(4, "little") for w in self.CPU1_LOOP_CODE)
        await self.core.mem_write(self.CPU1_LOOP, code)
        await self.core.mem_write(self.CPU1_START,
                                  self.CPU1_LOOP.to_bytes(4, "little"))
        await self.cpu1.run_at(self.CPU1_LOOP)


class ZynqTarget(Target):
    OCM_LOW = (0x00000000, 0x00030000)
    DDR = (0x00100000, 0x3ff00000)
    AXI_GP0 = (0x40000000, 0x40000000)
    AXI_GP1 = (0x80000000, 0x40000000)
    IOP = (0xe0000000, 0x00300000)
    SMC = (0xe1000000, 0x05000000)
    SLCR = (0xf8000000, 0x00000c00)
    PS_SYSTEM = (0xf8001000, 0x0080f000)
    CPU_PRIVATE = (0xf8900000, 0x00603000)
    QSPI = (0xfc000000, 0x02000000)
    OCM_HIGH = (0xffff0000, 0x00010000)

    LOADABLE = ("ocm_low", "ddr", "ocm_high")

    def __init__(self, dp: Dp, pl: Zynq | None):
        super().__init__(pl.name if pl is not None else "zynq")
        aps = {ap.base >> 24: ap for ap in dp.children_of_class(MemAp)}
        if 0 not in aps:
            raise NoMatch("Zynq AHB-AP", dp.name)
        debugs = sorted(dp.children_of_class(V7Debug), key=lambda d: d.base)
        if not debugs:
            raise NoMatch("Zynq CPU debug", dp.name)
        self.claim(dp, *aps.values())
        ahb = aps[0]

        self.cores = [CortexACore(f"cpu{i}", d) for i, d in enumerate(debugs)]
        cpu0 = self.cores[0]

        regions = {
            "ocm_low": self.OCM_LOW, "ddr": self.DDR,
            "axi_gp0": self.AXI_GP0, "axi_gp1": self.AXI_GP1,
            "iop": self.IOP, "smc": self.SMC, "slcr": self.SLCR,
            "ps_system": self.PS_SYSTEM, "cpu_private": self.CPU_PRIVATE,
            "qspi": self.QSPI, "ocm_high": self.OCM_HIGH,
        }

        debug = ZynqDebuggable(self.cores, ahb)
        for name, (address, size) in regions.items():
            debug.memory_map.append(Ram(name, address, size))
        self.child_add(debug)

        for view, bus in (("memory", cpu0), ("ahb", ahb)):
            memory = Memory(bus, view)
            for name, (address, size) in regions.items():
                memory.child_add(BusRam(name, address, size, bus))
            self.child_add(memory)

        loadable = ZynqPsLoadable(cpu0, self.cores[1] if len(self.cores) > 1 else None)
        for name in self.LOADABLE:
            address, size = regions[name]
            loadable.child_add(BusRam(name, address, size, cpu0))
        self.child_add(loadable)

        if pl is not None:
            self.child_add(FpgaLoadable(pl, "pl"))


@Target.register(Zynq, precedence=900)
def zynq_probe(pl: Zynq):
    """Pairs the PL TAP with the DAP TAP next to it on the chain."""
    chain = pl.parent.children
    index = chain.index(pl)
    for neighbour in chain[index - 1:index] + chain[index + 1:index + 2]:
        if isinstance(neighbour, JtagDpTap):
            dps = neighbour.children_of_class(Dp)
            if dps:
                target = ZynqTarget(dps[0], pl)
                target.claim(neighbour)
                return target
    raise NoMatch("Zynq DAP", pl.name)


@ArmSocTarget.db.register(PartId.from_idcode(0x003b2313),
                          PartId.from_idcode(0x003b3313))
def zynq_ps_factory(dp: Dp):
    return ZynqTarget(dp, None)
