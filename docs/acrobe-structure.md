# The shape of acrobe

This document is a map. It explains how acrobe is built from the
ground up, what each layer is responsible for, and where the
boundaries are. Read it once before adding a new chip, a new
adapter, or a new CLI command — the rest of the docs make a lot
more sense once you know which layer they live in.

The two sibling guides — `docs/adding-an-adapter.md` and
`docs/adding-a-target.md` — drill into the two layers most people
extend. This one frames where they fit.

## The four layers

```
┌────────────────────────────────────────────────────────────┐
│  4. User operation layer                                   │
│     CLI subcommands, GDB server, programmatic Python use   │
│     `acrobe.cli/`, `acrobe.target.gdb`                     │
├────────────────────────────────────────────────────────────┤
│  3. Target framework                                       │
│     Pattern-match the component tree, build operational    │
│     views (Loadable, Debuggable, Puppet, DebugAuth)        │
│     `acrobe.target/`                                       │
├────────────────────────────────────────────────────────────┤
│  2. Component tree                                         │
│     Chip-shaped Nodes auto-discovered from a live wire     │
│     (TAPs, DPs, APs, ROM tables, SCS, flash IPs, …)        │
│     `acrobe.component/`                                    │
├────────────────────────────────────────────────────────────┤
│  1. Adapter (wire protocol)                                │
│     Speak JTAG / SWD / SPI / I²C / serial through a real   │
│     piece of hardware                                      │
│     `acrobe.adapter/`, `acrobe.protocol/`                  │
└────────────────────────────────────────────────────────────┘
```

Each layer publishes a small surface to the one above it and is
otherwise free to refactor itself. Everything flows through a
single unified Node tree rooted at `HwRoot`.

## The unifying primitive: `Node`

Before layering, the one type to understand is `acrobe.node.Node`
(`acrobe/node.py`). Every live thing in acrobe — an adapter, a
JTAG TAP, an ARM CPU, a flash region, a parsed `.elf` file — is a
Node. Nodes form a tree with three knobs:

* **`child_add(node)`** — eager attach. The parent already knows
  this child exists (an adapter attaching its `jtag` interface
  after `open()`, a probe attaching a `Debuggable`).
* **`child_spawn(name)`** — lazy factory. Override to create a
  child on demand when its name is first requested. Used at
  every layer: `HwRoot.child_spawn` delegates to enumerators,
  `Adapter.child_spawn` returns interface objects,
  `JtagInterface.child_spawn` returns a `Chain`.
* **`child_summon(*parts)`** — path walk. Looks up an existing
  child, otherwise calls `child_spawn`; recurses for the next
  path part. This is what `-r proby-9/jtag/chain/0/dap` does in
  the CLI.

A small set of mixins on top of `Node` (`Readable`, `Writable`,
`Addressable`) describe byte-level surfaces; see
`docs/vfs-design.md` for the VFS side. The component / target /
operation layers all subclass `Node`, so the same `info
enumerate` walk dumps everything in one go.

A second cross-cutting primitive is `acrobe.engine.Batcher`. Most
Nodes that issue wire traffic mix it in: `post(op)` enqueues an
op synchronously and returns a Future, `flush_ops(batch)` runs
when the loop yields. Layers stack — Chain posts to
JtagInterface posts to MpsseEngine posts to FtdiTransport — and
each layer coalesces its own batch into one transaction for the
next one down. See `docs/adding-an-adapter.md` for the full
discussion.

## Layer 1 — Adapter (wire protocol)

**What it owns**: turning a real piece of hardware into a usable
wire protocol.

**Files**: `acrobe/adapter/`, `acrobe/protocol/`.

An *adapter* is a physical thing — a USB-attached JTAG/SWD probe,
an FTDI board, a network endpoint (Altera `jtagd`, Xilinx Virtual
Cable), or a target's own ROM bootloader (RP2040 PICOBOOT). It is
discovered by an enumerator under `HwRoot`, opened to a live
device handle, and asked to spawn interface children for each
wire protocol it supports.

The *protocol* modules (`acrobe/protocol/jtag.py`,
`acrobe/protocol/swd.py`, `acrobe/protocol/spi.py`,
`acrobe/protocol/i2c.py`, `acrobe/protocol/serial.py`) define
abstract `Interface` bases plus the op dataclasses each interface
batches. Adapter-side concrete subclasses
(`JtagMpsse`, `CmsisDapSwdInterface`, `JLinkSwdInterface`,
`StLinkJtagDp`, …) implement `flush_ops` for their hardware.

What the layer publishes upward: a started interface Node
(`adapter/jtag`, `adapter/swd`, …) that accepts protocol ops via
`post()` and resolves the returned future with the natural
result (a `BitString` from a reading JTAG shift, an int from an
SWD AP read, `None` otherwise).

What it does **not** do: it does not know about chips. A JTAG
interface ships JTAG bits; it has no opinion on whether the
device on the other end is Altera, Xilinx, or ARM.

> Adding a new physical adapter is its own guide:
> `docs/adding-an-adapter.md`.

## Layer 2 — Component tree

**What it owns**: a live, structured view of the silicon the
adapter is connected to.

**Files**: `acrobe/component/`.

Once a wire interface is up, the component layer probes it and
parents Node instances that mirror the chip's debug architecture.
The shape is always discovered, never declared by the user.

### JTAG side

`acrobe.protocol.jtag.Chain` is the bridge — a child of the JTAG
`Interface`. Its `discover()` runs the canonical blind-discovery
sequence (TLR, capture-IR / capture-DR length probing, IDCODE
read) and parents one `Tap` per device found in the chain. Each
TAP's IDCODE selects a concrete `Tap` subclass via
`Tap.db` (with revision masking on the IDCODE equality check) —
that's how an Altera Agilex TAP becomes an `Agilex5Tap`, an ARM
DP-JTAG becomes a `JtagDp`, etc. Subclasses then carry on
discovering their own world (an SDM under an Altera SoC, a
CoreSight bus under an ARM DP).

### SWD side

Same shape, simpler: the SWD interface spawns an ARM `SwDp`,
the DP enumerates its APs by walking APSEL space and matching
each AP IDR against `Ap.db`, and each AP discovers what's at
its base — typically a ROM table whose entries spawn more
components (SCS, DWT, FPB, ITM, …).

### Other protocols

SPI Flash chips discover via JEDEC SFDP. I²C devices are
discovered by an explicit probe + per-address driver match
(typed by the bus, not auto-scanned). The pattern is the same:
spawn the abstract Node, let it interrogate the wire, register
chip-specific subclasses keyed on a discoverable identifier.

### Spawning vs. discovery

Some components can be auto-discovered; some can't. The general
rule:

* If the wire exposes an identifier (IDCODE, IDR, JEDEC ID,
  CIDR…), use it to pick a subclass via the relevant `Db`.
* If there's no identifier (a SPI flash on a board where SFDP
  isn't supported, an I²C device at a known address), allow
  *explicit spawning* — `child_summon("flash")` calls
  `child_spawn("flash")` which builds the component blind. The
  user pays for the assumption by typing the name.

What this layer publishes upward: a tree of typed component
Nodes. Every node above the adapter root (`proby-9/jtag/chain/
0/dap/ahb-ap@0/rom@e00ff000/scs`, etc.) is what subsequent
layers pattern-match against.

What it does **not** do: it does not decide what *operations*
the user can run. A `Dp` exists whether you intend to flash the
chip, debug it, or just dump CoreSight ROM tables — it does not
know.

## Layer 3 — Target framework

**What it owns**: the operational view of a real chip.

**Files**: `acrobe/target/`.

Targets are the answer to the question: "Given the component
tree we just discovered, what user-facing operations does this
chip support?" The framework walks the component tree, matches
explorers against component classes, and parents Targets flat
under `HwRoot`. A Target is a Node carrying chip-specific
capability children:

* **`Loadable`** — programmable view. Holds `Region` children
  (Flash, Ram, Eeprom). The CLI's `chip program` and GDB's
  `vFlashErase/Write/Done` both go through `Loadable.write`.
* **`Debuggable`** — run-control + memory view. Holds `Core`
  children and a `memory_map` declaring what GDB is allowed to
  inspect.
* **`Puppet`** — on-target stub execution. Used by flash drivers
  to keep per-page loops off the SWD wire. See `docs/
  adding-a-target.md` § "On-target stubs: the Puppet framework".
* **`DebugAuth`** — keyed unlock flows for locked debug.
* **`Memory`** — flat addressable view of target memory (for
  `acrobe debug read/write`).

### Discovery

`TargetDiscovery.run(root)` is a fixed-point loop:

1. Walk every non-Target node under `root`.
2. For each component, try every `@Target.register(*Types,
   precedence=N)` explorer whose component types match. Lower
   precedence runs first; the generic Cortex-M explorer sits at
   `precedence=10000` so chip-specific probes (nRF52, EFM32 at
   `precedence=500`) get first refusal.
3. An explorer that recognises the chip builds the Target and
   returns it; one that doesn't raises `NoMatch` /
   `NotImplementedError` to decline.
4. Successful explorers call `target.claim(*components)` so
   later passes don't try to spawn another Target from the same
   component.
5. Repeat until a pass spawns nothing new — newly-spawned
   Targets may expose new components (a puppet-driven QSPI
   master with a SpiFlash beneath, etc.).

What the layer publishes upward: a list of started Target
instances, each with whichever capability children apply.

What it does **not** do: the framework has no CLI surface and no
GDB surface of its own. Layer 4 talks to it through plain
Node-tree access (`target.children_of_class(Loadable)` etc.).

> Adding a new chip target is its own guide:
> `docs/adding-a-target.md`.

## Layer 4 — User operation layer

**What it owns**: the entry points the user actually invokes.

**Files**: `acrobe/cli/`, `acrobe/target/gdb/`, plus whatever
the user writes when embedding acrobe as a library.

### The CLI

`acrobe.cli.console::main` is the entry point. It calls
`plugin.load_plugins()` (to fire `acrobe_plugin` entry-point
imports so out-of-tree adapter / chip modules register), then
hands control to the click group built up across
`acrobe/cli/*.py`. Each submodule attaches one subcommand group
to `base.cli`:

* `info` — list adapters, dump component tree, list discovered
  targets, dump CPU info.
* `chain` — JTAG-specific operations: probe, scan, raw shift.
* `chip` — program / verify / readback / erase / reset against
  a Loadable.
* `debug` — Cortex-M run-control: halt, resume, step, reset,
  reg read/write, mem read/write.
* `gdb` — start a GDB Remote Serial Protocol server backed by
  the Target's Debuggable + Loadable.
* `loadable`, `xilinx`, `altera`, `stapl`, `run`, `repl`,
  `rfc2217`, `wire` — assorted.

Two recurring patterns to know:

1. **Root paths.** Most subcommands take one or more
   `-r <path>` arguments. The path walks through the shared
   `HwRoot` via `child_summon`: `proby-9/jtag-pt` opens the
   Proby adapter, summons its `jtag-pt` interface, starts the
   subtree. Subsequent `-r` on the same invocation reuse the
   same `HwRoot`.
2. **Target / Loadable selection.** Commands that need a Target
   call `hw_root.discover_targets()` (triggering Layer 3) and
   pick from `hw_root.children_of_class(Target)` by index or
   substring match. Commands that need a specific Loadable do
   the same one level deeper on `target.children_of_class
   (Loadable)`.

### The GDB server

`acrobe gdb-server -r <path>` finds the first Target with a
Debuggable, attaches, and serves the GDB Remote Serial
Protocol. `target.xml` is built from the Core's register set;
the memory map is built from `Debuggable.memory_map` plus the
Loadable's regions; `vFlashErase/Write/Done` routes into
`Loadable.write` so GDB's `load` command flashes the chip.
Monitor commands (`monitor reset|halt|resume|erase`) dispatch
through `Debuggable.monitor`.

### Library use

There is no separate library API — programmatic users do exactly
what the CLI does:

```python
from acrobe.adapter.model import make_hw_root
from acrobe.target import Loadable

root = make_hw_root()
adapter = await root.child_summon("proby-9")
swd = await adapter.child_summon("swd")
await swd.start_tree()

await root.discover_targets()
target = root.children_of_class(Target)[0]
loadable = target.children_of_class(Loadable)[0]
await loadable.write(my_memory_map)

import acrobe.lifecycle
await acrobe.lifecycle.shutdown()
```

The CLI is a thin shell over this same surface. There is no
hidden state the CLI initialises that library users miss —
except `lifecycle.shutdown()`, which the CLI drains
automatically on exit.

## How a single CLI invocation flows

`acrobe chip -r proby-9/jtag-pt program myapp.hex`:

1. **CLI** parses args, builds a `CliContext` with a fresh
   `HwRoot`.
2. **CLI → Adapter**: `ctx.resolve("proby-9/jtag-pt")` calls
   `hw_root.child_summon("proby-9", "jtag-pt")`. The
   `UsbEnumerator` finds the Proby on the USB bus, opens it,
   `ProbyAdapter` reprograms the FPGA for passthrough mode,
   spawns the `jtag-pt` interface. Layer 1 is now up.
3. **CLI → Component**: `start_tree()` triggers the JTAG
   Chain's `discover()`. TAPs are spawned, IDCODEs matched,
   subclasses chosen, sub-trees recursed (ARM DP → APs → ROM
   table → SCS / DWT / FPB / …). Layer 2 is now populated.
4. **CLI → Target**: `hw_root.discover_targets()` walks the
   tree. The nRF52 explorer recognises the DP, reads FICR,
   builds an `Nrf52Target` with a `Debuggable`, a `Loadable`
   with `NvmcFlash` + `UicrFlash` regions, an `ArmMPuppet`,
   parents it under root. Layer 3 is now ready.
5. **CLI → operation**: the `chip program` command picks the
   Target by index / name, picks the Loadable, walks
   `myapp.hex` through `MemoryMap.from_node`, calls
   `loadable.write(map, do_erase=True)`. That cascades:
   `Loadable.pre_program` halts the CPU, `Region.plan_update`
   pages the data, `NvmcFlash.write` posts SWD packets through
   the puppet stub, which ultimately becomes USB transactions
   driven by the FTDI MPSSE engine in Layer 1.
6. **CLI → cleanup**: on click context close,
   `lifecycle.shutdown()` runs, draining USB contexts and
   sockets.

Every layer in the stack contributed; none knew the layer above
it existed.

## Where the seams are

A few seams are deliberately drawn:

* **Adapter ↔ Component**: the seam is *the abstract protocol
  interface*. Components see `JtagInterface` /
  `swd.Interface` / `SpiInterface`. Adapters implement those.
  Nothing chip-specific leaks down; nothing adapter-specific
  leaks up.
* **Component ↔ Target**: the seam is the *component class*.
  Explorers match on `Dp`, `Picoboot`, `SpiFlashComponent`,
  `SramFpga`, etc. — never on adapter type, never on USB VID.
  This is what lets the nRF52 target work identically over a
  Proby (FTDI MPSSE), a J-Link (vendor firmware), or a
  CMSIS-DAP (HID command set).
* **Target ↔ Operation**: the seam is the *capability child
  class*. `Loadable`, `Debuggable`, `Puppet`, `DebugAuth`.
  CLI subcommands and the GDB server look for those by class,
  never by name. A new capability becomes available to every
  Target that ships it, automatically.

When you find yourself wanting to cross a seam — a probe that
sniffs USB VIDs, a CLI command that does its own JTAG shifts —
stop. There is almost always a cleaner shape one layer up or
down.

## Where things live: quick file map

```
acrobe/
    node.py                # Node, child_add/spawn/summon
    engine.py              # Batcher: post + flush_ops
    db.py                  # Db (registry with custom eq)
    bitstring.py           # BitString (LSB-first)
    lifecycle.py           # on_shutdown / shutdown
    configuration.py       # ~/.config/acrobe.conf
    plugin.py              # acrobe_plugin entry-point loader
    log.py                 # structured logging w/ per-node IDs

    protocol/              # Abstract wire protocols
        jtag.py            #   JTAG ops + JtagInterface + Chain + Tap
        swd.py             #   SWD ops + Interface
        spi.py, i2c.py, serial.py

    adapter/               # Physical adapters (Layer 1)
        model.py           #   HwRoot, AdapterInfo, adapter_db,
                           #   UsbEnumerator, make_hw_root
        ftdi/, jlink/, stlink/, cmsisdap/, xds110/, …
        proby/             #   FPGA-backed adapters
        picoboot/          #   RP2040 BOOTSEL as an "adapter"
        aji/, xvc/         #   Network-attached
        tty.py             #   Serial enumerator

    component/             # Discovered chips (Layer 2)
        arm/               #   DP, AP, MemAp, CoreSight ROM,
                           #   SCS, CPUID, …
        altera/, xilinx/, gowin/, lattice/   # FPGAs + SoCs
        nordic/, ti/, raspberry/, renesas/   # MCU-side IPs
        nsl/               #   Transactors used by Proby firmware

    target/                # Operational view (Layer 3)
        target.py          #   Target + explorer registry
        discovery.py       #   TargetDiscovery fixed-point loop
        loadable.py        #   Loadable + region orchestration
        debuggable.py      #   Debuggable + Core
        region.py          #   Flash / Ram / Eeprom
        puppet.py          #   Puppet framework
        debug_auth.py      #   Locked-debug unlock flows
        memory.py          #   Addressable memory view
        fpga.py, spi_flash.py
        arm/               #   Cortex-M targets (nrf52, efm32, rp2040)
        gdb/               #   GDB Remote Serial Protocol server

    cli/                   # User entry points (Layer 4)
        console.py         #   main entry
        base.py            #   root click group, CliContext, RESOURCE
        info.py, chain.py, chip.py, debug.py, gdb.py, …

    vfs/                   # Format-aware file tree (orthogonal)
        ihex.py, elf.py, bin.py, uf2.py, …
        See docs/vfs-design.md
```

## Where to read next

* Building a new adapter: `docs/adding-an-adapter.md`.
* Bringing up a new chip: `docs/adding-a-target.md`.
* File formats and the `as(type=...)` syntax:
  `docs/vfs-design.md`.
* JTAG re-discovery semantics across detach / TLR:
  `docs/jtag-refresh.md`.
