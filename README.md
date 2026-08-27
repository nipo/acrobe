# Acrobe

Acrobe is a generic hardware probe toolset.  It drives the usual
serial protocols (JTAG, SWD, I2C, SPI, SMBus, 1-wire, UART, ...)
through adapters, and builds on top of them to identify, inspect,
program and debug chips.

## Using acrobe

Three frontends share the same object model:

* **CLI** — the `acrobe` entry point is a Click command tree.  `acrobe
  info adapters` lists detected probe hardware, `acrobe info enumerate
  -r <root>` walks a bus and shows what was found, `acrobe chip -r
  <root> program image.elf` programs a target.  See `acrobe --help`
  for the full command set.
* **REPL** — `acrobe repl` opens an interactive Python shell
  (ptpython) with the component `root` preloaded, for exploratory
  poking at hardware.
* **Scripts** — `acrobe run script.py` (or `acrobe run -m module`)
  runs a Python script with an initialized acrobe context, for
  automated or repetitive jobs.

## Architecture

### Component tree

Everything in acrobe is a `Node` (see `acrobe.node`), organized as a
tree: enumerators at the root, then adapters, interfaces, buses, and
finally chips and their internal blocks.  Children are looked up or
spawned on demand, so a acrobe session only instantiates what it
actually touches.  Tree nodes are addressed by slash-separated paths
where each element selects a child by index or name, optionally with
options in parentheses, e.g.  `ftdi/0/jtag(fmax=6M,reset)`.

Layers:

* `acrobe.adapter` — drivers for probe hardware (FTDI, J-Link,
  CMSIS-DAP, USB Blaster, XVC, plain serial, ...) and the enumerators
  that detect them;
* `acrobe.protocol` — adapter-independent wire protocol interfaces
  (JTAG, SWD, I2C, SPI, ...);
* `acrobe.component` — chip and IP-block drivers, sorted by
  manufacturer or by industry standard;
* `acrobe.target` — merges disparate components back into a common
  feature set (e.g. reset, erase, program).

### I/O batching

Vast majority of operations method return futures.  Operations are
posted in a pending list that is batched to parent as soon as
possible, but not immediately, in a way user may perform multiple
operations and await them at the end without relying on ugly asyncio
constructs.

Nodes that accept batching inherit `acrobe.engine.Batcher`. It
provides sugar to implement this pattern.

### Autodiscovery

Adapters, buses and components are enumerated at runtime and exposed
as one uniform tree.

Autodiscovery is used as much as possible: each protocol in the node
tree can hold a database of potential children, registered by
protocol-specific ID.  When protocol allows so, IDs are looked up
automatically and components are automatically resolved.

### Extensibility

Acrobe is pluggable through the `acrobe_plugin` PEP 420 namespace
package: external packages can contribute enumerators, adapters,
components, targets and CLI commands without touching this tree.

## Documentation

This file is only an overview; the documentation tree lives in `doc/`.

## Previous work

Acrobe mostly started as an asynchronous rewrite of
[Crobe](https://github.com/nipo/crobe), that began before asyncio.

## License

BSD, see `License`.
