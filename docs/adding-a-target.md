# Adding a new MCU target

This guide walks through bringing up support for a new ARM Cortex-M
based MCU in acrobe — STM32, NXP, SiLabs, TI, Microchip, etc. By
"target" we mean the operational view: detection, run-control,
flash programming, and the glue that ties them into the GDB binding
and the CLI.

The example you'll see most often is `acrobe/target/arm/nrf52.py`
and its sibling `acrobe/component/nordic/ctrl_ap.py` — they cover
every pattern this document discusses.

## What the target framework gives you for free

Once a Target is registered for your chip, the framework handles:

- **Discovery** — when CLI commands resolve a `-r <path>` to a DP /
  AP, the framework runs target discovery and parents matching
  Targets at the root.
- **Run-control** — `acrobe debug -r ... halt|resume|step|reset|
  reg read|reg write` works through the `CortexMDebuggable` view.
- **GDB Remote Serial Protocol** — `acrobe gdb-server -r ...`
  serves a fully functional GDB endpoint. `target.xml` is built
  from the Core's register set; memory map from the Loadable's
  regions; `vFlashErase/Write/Done` routes into `Loadable.write`.
- **CLI flashing** — `acrobe chip -r ... program <file>` walks any
  file the VFS understands (`.hex`, `.elf`, `.bin`, vendor `.rbf` /
  `.sof`, …) into a MemoryMap and dispatches per-region.
- **Verify / read-back / erase-all** — same `chip` subcommand.
- **`info target` / `info target -v`** — sanity-check what the
  framework sees, what regions exist, which CPUs are wired.

What you'll typically have to write yourself is the Target class,
one or more Flash region classes, and (if the chip has them) any
vendor-specific Access Ports.

## The tree shape

```
HwRoot
├── adapter-foo / swd / dap          ← component tree (adapters + APs + APs' children)
└── MyMcu                            ← Target (flat under root)
    ├── debug                        ← Debuggable (single per Target)
    │   └── cores
    │       └── core / core0 / …     ← Core (per physical CPU)
    ├── main                         ← Loadable (programming view)
    │   ├── flash                    ← Region (Flash / Ram / Eeprom)
    │   ├── uicr / opt / …           ← more Regions
    │   └── …
    ├── puppet-coreX                 ← optional; Slice 2+ chips that need
    │                                   target-side code for flash
    └── auth                         ← optional; DebugAuth for keyed unlock
```

Key facts:

1. **Targets live flat under the root.** Not nested under the
   adapter they were discovered from. The framework parents them
   that way so multi-adapter targets work cleanly.
2. **Capability children are plain Nodes.** No `Capability` base
   class. `Loadable`, `Debuggable`, `Puppet`, `DebugAuth`,
   `Core` are independent Node subclasses with their own contracts.
3. **Cross-tree references are plain attributes.** Your Target /
   Loadable / Core hold direct references to Mem-AP, CTRL-AP, SCS,
   FPB, DWT, etc. The component tree owns those Nodes; you borrow
   them.
4. **Best-effort invalidation.** If the adapter goes away, the
   next op against the stale reference surfaces an `IOError`.
   No subscription / cleanup machinery in V1.

## Discovery + registration

Discovery is triggered by `HwRoot.discover_targets()` (called by
the CLI). It walks every Node under the root and tries every
registered explorer against every Node. An explorer is a function
decorated with `@Target.register(*ComponentTypes, precedence=N)`.

`precedence` is **ascending** — lower numbers run first. The
generic Cortex-M explorer is at `precedence=10000`, so any
chip-specific explorer should pick a smaller number (the nRF52
explorer uses `500`).

```python
from acrobe.component.arm.dp import Dp, DpAccessFailure
from acrobe.component.arm.mem_ap import MemAp
from acrobe.db import NoMatch
from acrobe.target import Target

@Target.register(Dp, precedence=500)
async def mychip_probe(dp):
    for ap in dp.children_of_class(MemAp):
        try:
            # Read whatever ID register the chip exposes.
            part = await ap.read32(SOC_ID_REG)
        except DpAccessFailure:
            continue
        if part not in KNOWN_PARTS:
            continue
        return await _build_target(dp, ap, part)
    # Tell the framework we don't claim this DP; the generic
    # Cortex-M target catches it next.
    raise NoMatch("mychip_probe", "no MyChip behind DP")
```

Rules:

- **Probes can be async.** The framework awaits the returned
  coroutine. Use this to query identification registers on the
  target.
- **Raise `NoMatch` (or `NotImplementedError`) to decline.**
  Anything else propagates and aborts discovery.
- **Don't re-instantiate** what's already in the component tree.
  If a sibling Mem-AP already enumerated a ROM Table with an SCS,
  consume those existing instances via `children_of_class`.
- **Call `target.claim(*components)`** for every component node
  your Target references. `TargetDiscovery` won't spawn a second
  Target from those components in subsequent passes.

The Target itself is a plain `Node` subclass — usually inheriting
`CortexMTarget` which is empty by design (subclassing tags the
target as Cortex-M-shaped without adding behaviour):

```python
class MyChipTarget(CortexMTarget):
    """My Chip Family — Cortex-M4 with on-chip flash."""
```

## The minimum viable Cortex-M target

For a chip whose flash programming is straightforward MMIO (writes
to a controller register block, polled status), you need three
pieces:

1. **A Flash region.** Subclass `Flash`, implement
   `read` / `write` / `erase`. The framework's default
   `plan_update` will handle erase-page alignment, page paging,
   and dedup automatically.
2. **A Loadable.** Often the stock `Loadable` is enough; subclass
   only if you need custom `pre_program` / `post_program` /
   `erase_all` (see "Pitfalls" below).
3. **The probe function** above.

Skeleton, modeled after nRF52:

```python
# acrobe/target/arm/mychip.py

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp, DpAccessFailure
from ...component.arm.mem_ap import MemAp
from ...db import NoMatch
from ..loadable import Loadable
from ..region import Flash
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


class MyChipFlash(Flash):
    """Flash region driven by the chip's on-die flash controller."""

    def __init__(self, name, address, size, mem_ap, *, page_size):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.mem_ap = mem_ap

    async def read(self, offset, size):
        return await self.mem_ap.mem_read(self.address + offset, size)

    async def erase(self, offset, size):
        # Implement vendor-specific erase
        ...

    async def write(self, offset, data):
        # Implement vendor-specific write
        ...


class MyChipTarget(CortexMTarget):
    pass


@Target.register(Dp, precedence=500)
async def mychip_probe(dp):
    for ap in dp.children_of_class(MemAp):
        try:
            part = await ap.read32(SOC_ID_REG)
        except DpAccessFailure:
            continue
        if part not in MYCHIP_PARTS:
            continue
        return await _build(dp, ap, part)
    raise NoMatch("mychip_probe", "no MyChip behind DP")


async def _build(dp, ap, part):
    page_size = await ap.read32(FLASH_PAGE_SIZE_REG)
    flash_size = await ap.read32(FLASH_SIZE_REG)
    rom_tables = ap.children_of_class(RomTable)
    rt = next((r for r in rom_tables if r.children_of_class(Scs)), None)
    if rt is None:
        raise NoMatch("mychip_probe", "no SCS under MemAp")

    target = MyChipTarget(MYCHIP_PARTS[part])
    target.claim(dp, ap, rt)
    target.child_add(CortexMDebuggable.from_romtable(rt, ap))

    loadable = Loadable("main")
    loadable.child_add(
        MyChipFlash("flash", 0, flash_size, ap, page_size=page_size))
    target.child_add(loadable)
    return target
```

Then wire the module into the package init:

```python
# acrobe/target/__init__.py
from .arm import mychip as _mychip  # noqa: F401,E402
```

That's enough for `info target`, `info cpu`, `debug`, `chip`,
and `gdb-server` to work end-to-end against the chip.

## Flash region — what the framework expects

`Region.plan_update(region_map)` is the contract. Default
implementation in `Flash` does:

1. Compute the set of erase pages touched by the input chunks
   (aligned to `erase_page_sizes[0]`, deduped).
2. Issue one `erase(page_offset, erase_page_size)` per page —
   unless `is_blank` is True.
3. Page-align input chunks to `write_page_size`, fill gaps with
   `erased_value` (0xff by default).
4. Yield `(offset, page_bytes)` for each `write_page_size` slice.

`Loadable.write` then loops: `await region.write(offset, data)`
per yielded chunk.

That means:

- **Your `write` is per-page, not per-blob.** Don't loop inside
  `write` to handle multi-page payloads — that's `plan_update`'s
  job. Override `plan_update` only if your hardware needs an
  unusual sequence (e.g. write enable bit set once across many
  pages).
- **Your `erase` is per-region-aligned-page**, again because
  `plan_update` calls it with aligned offset+size.
- **`is_blank` is your hint** — set it to True after a full-region
  erase so the next `plan_update` skips per-page erase. nRF52's
  `NvmcFlash.erase` does this when `offset==0 && size==self.size`.

For flash that can only be written once between erases (the
common case), no extra work — the default flow does the right
thing.

For "fuse" / OTP regions, override `update` to raise
`NotUpdatable`. The Loadable surfaces this as a programming
error rather than corrupting state.

## Vendor-specific Access Ports

ARM CoreSight defines AHB-AP / APB-AP / JTAG-AP; vendors add their
own. Nordic's CTRL-AP (IDR `0x02880000`) bypasses APPROTECT to
mass-erase a locked chip. STM32 has equivalents. Microchip has
DSU. TI has ICEpick (already in `acrobe/component/ti/icepick.py`).

Register your AP against `Ap.db` keyed on its IDR. The framework's
DP enumeration walks `ApIdr` → matching subclass automatically:

```python
# acrobe/component/myvendor/ctrl_ap.py

from ..arm.ap import Ap

@Ap.db.register(0x02880000)
class MyVendorCtrlAp(Ap):
    RESET_OFFSET    = 0x00
    ERASEALL_OFFSET = 0x04
    STATUS_OFFSET   = 0x08

    def __init__(self, dp, base, idr=0, name=None):
        if name is None:
            name = f"vendor-ctrl-ap@{base >> 24}"
        super().__init__(dp, base, idr=idr, name=name)

    async def erase_all(self):
        await self.reg_write(self.ERASEALL_OFFSET, 1)
        # Poll the status bit until done…
```

The IDR match equality function masks REVISION and VARIANT, so
one registration covers every silicon roll of the AP. Import the
module from your target file so the registration fires before
discovery runs.

Your Target probe then looks for the AP among the DP's children:

```python
ctrl_aps = dp.children_of_class(MyVendorCtrlAp)
ctrl_ap = ctrl_aps[0] if ctrl_aps else None
target.claim(*([ctrl_ap] if ctrl_ap else []))
```

Pass it to a custom Loadable as needed.

## Custom Loadable: when to subclass

The stock `Loadable.write` is enough when your regions handle
erase + write themselves. Subclass when you need:

- **CPU halt around flash ops.** Most MCUs need this — a running
  CPU contending with flash bus access produces unreliable
  programming. Override `pre_program` / `post_program`:

  ```python
  async def pre_program(self, *, do_erase, assume_clean):
      core = self.__core()
      if core is not None:
          await core.halt()
      await super().pre_program(do_erase=do_erase,
                                assume_clean=assume_clean)

  async def post_program(self, *, success, do_start):
      if do_start and success:
          await self.__core().reset(stop=False)

  def __core(self):
      from acrobe.target.debuggable import Debuggable
      target = self._parent
      if target is None:
          return None
      debuggables = target.children_of_class(Debuggable)
      if not debuggables or not debuggables[0].cores:
          return None
      return debuggables[0].cores[0]
  ```

- **Mass-erase via vendor AP.** Override `erase_all` to use the
  CTRL-AP / DSU / similar path when available; fall back to
  per-page region erase otherwise. nRF52's `Nrf52Loadable.erase_all`
  is the canonical example.
- **Post-erase target stitching.** Some chips need a sequence of
  config-byte reload / option-byte unlock / etc. after programming.
  STM32 F1 option-byte handling went here in crobe; the hooks are
  designed for it.

Don't subclass to handle multi-page programming — that's `Region`'s
job.

## When you actually need a Puppet

A Puppet is on-target code that the host runs in trampoline mode
to drive flash hardware faster than MMIO-from-host could.

You **don't** need a Puppet when:

- The flash controller is memory-mapped (nRF52 NVMC, RP2040 boot
  ROM via the watchdog, …).
- Programming throughput is acceptable through the Mem-AP. nRF52
  at ~25s for 1 MiB is "acceptable" for most use cases.

You **do** need a Puppet when:

- The flash interface is a complex protocol that's prohibitively
  slow to drive one register at a time over SWD (STM32 H7 octo-SPI,
  any QSPI-flash-behind-an-IP-block, smart-card-style commands).
- The chip's flash requires precise timing the host can't
  guarantee.

Puppet support is still scaffolded — `acrobe/target/puppet.py` is
the interface skeleton; concrete `ArmMPuppet` / `AutoPuppet` /
`PuppetStub` haven't landed in V1. When you need them, follow
crobe's `target/soc/arm_based/puppet_code.py` pattern: small
hand-rolled ARM stubs compiled to bytes, loaded into target RAM
at runtime, called via the Cortex-M debug registers.

## Pitfalls

These are the common ways the first port goes wrong.

### CPU contention

Symptom: flash programming succeeds after `acrobe chip program
--erase` but fails on a chip with user code already running.

Cause: NVMC / FPEC / similar contends with the CPU for the flash
bus. The CPU must be halted (or held in reset) before flash ops.

Fix: see "Custom Loadable" above — halt the core in `pre_program`.

### Alignment

Symptom: `ValueError: erase must be page-aligned` deep inside
`plan_update`.

Cause: framework default `Flash.plan_update` already aligns erase
offsets to `erase_page_sizes[0]` — but if your `erase` method
checks alignment, make sure your `erase_page_sizes` list is
correct. For nRF52 with a single 4 KiB erase page, the entry is
`[4096]`. If the chip supports multiple erase sizes (e.g. STM32 F4
has 16 KiB / 64 KiB / 128 KiB sectors), list them ascending; the
framework picks index 0.

### APPROTECT / locked debug

Symptom: probe's first AP read raises `DpAccessFailure` and
discovery declines. The user gets the generic Cortex-M target (or
nothing).

Cause: the chip's lock bit blocks Mem-AP transactions until cleared.

Fix:

- For nRF52: register `CtrlAp` (already in
  `acrobe/component/nordic/ctrl_ap.py`); call
  `ctrl_ap.is_protected()` early in the probe and emit a clear
  warning suggesting `chip program --erase` (mass-erase clears
  APPROTECT).
- For STM32: similar — different AP / different sequence per family.
- For most TrustZone-enabled chips: Slice 4 `DebugAuth` capability
  will handle keyed challenges. Until then, document the manual
  unlock procedure.

### Reading FICR / system registers before debug is enabled

Symptom: works on some chips, hangs on others.

Cause: some chips gate parts of the address map behind `DEBUGEN`
in DHCSR. Acrobe's SCS sets DEBUGEN as part of
`CortexMDebuggable.attach()`, but discovery runs **before** any
explicit attach. The Mem-AP itself comes up enabled enough to
read CPUID, but vendor-specific registers (Nordic's FICR is fine;
others may not be) can require DEBUGEN.

Fix: if probe reads spuriously fail, call
`await scs.enable_debug()` before reading vendor registers. Be
ready to detect this — the second discovery pass after CLI
runs `Debuggable.attach()` won't help because the same Mem-AP
already declined.

### Confusing "this works" with "this works on a programmed chip"

Symptom: probe and discovery work fine on a freshly-erased chip;
fail or behave weirdly on a chip with active firmware.

Cause: the CPU is running, touching peripherals, interrupts
firing, watchdogs munching, etc. Reading a peripheral register
out from under a running CPU can return whatever value it was
mid-write.

Fix: do everything that matters (FICR, ID register reads, lock
status, etc.) on either a halted CPU or registers that are
genuinely safe to read concurrently. FICR is read-only and stable
on Nordic, so reads are safe — but verify the chip's manual for
your peripheral.

### GDB silently can't read peripherals

Symptom: in `arm-none-eabi-gdb` connected via `gdb-server`,
`x/w 0x40000000` (or any peripheral address) reports `Cannot
access memory at address 0x4000_0000` — and there's **no
matching `m` packet in the acrobe protocol log**.

Cause: GDB applies strict memory-map clamping whenever the
server advertises a memory-map (which we do as soon as any
flash region exists). Addresses outside every declared region
get rejected client-side before any packet is sent.

Fix: add the peripheral / RAM / FICR ranges to
`Debuggable.memory_map` in your probe — see "Declaring the
memory map" under "GDB" above. The Cortex-M PPB
(`0xE0000000`-`0xE00FFFFF`) is already covered by
`CortexMDebuggable`'s default; add whatever else your chip
exposes.

Quick escape hatch for one-off poking: `set mem
inaccessible-by-default off` in the gdb session disables the
clamping client-side. Useful while debugging the map, not a
substitute for declaring it server-side.

### Mistaking a SOF for an RBF

Symptom (Altera-specific): `Configuration failed: CONF_DONE not
asserted` after shifting an entire SOF's `config_data` through
the JTAG CONFIG instruction.

Cause: SOF's `config_data` section is Quartus's internal frame
format, not the JTAG-ready RBF.

Fix: convert with `quartus_cpf -c file.sof file.rbf` and feed
`.rbf`. acrobe rejects non-RBF input at the top of
`Cyclone10.load` with a clear message — apply the same pattern
to other Altera FPGAs you bring up.

## Integrating with environment

If you implement Target + Debuggable + Loadable per the patterns
above, you get the surrounding integrations for free — no per-chip
work needed.

### GDB

`acrobe gdb-server -r <path>` picks the first Target with a
Debuggable, attaches, and serves. The Responder builds
`target.xml` from `Core.gdb_feature_name` and `Core.registers`;
memory-map from `Debuggable.memory_map` plus the Loadable's
regions; routes `vFlashErase/Write/Done` into `Loadable.write`
(so GDB's `load` command works). Watchpoints (Z2/Z3/Z4) go
through `Core.watchpoint_add` which hits the DWT. Monitor
commands (`monitor reset|halt|resume|erase`) come from
`CortexMDebuggable.monitor`; override and `super()` for chip-
specific extensions.

If your chip needs a custom GDB feature name (RISC-V, ARMv8
heterogeneous), set it on the Core:

```python
class MyCore(CortexMCore):
    gdb_feature_name = "com.example.my-arch"
```

#### Declaring the memory map

The biggest gotcha when bringing a new chip online: GDB applies
strict **memory-map clamping**. When we advertise a memory-map
via `qXfer:memory-map:read+` (we always do, as soon as any flash
region exists), GDB refuses to send `m` packets to addresses
outside any declared region. The failure is invisible from our
side — no protocol traffic, no log line — the user just sees
`Cannot access memory at address 0x4000_xxxx` in the gdb prompt.

What's covered out of the box:

- **Loadable.regions** — every Flash / Ram / Eeprom region you
  attach to your Loadable is in the map. So your chip's main
  flash and any volatile RAM region you declared automatically
  works.
- **`CortexMDebuggable.memory_map` default** — the ARM private
  peripheral bus at `0xE0000000` (1 MiB) is pre-populated, so
  SCS / DWT / FPB / ITM addresses are always GDB-accessible.

What you add per chip (typically in the probe, on the
`CortexMDebuggable` returned by `from_romtable`):

```python
from ..region import Ram

debug = CortexMDebuggable.from_romtable(rt, ap)
# Chip-specific ranges GDB should be able to inspect.
debug.memory_map.append(Ram("sram", 0x20000000, ram_size))
debug.memory_map.append(Ram("ficr", 0x10000000, 0x1000))
debug.memory_map.append(Ram("apb",  0x40000000, 0x80000))
debug.memory_map.append(Ram("ahb",  0x50000000, 0x80000))
```

`Ram` is used as the GDB region type for "accessible but not
flash-programmable" — peripherals, SRAM, factory info ranges,
anything GDB should `m`-read but not try to `vFlashErase`.
Pulling a `Ram` into the debug map doesn't make it a real RAM
target for `Loadable.write` (that path uses `Loadable.regions`
separately) — it's purely a hint to GDB.

Reads against declared-but-reserved addresses (the holes
between AHB and APB blocks, etc.) propagate as bus errors;
`Responder.handle_m` catches them and replies `E01` so GDB
shows `Cannot access` instead of the session dying. No need to
trim the declared range to "only the actually-mapped" sub-block —
declare the architectural span and let the bus return errors on
the holes.

#### Register numbering

Cortex-M `Register.number` is the **GDB regnum** matching stock
GDB's `org.gnu.gdb.arm.m-profile` / `m-system` features:
`xpsr=25`, `msp=26`, `psp=27`. `CortexMCore.__DCRSR_SELECTOR`
maps register names to the chip-side DCRSR selector (the
ARM-defined numbering: `xpsr=16`, `msp=17`, `psp=18`) — used
internally for register I/O. Don't conflate the two when adding
a new core type: the GDB unwinder hard-codes specific regnums
for unwind state (xpsr=25 in particular), so target.xml needs
to match those even if the chip's own selectors disagree.

For chip-specific `monitor` commands, override
`Debuggable.monitor` and dispatch on the command word.

### CLI flashing

The `chip` subcommand uses `Loadable.write`. Inputs that the VFS
auto-detects (Intel hex, ELF, raw bin, vendor-specific) walk
through `MemoryMap.from_node`, which collects addressable leaves.
For your chip to be reachable, your Flash region's `address` and
`size` must match the addresses in the input file — typically
that's just the chip's documented flash base + size.

If your file format needs special handling (signature, header,
encrypted regions), add a `FormatNode` parser in
`acrobe/component/<vendor>/formats.py` and register it via
`@register_format(...)`. The VFS picks it up automatically based
on extension or mime type.

### `info cpu` / `info target -v`

These walk the target tree, so a correctly-shaped target appears
in both automatically. `info cpu` delegates to
`Core.dump_cpu(verbose=full)` — `CortexMCore` already implements
it via SCS feature registers, so nothing to do.

## Plugin packaging (optional)

If your target shouldn't be part of acrobe core (proprietary chip,
NDA'd peripheral, …), package it as a plugin:

```
mychip_acrobe/
├── pyproject.toml
└── mychip_acrobe/
    ├── __init__.py        # registers via acrobe.plugin entry points
    └── target.py          # @Target.register(...) decorators here
```

`acrobe_plugin` is the entry-point namespace; modules registered
there get imported during `load_plugins()` and their decorators
fire before discovery runs.

## Test patterns

The hard parts to test are the JTAG / DAP transactions. Use the
patterns in `tests/test_nrf52.py`:

- `MockAp(MemAp)`: bypasses `MemAp.__init__` via direct
  `Node.__init__`, implements `read32` / `write32` /
  `mem_read` / `mem_write` returning resolved futures, tracks
  every transaction for assertions.
- Build a synthetic component tree: `Node`-based DP, attach the
  MockAp, attach a `RomTable` with `Scs` child — enough to make
  `nrf52_probe` succeed without hardware.
- For Loadable + region test: instantiate them stand-alone (don't
  go through discovery), drive `Loadable.write` on a small
  `MemoryMap`, assert the MockAp's `writes` log matches the
  expected NVMC sequence.

Don't try to integrate with the real Mem-AP machinery — it pulls
in the entire DAP / Batcher stack and isn't worth it for unit
testing.

## Checklist for a new chip

Use this as a final pass before merging:

- [ ] Target class subclasses `CortexMTarget` (or appropriate
      analogue when ARM9 / RV land).
- [ ] `@Target.register(Dp, precedence=<N>)` where `N` < 10000.
- [ ] Probe is async, reads identification registers, raises
      `NoMatch` for unrecognised parts.
- [ ] `target.claim(...)` covers every referenced component.
- [ ] Loadable halts CPU in `pre_program` (unless flash is
      truly contention-free).
- [ ] Flash region's `read` / `erase` / `write` are correct;
      `erase_page_sizes` matches the device's erase granularity.
- [ ] Mass-erase path uses a vendor AP if available.
- [ ] Vendor APs registered against `Ap.db` by IDR, module
      imported from the target file so registration fires.
- [ ] `Debuggable.memory_map` declares every range you want GDB
      to be able to inspect (RAM, FICR / DEVINFO, peripheral
      blocks). The Cortex-M PPB is already covered.
- [ ] Module imported from `acrobe/target/__init__.py`.
- [ ] Tests: probe success/decline, region erase + write
      sequencing, full Loadable.write through to mock bus.
- [ ] Live-verified at least once: `info target -v` lists the
      target, `info cpu` reads CPUID, `chip program <known>.hex`
      succeeds, `gdb-server` lets `arm-none-eabi-gdb` attach.

A first-pass port that ticks every box typically lands in
300–500 lines of target code plus 200–400 lines of tests.
