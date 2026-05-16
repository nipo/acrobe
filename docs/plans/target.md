# PLAN_target.md — Target framework

Living document. Captures the model and the slicing for porting the
target framework from `crobe/` to `acrobe/`. Update as decisions land.

## Goal

A target is anything we can act on as a unit: program, debug,
provision. The target framework sits *on top* of the
adapter/protocol/component tree and exposes a second, disjoint tree
where each node represents a target and its operational facets.

The framework must accommodate, in one shape:

* SoCs with one or many CPUs (Cortex-M, Cortex-A, ARM9, RISC-V, …),
* FPGAs (SRAM-only, SRAM + on-die NVCM/UFM, etc.),
* SPI flash, I²C EEPROM, and similar passive memories,
* Targets exposed only for provisioning (key burn, fuses, OTP),
* Future targets reachable through gated/authenticated debug.

## Model

### One tree, no special grouping

Adapters, components, and Targets all live as Nodes under one root
(today `HwRoot`). Targets are flat children of the root, peers of
adapters. There is no `targets/` intermediate, no `Field` Node.

CPUs live **under the Target they belong to**, not under any
component subtree. The reason is the same as crobe's: a CPU may
reference many components (Cortex-M spans SCS, DWT, ITM, FPB,
Mem-AP, …) or, conversely, a debug fabric may expose many CPUs
through one component. There is no natural parent in the hardware
enumeration; the Target supplies one.

References across the tree are normal Python attributes on Targets
and their capability children — they do *not* affect parenthood.
The tree stays strictly a tree.

### Layout

```
HwRoot/
  proby-9/jtag/dap/ap0/...        ← adapters + components
  stm32-f103-xxx/                 ← Target, flat under root
    debug/                        ← Debuggable (Node)
      cores/
        cm3-0/                    ← Core (Node)
    loadable-main/                ← Loadable (Node); name freely chosen
    loadable-opt/                 ← second Loadable for option bytes
    puppet-cm3-0/                 ← Puppet bound to cm3-0
    auth/                         ← DebugAuth (no-op by default)
  machxo2-2000/                   ← another Target, flat under root
    loadable-sram/
    loadable-nvcm/
  qspi-flash/                     ← secondary Target, flat under root,
    loadable-main/                  references SpiFlash component sitting
                                    under stm32-f103-xxx/qspi-host/
```

Rules:

* No `Capability` base class. `Loadable`, `Debuggable`, `Puppet`,
  `DebugAuth` are each plain `Node` subclasses; they share no
  behaviour beyond Node, so a common base would be dead weight.
* Multi-instance "views" (`Loadable`, `Puppet`) are named at
  construction; single-instance ones (`Debuggable`, `DebugAuth`)
  use a fixed default name (`debug`, `auth`).
* A view discovers peers under its Target through
  `self._parent.children_of_class(Loadable)` etc. No back-references
  through hidden attributes. No `self.soc.puppet()` coupling.
* A view holds direct references to component-tree Nodes
  (e.g. `self.mem_ap = mem_ap_node`). These refs cross the tree
  but never alter parenthood.
* `Target.__init__` is the only place that wires views (see
  "Discovery" below).
* Lifetime: `Target.close` cascades through children in declared
  order (Puppet → Debuggable → DebugAuth → Loadables). Each view
  registers `on_shutdown` for its non-Node resources at construction.
* Cross-tree reference invalidation is **best-effort**: when an
  adapter goes away, the referenced component Nodes are removed; a
  Target's next operation surfaces the error from the dead reference.
  The Target itself stays parked under the root until the user runs
  re-discovery or removes it manually. Graduate to subscription-based
  invalidation only when hot-swap pain demands it.

### "Soft" components beneath a Target

A view may itself be (or expose) a normal component-tree node. The
canonical example is a puppet-driven QSPI master:

```
stm32-f103-xxx/
  puppet-cm3-0/
  qspi-host/        ← Node + protocol.spi.Interface
    W25Q64/         ← SpiFlash component, child of qspi-host
```

`QspiHost` here inherits both `Node` and `protocol.spi.Interface`. It
is *not* a `Loadable`/`Debuggable`/`Puppet` — it is a regular Node
that happens to live under a Target. Discovery walks it on the next
round and spawns a `qspi-flash` Target referencing the SpiFlash child
(parked flat under the root).

## Discovery

`Field` (the Node) is gone. The orchestration that used to live in
`Field.discover` becomes a stateful helper class — say
`acrobe/target/discovery.py:TargetDiscovery` — that walks the root,
matches `@Target.register(*ComponentTypes)` entries, instantiates
Targets, and parents them flat under the root.

The discovery loop is a **fixed-point**: a freshly spawned Target
may expose new component children (e.g. a puppet-driven QSPI master
with a SpiFlash beneath), and those are candidates for further
Target discovery. Each pass walks the root, attempts spawns for
components not yet considered, and repeats until no new Target is
spawned.

### Dedup state

A Target must not be spawned twice from the same component(s).
`TargetDiscovery` keeps two sets across rounds:

* `claimed_components`: every component Node that has already been
  consumed by a spawned Target. Provided by the Target via
  `Target.claimed_components()` returning the iterable of refs it
  was constructed with. A component in this set is skipped in
  subsequent rounds.
* `attempted_pairs`: `(component, target_class)` tuples already
  tried. Prevents retrying a registration that returned `NoMatch` /
  `NotImplementedError` / `DisabledEntry`.

Both survive across pass-triggered re-runs of `TargetDiscovery`
(i.e. they live on the helper instance held by the root, not just
on a single `discover()` call).

### Triggering discovery

`HwRoot` exposes `request_discovery()`. The first call within an
event-loop turn schedules an `asyncio.Task` that runs the
fixed-point loop; subsequent calls before the task fires are
coalesced (de-duplicated by design — same pattern as the Batcher's
"post then dispatch later").

Callers:

* CLI entry points run an explicit `await root.discover_targets()`
  once after enumeration; this is just `request_discovery()` +
  awaiting completion.
* Any Node that exposes new component children (a `qspi-host` once
  its bus is configured, an `aji-server` once it connected, etc.)
  calls `root.request_discovery()` to ask for another sweep. The
  caller doesn't need to know whether a sweep is already pending.

### Target classes wire their own children in `__init__`

No plugin-time view injection in V1; flexibility comes from each
Target class exposing its own `Db` for variant resolution.

Example pattern for STM32 (mirrors crobe's runtime DBID lookup, now
backed by `acrobe.db.Db`):

```python
@Target.register(SwDp, JtagDp, precedence=1000)
class Stm32Target(Target):
    db = Db("STM32 model")           # per-target-class subtype DB

    def __init__(self, dp):
        super().__init__("STM32")
        self._dp = dp
        debug = CortexMDebuggable(dp)
        self.child_add(debug)
        # variant-specific wiring via own db
        info = read_st_id_register(debug)
        self.db.call(info.dev_id, self, debug, info)

    def claimed_components(self):
        return (self._dp,)
```

The class-level `db` is the place to attach variant-specific flash
geometry, RAM sizing, gating, etc. The pattern matches crobe's
`Info.from_soc` but uses the project's `Db` registry.

## Views — V1 interfaces

API shape only. All methods are async (`Batcher`-friendly futures).
Each view is a plain `Node` subclass.

### Loadable

```python
class Loadable(Node):
    """Programs target memory from a MemoryMap or VFS node."""

    @property
    def regions(self): ...                       # list[Region]

    async def write(self, source, *, do_erase=False,
                    do_verify=False, do_start=False,
                    update=True, assume_clean=False): ...

    async def read(self, begin=0, end=None) -> MemoryMap: ...

    async def verify(self, source) -> bool: ...

    async def erase_all(self): ...

    # Hooks for subclasses
    async def _pre_program(self, *, do_erase, assume_clean): ...
    async def _post_program(self, *, success, do_start): ...
```

Existing `acrobe/target/__init__.py:Target.write` moves here and
becomes `Loadable.write`. The hooks (`_pre_program`, `_post_program`)
factor out crobe's `program_begin/program_end` so STM32-style
unlock/reload flows override hooks only.

### Region update

`Region` grows one method:

```python
class Region:
    async def update(self, offset, data): ...    # default: erase+write
                                                  # may raise NotUpdatable
```

* Default `Region.update` does `erase(offset, len(data))` then
  `write(offset, data)`.
* `Flash` subclasses may override (puppet CRC-skip, smart-card
  scripts, vendor protocol).
* OTP/fuse-like regions raise `NotUpdatable`; `Loadable.write` either
  surfaces it as an error or skips depending on policy.
* `Loadable.write` calls `region.update(...)` per page; there is no
  per-loop erase/write split in the framework loop. Regions that need
  bulk erase before writing pages override `update` (rare) or the
  framework batches per-region erase+write via a strategy below.

Pragmatically the loop is:

```python
async def write(self, source, ...):
    await self._pre_program(do_erase=do_erase, assume_clean=assume_clean)
    m = (await _coerce(source)).simplified()
    for region in sorted(self.regions):
        region_m = m.within(region.address, region.end)
        if not region_m: continue
        async for off, data in region.plan_update(region_m):
            await region.update(off, data)
    success = (not do_verify) or await self.verify(m)
    await self._post_program(success=success, do_start=do_start)
```

`Region.plan_update(MemoryMap)` defaults to "yield (offset, page) for
each `write_page_size` page after issuing `erase` for the affected
range" — i.e. crobe's behaviour, but expressed by the region. A
`PuppetFlash` overrides `plan_update` to skip pages whose target-side
CRC already matches.

### Debuggable

```python
class Debuggable(Node):
    """Run-control + memory access. CPU-family agnostic surface."""

    @property
    def cores(self): ...                        # list[Core]

    @property
    def memory_map(self): ...                   # list[Region] for GDB

    async def attach(self): ...
    async def detach(self): ...

    async def mem_read(self, addr, size): ...
    async def mem_write(self, addr, data): ...

    # Routes vFlashErase/Write from GDB to the target's Loadable.
    # Default: target.children_of_class(Loadable)[0]. Targets with
    # multiple Loadables override or expose policy.
    @property
    def flash_route(self): ...                  # Loadable | None

    async def monitor(self, cmd, args): ...     # extension hook
```

`mem_read`/`mem_write` are *on Debuggable*, not on a component-side
`Bus`. That decision is what lets Cortex-M (Mem-AP), ARM9 (instruction
stuffing), and RISC-V (DM system-bus / program-buffer) share a single
GDB-facing surface. Each Debuggable subclass implements memory access
in whatever way its CPU family allows.

### Core

```python
class Core(Node):
    """One thread of execution. Lives under Debuggable.children/cores/.
    Self-describes its GDB feature set."""

    class State(Enum): RUN, HALT, SLEEP, FAULT, LOCKUP, UNKNOWN

    class HaltCause(Enum):
        EXCEPTION, INSTRUCTION, BREAKPOINT, WATCHPOINT, DEBUGGER, UNKNOWN

    name: str
    gdb_feature_name: str            # 'org.gnu.gdb.arm.m-profile', etc.
    gdb_byteorder: Literal["little", "big"]
    registers: list[Register]        # number, name, width, type, group, gdb_visible

    async def state(self) -> State: ...
    async def halt_cause(self) -> HaltCause: ...

    async def halt(self): ...
    async def resume(self, *, allow_interrupts=True): ...
    async def step(self): ...
    async def reset(self, *, stop=True): ...

    async def reg_read(self, regs): ...          # dict[Register, int]
    async def reg_write(self, reg_values): ...   # dict[Register, int]

    async def breakpoint_add(self, addr, kind): ...
    async def breakpoint_remove(self, bp): ...
    async def breakpoint_list(self): ...
```

`Register` is reused from the existing `acrobe.bitfield` style or
introduced as `acrobe/target/register.py` (small dataclass with
number/name/width/type/group/gdb_visible). Core types `GPR`, `FLOAT`,
`DOUBLE`, `PC`, `LR`, `SP`, `SYSTEM`.

### Puppet

```python
class Puppet(Node):
    """Trampoline-based remote code exec on one Core.
    Lives under a Target so multiple AMP cores can each host a Puppet."""

    def __init__(self, name, core, ram, *,
                 pc_reg, sp_reg, arg_regs,
                 trampoline_code, stack_size=128, stack_direction=-1):
        super().__init__(name)
        self.core = core
        self.ram = ram
        self.allocator = Allocator(ram.address, ram.size)
        ...

    def allocate(self, size, align=1) -> Zone: ...
    def unallocate(self, zone): ...

    async def prepare(self, pc, *args): ...
    async def run(self): ...
    async def wait(self, timeout=None) -> int: ...
    async def call(self, pc, *args, timeout=None) -> int: ...

    def stub(self, code) -> "PuppetStub": ...
```

`AutoPuppet` (with `__getattr__` based stub dispatch and
`AutoPuppetBuffer`) ports as-is in shape; the magic is acceptable for
the convenience it gives stub authors.

`PuppetFlash` (the renamed `StubFlash`) gets constructed with a
`Puppet` reference at discovery time:

```python
flash = PuppetFlash("main", 0x08000000, size, page,
                    bus=mem_ap, puppet=puppet,
                    erase_stub=stm32f01["flash_erase"],
                    write_stub=stm32f01["flash_write"])
```

No `self.soc.puppet()` callback.

### DebugAuth

Skeleton only in V1.

```python
class DebugAuth(Node):
    async def authorize(self, debuggable): ...   # default: pass
```

Real implementations (nRF53 APPROTECT-CTRL-AP, STM32H5 DBGAUTH,
SAM-L11 DSU keyed unlock, RP2350 secure boot) arrive in Slice 4.
Debuggable's `attach()` calls into `target.child_of_class(DebugAuth)`
if present.

## GDB binding

Slice 2 deliverable. Standalone module
`acrobe/target/gdb/` (server + protocol). Generic over Debuggable +
Loadable.

* `target.xml`: assembled from `Core.gdb_feature_name` +
  `Core.registers` per core, byte-order taken from cores (all must
  agree or first wins with a warning).
* `qsThreadInfo`/`qfThreadInfo`: thread ID per core.
* `g/G/m/M/c/s/Z0/z0/Z1/z1`: routed to current core / Debuggable.
* `qXfer:memory-map:read`: from `Debuggable.memory_map` (flash + RAM
  with `blocksize` for flash regions).
* `vFlashErase/vFlashWrite/vFlashDone`: routed to
  `Debuggable.flash_route` (a Loadable).
* `qRcmd` (monitor): `Debuggable.monitor(cmd, args)`.

CLI: `acrobe gdb-server -r <target_path> [--port 3333]`.

## CLI evolutions (consequences)

These already-existing commands need rework once the target tree is
in place:

* `acrobe info cpu` — currently iterates SCS instances under a
  component path. Becomes: walk the target tree under `-r`, find
  `Debuggable.cores`, dispatch dump by Core type. Cortex-M Core's
  `dump_cpu` (verbose/summary) stays Cortex-M specific; ARM9/RV cores
  ship their own.
* `acrobe info enumerate` keeps showing the full tree under the
  root (adapters + Targets, since the tree is unified). A
  `acrobe info target` filter lists Targets only.
* `acrobe loadable info` and friends keep working; addressed paths
  resolve through the root like any other Node.
* New: `acrobe debug -r <target_path> ...` group for run-control
  (halt/resume/step/reg-read/reg-write) as direct CLI ops on Cores.

## Slicing

### Slice 1 — framework shape, no CPU

* `acrobe/target/` rewrite:
  * `target.py` — `Target` (Node subclass with `claimed_components()`
    hook), `@Target.register` decorator.
  * `discovery.py` — `TargetDiscovery` orchestrator (fixed-point
    loop, dedup state). Not a Node.
  * `loadable.py` — `Loadable(Node)` with `_pre_program` /
    `_post_program` hooks.
  * `debuggable.py` — interface skeleton (`Debuggable(Node)`,
    `Core(Node)`, `Register`).
  * `puppet.py` — interface skeleton; no usable implementation.
  * `debug_auth.py` — no-op `DebugAuth(Node)`.
  * `region.py` — move existing `Region`/`Ram`/`Flash`/`Eeprom`;
    add `update`, `plan_update`, `NotUpdatable`.
* `acrobe/node.py` or `acrobe/root.py`: add
  `HwRoot.request_discovery()` (coalescing asyncio.Task scheduler).
* `Loadable.write` rewritten on top of `plan_update`.
* Reshape `FpgaTarget` to a Target with one `Loadable` child.
* Reshape `SpiFlashTarget` similarly.
* Reshape `MachXO2` (or stub Target) with **two** `Loadable` children
  to validate the multi-Loadable case end-to-end (uses existing
  `component/lattice` if present, otherwise a synthetic test target).
* Validate recursive discovery with a synthetic case (no hw needed):
  a stub `puppet-host` Node that exposes a `protocol.spi.Interface`
  child with a fake SpiFlash beneath; a second discovery pass spawns
  a `SpiFlashTarget` parked under the root.
* CLI: `acrobe info target` lists Targets under the root. Drop or
  fold any leftover `Field`-specific commands.
* Tests: per-region `update`, multi-Loadable target, recursive
  discovery + dedup (no double-spawn), best-effort invalidation
  surfaces as IO error on dead refs, Loadable hooks fire in order.

Out of scope here: any Debuggable implementation, Puppet
implementation, GDB.

Estimated size: 600–900 lines + tests.

### Slice 2 — Cortex-M end-to-end

* Port `crobe/component/arm/cortex.py` and CoreSight bits used by
  debug (SCS halt control DHCSR/DEMCR, DCRSR/DCRDR for regs, FPB for
  HW breakpoints, DWT for watchpoints) to `acrobe/component/arm/`.
  Async on `Batcher`.
* `acrobe/target/arm/cortex_m.py`:
  - `CortexMDebuggable(Debuggable)` referencing a MemAp and the
    SCS/DWT/FPB component instances.
  - `CortexMCore(Core)` per logical core. Self-describes
    `gdb_feature_name='org.gnu.gdb.arm.m-profile'`, register list with
    Cortex-M numbering, byteorder `little`.
* `acrobe/target/arm/puppet.py`: `ArmMPuppet`, `AutoPuppet`,
  `PuppetStub`, `AutoPuppetBuffer`. Port `puppet_code/` blobs.
* `acrobe/target/arm/puppet_flash.py`: `PuppetFlash` region (renamed
  `StubFlash`).
* One chip end-to-end: STM32F1 (smallest, easy test hardware). Target
  class wires Debuggable + Loadable + Puppet + flash regions; uses its
  own `Db` for variant resolution (DEV_ID → flash/RAM geometry).
* `acrobe/target/gdb/`: server + protocol generic over Debuggable.
  Implements `target.xml`, memory-map, g/G/m/M/c/s/Z0/z0,
  vFlashErase/Write, qRcmd.
* CLI: `acrobe gdb-server`, `acrobe debug` group, evolve
  `info cpu` to walk Cores.
* Tests: STM32F1 program/verify on real hw (manual), GDB protocol
  unit tests against a fake Debuggable.

Estimated size: ~3000 lines (mostly Cortex/CoreSight port).

### Slice 3 — generality probe: ARM9 + RISC-V

* ARM9 Debuggable via EmbeddedICE on scan chain. `Mem_read`/`write`
  implemented by instruction stuffing on SCAN_N + INTEST. One concrete
  chip (e.g. AT91SAM9 or i.MX23).
* RISC-V: port a `DebugModule` component (DTM/DMI access, abstract
  commands, program buffer, system bus access). `RvDebuggable`,
  `RvCore`, `RvPuppet`. One concrete chip (ESP32-C3 or NEORV32).
* The success criterion: the GDB binding code from Slice 2 needs no
  changes. If it does, Slice 1 got the surface wrong; revisit.

### Slice 4 — refinements

* `DebugAuth` real implementations (nRF53, STM32H5, SAM-L11, RP2350
  secure boot).
* RTT discovery and reading: `Debuggable.find_rtt()` returns a
  component-tree Node.
* Semihosting trap.
* TrustZone-aware multi-domain Debuggable on Cortex-M33 / Cortex-A.
* RTOS thread awareness (FreeRTOS, Zephyr) as an optional Debuggable
  augmentation.

## Open questions, deferred

* **Per-region progress reporting.** Currently `Loadable.write` uses
  one progress bar per phase (Erasing / Writing). Once regions own
  their update strategies, each region should be able to report
  progress in its own units. Probably a `Region.update` taking a
  progress sink. Defer to Slice 1 implementation.
* **Hot adapter replacement.** Today targets cache component
  references; under best-effort invalidation a removed adapter
  leaves stale Targets parked under the root. Out of V1 scope to
  auto-prune; user can run re-discovery or remove manually.
* **Persistence of `Allocator` state across CLI invocations.** Not
  needed in V1 (each CLI run discovers fresh).
* **Cross-target dependencies.** E.g. an external flash hanging off a
  SoC's QSPI controller, where programming the flash requires the SoC
  Debuggable to host a puppet. Models well as a separate Target whose
  Loadable holds a `Puppet` ref into the SoC Target. No framework
  change needed; design check at Slice 2.
