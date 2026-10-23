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

A boot image (BOOT.BIN) is loaded the way the BootROM and FSBL would
from flash: the FSBL runs until it reads the boot mode, by then the
clocks, MIO and DDR are initialized. The remaining partitions are then
placed from the host and CPU0 is handed to the first of them.
"""

from __future__ import annotations

from ...component.arm.coresight.v7_debug import V7Debug
from ...component.arm.dp import Dp
from ...component.arm.jtag_dp import JtagDpTap
from ...component.arm.mem_ap import MemAp
from ...component.arm.memory import BusRam
from ...component.xilinx.boot_image import Destination
from ...component.xilinx.zynq import Zynq
from ...db import NoMatch
from ...memory_map import MemoryMap
from ...part_id import PartId
from ..debuggable import CoreState, Debuggable, HaltCause
from ..fpga import FpgaLoadable
from ..loadable import Loadable, RamLoadable
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
            await core.points_clear()

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
            await self.cpu1_park(memory_map)
        await super().post_program(success=success, do_start=do_start,
                                   memory_map=memory_map)

    async def cpu1_park(self, memory_map):
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


class ZynqBootLoadable(Loadable):
    """Boots a BOOT.BIN image: plays the BootROM to run the FSBL,
    stops it once the hardware is initialized, then plays the FSBL to
    place the other partitions and hand over."""

    BOOT_MODE = 0xf800025c
    FSBL_TIMEOUT = 10.0
    DDR_START = 0x00100000

    SLCR_LOCK = 0xf8000004
    SLCR_UNLOCK = 0xf8000008
    SLCR_LOCK_KEY = 0x767b
    SLCR_UNLOCK_KEY = 0xdf0d
    FPGA_RST_CTRL = 0xf8000240
    LVL_SHFTR_EN = 0xf8000900
    LVL_SHFTR_ALL = 0xf

    SWDT_MODE = 0xf8005000
    SWDT_DISABLE = 0x00abc000

    L2_CLEAN_INVALIDATE_WAY = 0xf8f027fc
    L2_WAYS = 0xff

    def __init__(self, ps: ZynqPsLoadable, ahb: MemAp, pl: Zynq | None,
                 name="boot"):
        super().__init__(name)
        self.ps = ps
        self.ahb = ahb
        self.pl = pl

    async def write(self, source, *, do_erase=False, do_verify=False,
                    do_start=False, update=True, assume_clean=False):
        fsbl = source.child_lookup("fsbl")
        partitions = source.child_lookup("partition").children[1:]
        self.__check(partitions)
        cpu0 = self.ps.core
        async with self.event_emitter("program", target=self.parent.path,
                                      do_erase=do_erase, do_verify=do_verify):
            await self.ps.pre_program(do_erase=do_erase, assume_clean=assume_clean)
            for address, value in source.metadata.get("register_init", []):
                await self.__write32(cpu0, address, value)
            fsbl_data = await fsbl.read(0, fsbl.size)
            if self.ps.cpu1 is not None:
                await self.ps.cpu1_park(MemoryMap([(fsbl.metadata["load_address"], fsbl_data)]))
            await cpu0.mem_write(fsbl.metadata["load_address"], fsbl_data)
            await self.__fsbl_run(cpu0, fsbl.metadata["entry"])
            await self.__fsbl_stop(cpu0)
            for partition in partitions:
                await self.__place(partition)
            entries = [p.metadata["entry"] for p in partitions
                       if p.metadata["destination"] == Destination.PS]
            if do_start and entries:
                await cpu0.run_at(entries[0])

    def __check(self, partitions):
        """Refuse what the FSBL would refuse before it starts."""
        for p in partitions:
            name = p.metadata["name"] or p.name
            if p.metadata["destination"] == Destination.PL:
                if self.pl is None:
                    raise ValueError(f"{name}: no PL to configure")
            elif p.metadata["load_address"] < self.DDR_START:
                raise ValueError(
                    f"{name}: load address {p.metadata['load_address']:#x} "
                    "is below DDR, the FSBL would stop loading there; "
                    "set load= and startup= for it in the BIF")

    async def __fsbl_run(self, cpu0: CortexACore, entry: int):
        watch = await cpu0.watchpoint_add(self.BOOT_MODE, 4, 3)
        try:
            await cpu0.run_at(entry)
            if not await cpu0.wait_halted(self.FSBL_TIMEOUT):
                await cpu0.halt()
                pc = (await cpu0.reg_read(["pc"]))[cpu0.lookup_register("pc")]
                raise TimeoutError(
                    f"FSBL did not read the boot mode within "
                    f"{self.FSBL_TIMEOUT}s, CPU0 at {pc:#010x}")
            if await cpu0.halt_cause() != HaltCause.WATCHPOINT:
                raise RuntimeError(
                    f"FSBL stopped on {await cpu0.halt_cause()}")
        finally:
            await cpu0.watchpoint_remove(watch)

    async def __fsbl_stop(self, cpu0: CortexACore):
        """Take the FSBL's place at handoff: stop the watchdog it may
        have started, flush the caches it enabled to DDR and turn them
        off. The L2 controller is CPU private, out of the AHB-AP's
        reach."""
        await self.__write32(cpu0, self.SWDT_MODE, self.SWDT_DISABLE)
        await cpu0.dcache_clean_invalidate()
        await cpu0.caches_disable()
        await self.__write32(cpu0, self.L2_CLEAN_INVALIDATE_WAY, self.L2_WAYS)
        while int.from_bytes(await cpu0.mem_read(self.L2_CLEAN_INVALIDATE_WAY, 4), "little"):
            pass
        await self.__write32(cpu0, ZynqPsLoadable.L2_CONTROL, 0)

    async def __place(self, partition):
        name = partition.metadata["name"] or partition.name
        if partition.metadata["destination"] == Destination.PL:
            self.logger.info("Configuring PL from %s", name)
            await self.pl.load(partition.child_lookup("bitstream"))
            await self.__write32(self.ahb, self.SLCR_UNLOCK, self.SLCR_UNLOCK_KEY)
            await self.__write32(self.ahb, self.LVL_SHFTR_EN, self.LVL_SHFTR_ALL)
            await self.__write32(self.ahb, self.FPGA_RST_CTRL, 0)
            await self.__write32(self.ahb, self.SLCR_LOCK, self.SLCR_LOCK_KEY)
            return
        address = partition.metadata["load_address"]
        self.logger.info("Loading %s at %#010x", name, address)
        await self.ahb.mem_write(address, await partition.read(0, partition.size))

    @staticmethod
    async def __write32(bus, address: int, value: int):
        await bus.mem_write(address, value.to_bytes(4, "little"))


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
        self.child_add(ZynqBootLoadable(loadable, ahb, pl))


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
